import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_trials.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("compare_trials", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class CompareTrialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.contract = {
            "schema_version": 1, "min_pairs": 5, "confidence": 0.95,
            "bootstrap_samples": 300, "seed": 42,
            "scenarios": {"main": {
                "primary": {"metric": "output_token_throughput", "direction": "higher",
                            "min_relative_improvement": 0.02},
                "guardrails": [{"metric": "ttft_p99_s", "direction": "lower",
                                "max_relative_regression": 0.06}],
            }},
        }
        self.trial = {"schema_version": 1, "pairs": []}

    def write(self, name, value):
        path = self.base / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def run_data(self, run_id, scenario, side, throughput=100.0, latency=1.0):
        return {
            "schema_version": 1, "valid": True, "errors": [], "warnings": [],
            "manifest": {
                "schema_version": 1, "run_id": run_id, "scenario": scenario,
                "measurement_mode": "finite_batch", "start_ts": 0.0, "end_ts": 10.0,
                "clock": "client_perf_counter", "profiler_enabled": False,
                "quality_status": "pass", "effective_config_verified": True,
                "run_purpose": "performance",
                "workload_sha256": sha(scenario + "workload"),
                "config_sha256": sha(side), "environment_sha256": sha("env"),
                "metric_contract_sha256": sha("metric"), "benchmark_sha256": sha("script"),
                "client_measurement_policy_sha256": sha("client_policy"),
            },
            "source_hashes": {"manifest.json": sha(run_id + "manifest"),
                              "requests.jsonl": sha(run_id + "requests"),
                              "workload.json": sha(scenario + "workload source"),
                              "metadata.json": sha(side + "metadata source")},
            "metrics": {"output_token_throughput": throughput, "ttft_p99_s": latency,
                        "request_throughput": throughput / 10, "failure_rate": 0.0},
        }

    def add_pairs(self, count=5, scenario="main", gains=None, latencies=None):
        start = len(self.trial["pairs"])
        for offset in range(count):
            index = start + offset
            bname, cname = "b{}.json".format(index), "c{}.json".format(index)
            gain = gains[offset] if gains else 0.10 + offset * 0.001
            latency = latencies[offset] if latencies else 1.0
            self.write(bname, self.run_data("baseline-{}".format(index), scenario, "baseline"))
            self.write(cname, self.run_data("candidate-{}".format(index), scenario, "candidate",
                                          throughput=100 * (1 + gain), latency=latency))
            self.trial["pairs"].append({"scenario": scenario, "baseline": bname, "candidate": cname})

    def edit(self, name, edit):
        path = self.base / name
        data = json.loads(path.read_text())
        edit(data)
        self.write(name, data)

    def compare(self):
        self.write("contract.json", self.contract)
        self.write("trial.json", self.trial)
        return MODULE.compare(self.base / "contract.json", self.base / "trial.json")

    def cli(self, output="report.json"):
        self.write("contract.json", self.contract)
        self.write("trial.json", self.trial)
        result = subprocess.run([sys.executable, str(SCRIPT), "--contract", "contract.json",
                                 "--trial", "trial.json", "--output", output], cwd=self.base,
                                capture_output=True, text=True)
        report = json.loads((self.base / output).read_text()) if (self.base / output).exists() else None
        return result, report

    def test_legal_positive_effect_is_adopted_and_reproducible(self):
        self.add_pairs()
        first, status = self.compare()
        second, _ = self.compare()
        self.assertEqual(status, 0)
        self.assertEqual(first["recommendation"], "adopt")
        self.assertEqual(first["mechanism_status"], "unresolved")
        self.assertEqual(first, second)
        self.assertEqual(len(first["validated_inputs"]), 10)
        self.assertEqual(len(first["scenarios"]["main"]["primary"]["paired_values"]), 5)
        self.assertGreater(first["scenarios"]["main"]["primary"]["confidence_interval"]["lower"], 0.02)

    def test_single_3_8_percent_and_p99_24_percent_does_not_promote(self):
        self.add_pairs(count=1, gains=[0.038], latencies=[1.243])
        report, status = self.compare()
        self.assertEqual(status, 1)
        self.assertEqual(report["recommendation"], "revert")
        self.assertEqual(report["scenarios"]["main"]["primary"]["status"], "inconclusive")
        self.assertEqual(report["scenarios"]["main"]["guardrails"][0]["status"], "violated")
        self.assertIsNone(report["scenarios"]["main"]["primary"]["confidence_interval"])

    def test_insufficient_pairs_defer(self):
        self.add_pairs(count=2)
        report, status = self.compare()
        self.assertEqual((report["recommendation"], status), ("defer", 1))

    def test_missing_required_scenario_defer(self):
        self.add_pairs()
        self.contract["scenarios"]["protected"] = {
            "primary": None, "guardrails": copy.deepcopy(self.contract["scenarios"]["main"]["guardrails"])}
        report, status = self.compare()
        self.assertEqual((report["recommendation"], status), ("defer", 1))
        self.assertIn("required scenario is missing", report["scenarios"]["protected"]["issues"][0])

    def test_null_primary_regression_protection_is_checked(self):
        self.contract["scenarios"]["protected"] = {
            "primary": None, "guardrails": [{"metric": "request_throughput", "direction": "higher",
                                             "max_relative_regression": 0.02}]}
        self.add_pairs()
        self.add_pairs(scenario="protected")
        self.assertEqual(self.compare()[1], 0)
        self.edit("c9.json", lambda d: d["metrics"].update(request_throughput=8))
        report, code = self.compare()
        self.assertEqual((report["recommendation"], code), ("revert", 1))

    def add_protected_scenario(self):
        self.contract["scenarios"]["protected"] = {
            "primary": None,
            "guardrails": [{"metric": "ttft_p99_s", "direction": "lower",
                            "max_relative_regression": 0.06}],
        }
        self.add_pairs()
        self.add_pairs(scenario="protected")

    def test_same_deployments_across_scenarios_allow_distinct_workload_and_client_policy(self):
        self.add_protected_scenario()
        # Different workloads and client concurrency policies are expected in
        # regression scenarios. Only each side's serving configuration is global.
        for index in range(5, 10):
            for side in ("b", "c"):
                self.edit("{}{}.json".format(side, index), lambda d: d["manifest"].update(
                    client_measurement_policy_sha256=sha("protected-client-policy")))
        report, code = self.compare()
        self.assertEqual((report["recommendation"], code), ("adopt", 0))
        self.assertEqual(report["scenarios"]["protected"]["status"], "passed")
        self.assertIn("every required scenario", report["method"]["deployment_scope"])

    def add_implementation_trial(self):
        self.add_protected_scenario()
        change = {}
        for side in ("baseline", "candidate"):
            document = {"module_sha256": sha(side)}
            identity = sha(json.dumps(document, sort_keys=True, separators=(",", ":")))
            change[side + "_sha256"] = identity
            def source(name):
                return {"path": side + "/" + name, "sha256": sha(side + name)}
            fields = {
                "config_sha256": sha("fixed-config"),
                "implementation_sha256": identity,
                "implementation_identity": {"document": document, "source": source("identity.json")},
                "implementation_verified": True,
                "implementation_evidence": {"verified": True, "scope": "fixture", "implementation_sha256": identity,
                                            "source": source("evidence.json"), "artifacts": [source("loaded.txt")]},
                "quality_implementation_sha256": identity,
                "implementation_quality_source": source("quality.json"),
            }
            for pair in self.trial["pairs"]:
                self.edit(pair[side], lambda run: run["manifest"].update(fields))
        self.contract["implementation_change"] = change

    def test_code_only_requires_matching_config_and_no_environment_drift_across_scenarios(self):
        self.add_implementation_trial()
        for k in ("environment_sha256", "config_sha256"):
            with self.subTest(field=k):
                old = json.loads((self.base / "c9.json").read_text())["manifest"][k]
                self.edit("c9.json", lambda r: r["manifest"].update({k: sha("drift")}))
                with self.assertRaises(MODULE.InputError):
                    self.compare()
                self.edit("c9.json", lambda r: r["manifest"].update({k: old}))
        for pair in self.trial["pairs"][5:]:
            for side in ("baseline", "candidate"):
                self.edit(pair[side], lambda r: r["manifest"].update(environment_sha256=sha("another-image")))
        with self.assertRaisesRegex(MODULE.InputError, "identical config and environment"):
            self.compare()

    def test_implementation_provenance_and_each_side_required_on_all_runs(self):
        self.add_implementation_trial()
        original = json.loads((self.base / "c9.json").read_text())
        def drop_identity(r):
            for key in MODULE.FIELDS:
                r["manifest"].pop(key, None)
        cases = [
            drop_identity,
            lambda r: r["manifest"].pop("implementation_evidence"),
            lambda r: r["manifest"].update(implementation_sha256=sha("drift")),
            lambda r: r["manifest"]["implementation_evidence"].update(artifacts=[]),
            lambda r: r["manifest"]["implementation_evidence"].update(implementation_sha256=sha("wrong")),
            lambda r: r["manifest"].pop("quality_implementation_sha256"),
            lambda r: r["manifest"].update(quality_implementation_sha256=sha("wrong")),
        ]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                self.edit("c9.json", mutate)
                with self.assertRaises(MODULE.InputError):
                    self.compare()
                self.write("c9.json", original)

    def test_implementation_change_must_name_two_different_valid_digests(self):
        self.add_pairs()
        for change in (None, {}, {"baseline_sha256": sha("base"), "candidate_sha256": sha("base")},
                       {"baseline_sha256": "short", "candidate_sha256": sha("candidate")}):
            with self.subTest(change=change):
                self.contract["implementation_change"] = change
                with self.assertRaises(MODULE.InputError):
                    self.compare()

    def test_protected_scenario_cannot_substitute_baseline_for_candidate(self):
        self.add_protected_scenario()
        for index in range(5, 10):
            self.edit("c{}.json".format(index), lambda d: d["manifest"].update(
                config_sha256=sha("baseline")))
        result, report = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["recommendation"], "defer")
        self.assertFalse(report["input_valid"])
        self.assertIn("candidate deployment config drift", report["errors"][0])

    def test_protected_scenario_cannot_substitute_candidate_for_baseline(self):
        self.add_protected_scenario()
        for index in range(5, 10):
            self.edit("b{}.json".format(index), lambda d: d["manifest"].update(
                config_sha256=sha("candidate")))
        with self.assertRaisesRegex(MODULE.InputError, "baseline deployment config drift"):
            self.compare()

    def test_incomplete_invalid_run_does_not_enter_global_deployment_check(self):
        self.add_protected_scenario()
        self.edit("c5.json", lambda d: d.update(
            valid=False, errors=["incomplete manifest"], manifest={}, metrics={}))
        report, code = self.compare()
        self.assertEqual((report["recommendation"], code), ("defer", 1))
        self.assertTrue(report["input_valid"])
        self.assertIn("candidate: validator reported valid=false",
                      report["pairs"][5]["eligibility_issues"])

    def test_valid_but_ineligible_run_still_cannot_change_deployment_config(self):
        self.add_protected_scenario()
        for index in range(5, 10):
            self.edit("c{}.json".format(index), lambda d: d["manifest"].update(
                config_sha256=sha("different-deployment"), quality_status="not_evaluated"))
        with self.assertRaisesRegex(MODULE.InputError, "candidate deployment config drift"):
            self.compare()

    def test_mismatched_fingerprints_are_invalid(self):
        for key in MODULE.IDENTITIES:
            with self.subTest(key=key):
                self.trial["pairs"] = []
                self.add_pairs()
                self.edit("c0.json", lambda d: d["manifest"].update({key: sha("different")}))
                with self.assertRaisesRegex(MODULE.InputError, "mismatch"):
                    self.compare()

    def test_config_drift_and_workload_drift_across_repeats(self):
        self.add_pairs()
        self.edit("c4.json", lambda d: d["manifest"].update(config_sha256=sha("unexpected parameter")))
        with self.assertRaisesRegex(MODULE.InputError, "drift"):
            self.compare()
        self.edit("c4.json", lambda d: d["manifest"].update(config_sha256=sha("candidate")))
        for name in ("b4.json", "c4.json"):
            self.edit(name, lambda d: d["manifest"].update(workload_sha256=sha("new workload")))
        with self.assertRaisesRegex(MODULE.InputError, "drift"):
            self.compare()

    def test_duplicate_run_id_is_rejected(self):
        self.add_pairs()
        self.edit("c4.json", lambda d: d["manifest"].update(run_id="candidate-0"))
        with self.assertRaisesRegex(MODULE.InputError, "run_id reused"):
            self.compare()

    def test_same_file_and_hardlink_are_rejected(self):
        self.add_pairs()
        self.trial["pairs"][1]["baseline"] = "b0.json"
        with self.assertRaisesRegex(MODULE.InputError, "run file was reused"):
            self.compare()
        os.link(self.base / "b0.json", self.base / "alias.json")
        self.trial["pairs"][1]["baseline"] = "alias.json"
        with self.assertRaisesRegex(MODULE.InputError, "run file was reused"):
            self.compare()

    def test_same_raw_requests_renamed_as_independent_run_are_rejected(self):
        self.add_pairs()
        self.edit("c4.json", lambda d: d["source_hashes"].update({"requests.jsonl": sha("candidate-0requests")}))
        with self.assertRaisesRegex(MODULE.InputError, "Identical requests"):
            self.compare()

    def test_all_four_source_hashes_are_required(self):
        for filename in ("manifest.json", "requests.jsonl", "workload.json", "metadata.json"):
            with self.subTest(filename=filename):
                self.trial["pairs"] = []
                self.add_pairs()
                self.edit("c0.json", lambda d: d["source_hashes"].pop(filename))
                with self.assertRaisesRegex(MODULE.InputError, "source_hashes.*must be a 64-digit"):
                    self.compare()

    def test_ineligible_runs_defer_without_claiming_harm(self):
        mutations = [
            lambda d: d["manifest"].update(profiler_enabled=True),
            lambda d: d["manifest"].update(quality_status="not_evaluated"),
            lambda d: d["manifest"].update(quality_status="fail"),
            lambda d: d["manifest"].update(effective_config_verified=False),
            lambda d: d["manifest"].update(diagnostic=True),
            lambda d: d["manifest"].update(run_purpose="diagnostic"),
            lambda d: d.update(valid=False, errors=["invalid timing"]),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.trial["pairs"] = []
                self.add_pairs()
                self.edit("c0.json", mutation)
                report, code = self.compare()
                self.assertEqual((report["recommendation"], code), ("defer", 1))
                self.assertTrue(report["pairs"][0]["eligibility_issues"])

    def test_invalid_run_with_partial_manifest_is_not_promotable(self):
        self.add_pairs()
        self.edit("c0.json", lambda d: d.update(valid=False, errors=["missing identity"], manifest={}))
        report, code = self.compare()
        self.assertEqual((report["recommendation"], code), ("defer", 1))

    def test_run_purpose_is_required_and_never_defaults_to_performance(self):
        for value in (None, "", "benchmark", True, 1, [], {}):
            with self.subTest(value=value):
                self.trial["pairs"] = []
                self.add_pairs()
                self.edit("c0.json", lambda d: d["manifest"].update(run_purpose=value))
                with self.assertRaisesRegex(MODULE.InputError, "run_purpose must be"):
                    self.compare()
        self.edit("c0.json", lambda d: d["manifest"].pop("run_purpose"))
        with self.assertRaisesRegex(MODULE.InputError, "run_purpose must be"):
            self.compare()

    def test_diagnostic_baseline_also_cannot_promote(self):
        self.add_pairs()
        self.edit("b0.json", lambda d: d["manifest"].update(run_purpose="diagnostic"))
        report, status = self.compare()
        self.assertEqual((report["recommendation"], status), ("defer", 1))
        self.assertIn("baseline: run_purpose=diagnostic", report["pairs"][0]["eligibility_issues"])

    def test_missing_metric_or_zero_denominator_defer(self):
        self.add_pairs()
        self.edit("b0.json", lambda d: d["metrics"].update(output_token_throughput=0))
        report, code = self.compare()
        self.assertEqual((report["recommendation"], code), ("defer", 1))
        self.assertIn("zero", report["scenarios"]["main"]["primary"]["missing_or_undefined"][0]["reason"])
        self.edit("b0.json", lambda d: d["metrics"].update(output_token_throughput=100))
        self.edit("c0.json", lambda d: d["metrics"].pop("ttft_p99_s"))
        self.assertEqual(self.compare()[1], 1)

    def test_every_guardrail_is_checked(self):
        self.add_pairs()
        self.contract["scenarios"]["main"]["guardrails"].append(
            {"metric": "not_collected", "direction": "lower", "max_relative_regression": 0.01})
        report, code = self.compare()
        self.assertEqual((report["recommendation"], code), ("defer", 1))
        self.assertEqual(len(report["scenarios"]["main"]["guardrails"]), 2)

    def test_absolute_failure_rate_zero_needs_no_relative_denominator(self):
        self.add_pairs()
        self.contract["scenarios"]["main"]["guardrails"].append(
            {"metric": "failure_rate", "max_value": 0})
        report, code = self.compare()
        self.assertEqual((report["recommendation"], code), ("adopt", 0))
        protection = report["scenarios"]["main"]["guardrails"][1]
        self.assertEqual(protection["n_defined_pairs"], 5)
        self.assertIsNone(protection["confidence_interval"])
        self.edit("c2.json", lambda d: d["metrics"].update(failure_rate=0.01))
        report, code = self.compare()
        self.assertEqual((report["recommendation"], code), ("revert", 1))
        violation = report["scenarios"]["main"]["guardrails"][1]["observed_violations"][0]
        self.assertEqual(violation["constraint"], "max_value")
        self.assertEqual(violation["candidate_value"], 0.01)

    def test_absolute_minimum_and_combined_relative_guardrail(self):
        self.add_pairs()
        self.contract["scenarios"]["main"]["guardrails"][0]["max_value"] = 1.01
        self.contract["scenarios"]["main"]["guardrails"].append(
            {"metric": "request_throughput", "min_value": 10})
        self.assertEqual(self.compare()[1], 0)
        self.edit("c0.json", lambda d: d["metrics"].update(ttft_p99_s=1.02))
        # Relative +2% satisfies the 6% cap; the absolute limit still rejects it.
        self.assertEqual(self.compare()[0]["recommendation"], "revert")
        self.edit("c0.json", lambda d: d["metrics"].update(ttft_p99_s=1, request_throughput=9.9))
        self.assertEqual(self.compare()[0]["recommendation"], "revert")

    def test_absolute_guardrail_missing_candidate_defer_but_baseline_optional(self):
        self.add_pairs()
        self.contract["scenarios"]["main"]["guardrails"].append(
            {"metric": "failure_rate", "max_value": 0})
        self.edit("b0.json", lambda d: d["metrics"].pop("failure_rate"))
        self.assertEqual(self.compare()[1], 0)
        self.edit("c0.json", lambda d: d["metrics"].pop("failure_rate"))
        self.assertEqual(self.compare()[0]["recommendation"], "defer")

    def test_absolute_violation_survives_undefined_relative_effect(self):
        self.add_pairs()
        self.contract["scenarios"]["main"]["guardrails"].append(
            {"metric": "failure_rate", "direction": "lower",
             "max_relative_regression": 0.0, "max_value": 0.0})
        self.edit("c0.json", lambda d: d["metrics"].update(failure_rate=0.01))
        report, _ = self.compare()
        self.assertEqual(report["recommendation"], "revert")
        self.assertEqual(report["scenarios"]["main"]["guardrails"][1]["status"], "violated")

    def test_malformed_absolute_guardrails_rejected(self):
        self.add_pairs()
        malformed = [
            {"metric": "failure_rate"},
            {"metric": "failure_rate", "direction": "lower", "max_value": 0},
            {"metric": "failure_rate", "max_relative_regression": 0},
            {"metric": "failure_rate", "max_value": -1},
            {"metric": "failure_rate", "min_value": 1, "max_value": 0},
        ]
        for rule in malformed:
            with self.subTest(rule=rule):
                self.contract["scenarios"]["main"]["guardrails"] = [rule]
                with self.assertRaises(MODULE.InputError):
                    self.compare()

    def test_lower_is_better_primary_and_higher_guardrail(self):
        self.contract["scenarios"]["main"] = {
            "primary": {"metric": "ttft_p99_s", "direction": "lower", "min_relative_improvement": 0.02},
            "guardrails": [{"metric": "request_throughput", "direction": "higher", "max_relative_regression": 0.05}],
        }
        self.add_pairs(latencies=[0.90, 0.89, 0.91, 0.88, 0.90])
        report, code = self.compare()
        self.assertEqual((report["recommendation"], code), ("adopt", 0))
        self.assertAlmostEqual(report["scenarios"]["main"]["primary"]["mean_relative_improvement"], 0.104)
        self.edit("c2.json", lambda d: d["metrics"].update(request_throughput=9.0))
        self.assertEqual(self.compare()[0]["recommendation"], "revert")

    def test_effect_below_threshold_or_interval_spanning_threshold_defer(self):
        for gains in ([0.01] * 5, [0.10, 0.04, -0.04, 0.0, 0.02]):
            self.trial["pairs"] = []
            self.add_pairs(gains=gains)
            self.assertEqual(self.compare()[0]["recommendation"], "defer")

    def test_exact_threshold_is_not_misclassified_by_float_roundoff(self):
        self.add_pairs(gains=[0.02] * 5, latencies=[1.06] * 5)
        report, code = self.compare()
        self.assertEqual((report["recommendation"], code), ("defer", 1))
        self.assertEqual(report["scenarios"]["main"]["guardrails"][0]["status"], "passed")
        self.assertEqual(report["scenarios"]["main"]["primary"]["status"], "inconclusive")

    def test_primary_clear_regression_reverts(self):
        self.add_pairs(gains=[-0.10] * 5)
        self.assertEqual(self.compare()[0]["recommendation"], "revert")

    def test_observed_guardrail_violation_cannot_be_averaged_away(self):
        self.add_pairs(latencies=[0.80, 0.80, 0.80, 0.80, 1.061])
        report, _ = self.compare()
        self.assertEqual(report["recommendation"], "revert")
        self.assertLess(report["scenarios"]["main"]["guardrails"][0]["mean_relative_regression"], 0)

    def test_nan_in_any_input_and_exponent_overflow_are_rejected(self):
        self.add_pairs()
        for value in ("NaN", "Infinity", "1e999"):
            with self.subTest(value=value):
                data = self.run_data("candidate-0", "main", "candidate", throughput=110)
                raw = json.dumps(data).replace('"ttft_p99_s": 1.0', '"ttft_p99_s": ' + value)
                (self.base / "c0.json").write_text(raw)
                with self.assertRaisesRegex(MODULE.InputError, "non-finite"):
                    self.compare()

    def test_schema_invalid_metric_boolean_and_non_boolean_flags(self):
        self.add_pairs()
        self.edit("c0.json", lambda d: d["metrics"].update(output_token_throughput=True))
        with self.assertRaisesRegex(MODULE.InputError, "boolean"):
            self.compare()
        self.edit("c0.json", lambda d: d["metrics"].update(output_token_throughput=110))
        self.edit("c0.json", lambda d: d["manifest"].update(effective_config_verified="true"))
        with self.assertRaisesRegex(MODULE.InputError, "JSON boolean"):
            self.compare()

    def test_bounded_statistical_parameters(self):
        self.add_pairs()
        for key, value in [("min_pairs", 1), ("min_pairs", True), ("confidence", 1),
                           ("confidence", 0.1), ("bootstrap_samples", 10**9),
                           ("bootstrap_samples", 2.5), ("seed", -1)]:
            with self.subTest(key=key, value=value):
                original = self.contract[key]
                self.contract[key] = value
                with self.assertRaises(MODULE.InputError):
                    self.compare()
                self.contract[key] = original

    def test_contract_unknown_fields_cannot_hide_ignored_constraint(self):
        self.add_pairs()
        self.contract["scenarios"]["main"]["quality_gate"] = "must_pass"
        with self.assertRaisesRegex(MODULE.InputError, "unsupported fields"):
            self.compare()

    def test_only_protection_without_objective_is_rejected(self):
        self.add_pairs()
        self.contract["scenarios"]["main"]["primary"] = None
        with self.assertRaisesRegex(MODULE.InputError, "primary metric"):
            self.compare()

    def test_duplicate_json_fields_are_rejected(self):
        self.add_pairs()
        self.write("contract.json", self.contract)
        (self.base / "trial.json").write_text('{"schema_version":1,"schema_version":1,"pairs":[]}')
        with self.assertRaisesRegex(MODULE.InputError, "Duplicate JSON"):
            MODULE.compare(self.base / "contract.json", self.base / "trial.json")

    def test_cli_exit_codes_and_no_overwrite(self):
        self.add_pairs()
        result, report = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        before = (self.base / "report.json").read_bytes()
        result, _ = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual((self.base / "report.json").read_bytes(), before)
        self.contract["seed"] = -1
        result, report = self.cli("bad.json")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(report["input_valid"])
        self.assertEqual(report["recommendation"], "defer")

    def test_cli_defer_returns_one(self):
        self.add_pairs(count=1)
        result, report = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["recommendation"], "defer")

    def test_help_is_side_effect_free(self):
        before = list(self.base.iterdir())
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=self.base,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(list(self.base.iterdir()), before)


if __name__ == "__main__":
    unittest.main()

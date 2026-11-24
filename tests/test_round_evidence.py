"""Round-level gates exercised with collector SSE and explicitly synthetic timings."""
import json
from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import round_evidence as gate
from evaluate.tests import test_workflow_integration as workflow


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class RoundEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow.WorkflowIntegrationTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        workflow.WorkflowIntegrationTests.tearDownClass()

    def setUp(self):
        self.fixture = workflow.WorkflowIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.ref = save(self.root / "gold.json", {"r0": "several tokens together"})
        self.rule = {"method": "exact_match", "field": "content",
                     "reference": {"path": self.ref.name, "sha256": gate.file_hash(self.ref)}}
        self.contract = save(self.root / "contract.json", {
            "schema_version": 1, "min_pairs": 2, "confidence": 0.95,
            "seed": 19, "bootstrap_samples": 100, "scenarios": {"offline": {
                "primary": {"metric": "output_token_throughput", "direction": "higher",
                            "min_relative_improvement": 0.02},
                "guardrails": [{"metric": "failure_rate", "max_value": 0.0}]}},
            "quality_policy": {"scenarios": {"offline": self.rule}}})

    def build(self, source_change=False, fixed_source=False):
        identities = []
        runs = {}
        for side, wall in (("baseline", 10), ("candidate", 5)):
            implementation = "same-build" if fixed_source else (side if source_change else None)
            _, template, _, _, _ = self.fixture.collect(quality=None, implementation=implementation)
            p = template.parent
            config = {"setting": "fixed" if source_change else side}
            save(p / "config.json", config)
            observed = save(p / "observation.json", {
                "schema_version": 1, "instance_id": side + "-worker@start", "model": "offline-mock",
                "base_url": self.fixture.url, "effective_config": config,
                "implementation_sha256": load(template / "manifest.json").get("implementation_sha256")})
            config_evidence = save(p / "config-evidence.json", {
                "verified": True, "scope": "offline fixture", "observation": observed.name,
                "evidence": [observed.name, "effective-config.txt"]})
            args = ["--scenario", "offline", "--config", str(p / "config.json"),
                    "--environment", str(p / "environment.json"),
                    "--metric-contract", str(p / "metric-contract.json"), "--profiler-enabled", "off",
                    "--run-purpose", "performance", "--config-evidence", str(config_evidence),
                    "--output", str(p / "reprepared.json")]
            if source_change or fixed_source:
                args += ["--implementation", str(p / "implementation.json"),
                         "--implementation-evidence", str(p / "implementation-evidence.json")]
            self.assertEqual(self.fixture.invoke(workflow.prepare.main, args), 0)
            metadata = load(p / "reprepared.json")
            shutil.copy(p / "reprepared.json", template / "metadata.json")
            manifest = load(template / "manifest.json")
            manifest.update(config_sha256=metadata["config_sha256"],
                            metadata_sha256=gate.file_hash(template / "metadata.json"))
            save(template / "manifest.json", manifest)
            identities.append(gate.identity_of(manifest))
            for i in range(2):
                d = self.fixture.synthetic_raw(template, side + str(i), wall).parent
                shutil.copytree(template / "raw", d / "raw")
                runs[side + str(i)] = d
        trial = {"schema_version": 1, "pairs": [
            {"scenario": "offline", **{s: runs[s + str(i)].name for s in ("baseline", "candidate")}}
            for i in range(2)]}
        self.evidence = save(self.root / "evidence.json", trial)
        self.runs = runs
        self.identities = identities

    def evaluate(self):
        return gate.assess(self.contract, self.evidence, *self.identities)

    def goodput_contract(self, **thresholds):
        contract = load(self.contract)
        contract["scenarios"]["offline"]["primary"]["metric"] = "goodput"
        contract["goodput_policy"] = {"scenarios": {"offline": {
            "max_ttft_s": 2, "max_e2e_s": 10, **thresholds}}}
        save(self.contract, contract)
        return contract

    def test_rebuild_decision_and_all_provenance(self):
        self.build()
        self.assertEqual(gate.validate_policy(self.contract), [
            {"path": str(self.ref.resolve()), "sha256": gate.file_hash(self.ref)}])
        path = self.runs["candidate0"] / "validated.json"
        save(path, {"valid": True, "metrics": {"output_token_throughput": 9999}})
        result = self.evaluate()
        self.assertEqual(result["recommendation"], "adopt")
        self.assertNotIn("goodput", result)
        self.assertEqual(set(result["case_identity"]["offline"]),
                         set(gate.compare_trials.IDENTITIES) | {"measurement_mode"})
        self.assertEqual(len(set(result["run_ids"])), 4)
        self.assertEqual(len(set(result["requests_hashes"])), 4)
        for ref in result["evidence_files"]:
            self.assertEqual(gate.file_hash(ref["path"]), ref["sha256"])
        self.assertNotIn(str(path), {ref["path"] for ref in result["evidence_files"]})
        for record in result["comparison"]["validated_inputs"]:
            self.assertTrue(Path(record["path"]).is_dir())
            self.assertLess(record["metrics"]["output_token_throughput"], 99)
            self.assertNotIn("goodput", record["metrics"])

    def test_goodput_uses_the_intersection_and_full_run_wall_time(self):
        modes = ("normal", "normal", "normal", "normal", "empty_zero", "broken_stream")
        _, run, _, _, rows = self.fixture.collect(quality=None, modes=modes)
        manifest = load(run / "manifest.json")
        manifest.update(start_ts=100, end_ts=110)
        save(run / "manifest.json", manifest)
        timing = [(2, 4), (3, 4), (2, 5), (3, 5), (None, 2), (1, 2)]
        requests = run / "requests.jsonl"
        for row in rows:
            ttft, e2e = timing[int(row["request_id"][1:])]
            first = 100 + ttft if ttft is not None else None
            events = [first] * len(row["output_event_ts"])
            end = 100 + e2e
            row.update(planned_send_ts=100, send_start_ts=100, response_open_ts=100,
                       first_output_ts=first, last_output_ts=first,
                       finish_reason_ts=end if row["finish_reason"] is not None else None,
                       final_usage_ts=end if row["final_usage_received"] else None,
                       stream_done_ts=end if row["stream_done_received"] else None,
                       end_ts=end, output_event_ts=events)
        requests.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        validated = gate.validate_run.inspect_run(run)
        self.assertTrue(validated["valid"], validated["errors"])
        gold = {f"r{i}": "several tokens together" for i in range(len(rows))}
        gold.update(r3="incorrect reference for r3", r4="")
        quality = gate.score_run(run, validated, (self.rule, .4, gold), {})
        self.assertTrue(quality["passed"])
        result = gate.measure_goodput(run, validated, {"max_ttft_s": 2, "max_e2e_s": 4}, quality)
        self.assertEqual(result["qualifying_request_ids"], ["r0"])
        self.assertEqual(result["qualifying_output_tokens"], 7)
        self.assertEqual(result["wall_s"], 10)
        self.assertAlmostEqual(result["output_tokens_per_s"], .7)
        self.assertGreater(validated["metrics"]["output_token_throughput"], .7)
        rejected = {reason: set(ids) for reason, ids in result["excluded_request_ids"].items()}
        self.assertIn("r5", rejected["failed"])
        self.assertIn("r3", rejected["quality"])
        self.assertTrue({"r1", "r3", "r4"} <= rejected["ttft"])
        self.assertTrue({"r2", "r3"} <= rejected["e2e"])
        requests.write_bytes(requests.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            gate.measure_goodput(run, validated, {"max_ttft_s": 2, "max_e2e_s": 4}, quality)

    def test_goodput_comparison_preserves_raw_metrics_and_guardrails(self):
        self.build()
        contract = self.goodput_contract()
        result = self.evaluate()
        self.assertEqual(result["recommendation"], "adopt")
        primary = result["comparison"]["scenarios"]["offline"]["primary"]
        self.assertEqual(primary["metric"], "goodput")
        self.assertEqual(primary["mean_relative_improvement"], 1)
        for record in result["comparison"]["validated_inputs"]:
            report = result["goodput"][record["manifest"]["run_id"]]
            self.assertEqual(report["thresholds"]["max_ttft_s"], 2)
            self.assertEqual(report["thresholds"]["max_e2e_s"], 10)
            self.assertEqual(report["qualifying_output_tokens"], 7)
            self.assertEqual(report["output_tokens_per_s"], record["metrics"]["goodput"])
            self.assertEqual(record["metrics"]["output_token_throughput"], record["metrics"]["goodput"])
        contract["scenarios"]["offline"]["guardrails"].append({"metric": "e2e_p99_s", "max_value": 4})
        save(self.contract, contract)
        result = self.evaluate()
        self.assertEqual(result["recommendation"], "revert")
        self.assertEqual(result["comparison"]["scenarios"]["offline"]["primary"]["status"], "passed")

    def test_visible_first_output_uses_replayed_channel_positions(self):
        self.build()
        self.goodput_contract(max_ttft_s=4, first_output_channel="content")
        result = self.evaluate()
        for side, run in self.runs.items():
            row = load(run / "requests.jsonl")
            self.assertEqual(row["first_channel_event"], {"reasoning_content": 0, "content": 1})
            measured = result["goodput"][row["run_id"]]
            self.assertEqual(measured["qualifying_output_tokens"], 7 if side.startswith("candidate") else 0)
        # Correct-looking timestamps do not justify moving a content event earlier than the SSE bytes.
        row["first_channel_event"]["content"] = 0
        save(run / "requests.jsonl", row)
        with self.assertRaisesRegex(ValueError, "first_channel_event"):
            self.evaluate()

    def test_content_counts_require_explicit_usage_and_do_not_hide_request_goodput(self):
        self.build()
        self.goodput_contract(max_ttft_s=10, token_source="server_content_tokens")
        unavailable = self.evaluate()
        self.assertEqual(unavailable["recommendation"], "defer")
        for report in unavailable["goodput"].values():
            self.assertIsNone(report["output_tokens_per_s"])
            self.assertGreater(report["qualified_requests_per_s"], 0)
            self.assertGreater(report["qualified_server_completion_tokens_per_s"], 0)
            self.assertEqual(report["missing_content_token_request_ids"], ["r0"])
        contract = load(self.contract)
        contract["scenarios"]["offline"]["primary"]["metric"] = "qualified_requests_per_s"
        save(self.contract, contract)
        self.assertEqual(self.evaluate()["recommendation"], "adopt")
        self.goodput_contract(max_ttft_s=10, token_source="server_content_tokens")
        for run in self.runs.values():
            stream = run / "raw/00000000.sse"
            text = stream.read_text()
            self.assertIn('"completion_tokens": 7', text)
            stream.write_text(text.replace('"completion_tokens": 7',
                                          '"completion_tokens": 7, "completion_tokens_details": {"content_tokens": 3}'))
            record = load(run / "requests.jsonl")
            record["server_content_tokens"] = 3
            save(run / "requests.jsonl", record)
        available = self.evaluate()
        self.assertEqual(available["recommendation"], "adopt")
        self.assertTrue(all(report["qualifying_output_tokens"] == 3 for report in available["goodput"].values()))

    def test_open_loop_planned_latency_and_legacy_sse_reconstruction(self):
        self.build()
        run = self.runs["candidate0"]
        manifest = load(run / "manifest.json")
        policy = manifest["client_measurement_policy"]
        policy.pop("channel_evidence_version")
        policy["mode"] = "open_loop"
        manifest["client_measurement_policy_sha256"] = gate.validate_run.canonical_hash(policy)
        workload = load(run / "workload.json")
        workload[0]["arrival_offset_ms"] = 0
        manifest["workload_sha256"] = gate.validate_run.canonical_hash(workload)
        save(run / "manifest.json", manifest)
        save(run / "workload.json", workload)
        record = load(run / "requests.jsonl")
        record.pop("first_channel_event")
        record.pop("server_content_tokens")
        record["send_start_ts"] += 0.4
        save(run / "requests.jsonl", record)
        checked = gate.validate_run.inspect_run(run)
        self.assertTrue(checked["valid"], checked["errors"])
        thresholds = {"max_e2e_s": 4.8, "max_ttft_s": 4, "first_output_channel": "content"}
        self.assertEqual(gate.measure_goodput(run, checked, thresholds, None)["qualifying_output_tokens"], 7)
        thresholds["latency_origin"] = "planned_arrival"
        self.assertEqual(gate.measure_goodput(run, checked, thresholds, None)["excluded_request_ids"]["e2e"], ["r0"])
        record["planned_send_ts"] += 0.1
        save(run / "requests.jsonl", record)
        self.assertFalse(gate.validate_run.inspect_run(run)["valid"])

    def test_goodput_zero_baseline_defers_relative_gate(self):
        self.build()
        self.goodput_contract(max_e2e_s=5)
        result = self.evaluate()
        self.assertEqual(result["recommendation"], "defer")
        primary = result["comparison"]["scenarios"]["offline"]["primary"]
        self.assertEqual(primary["n_defined_pairs"], 0)
        self.assertIn("baseline is zero", primary["missing_or_undefined"][0]["reason"])
        self.assertEqual(primary["paired_values"][0]["baseline"], 0)
        self.assertGreater(primary["paired_values"][0]["candidate"], 0)

    def test_parallel_goodput_primary_is_required_alongside_throughput(self):
        self.build()
        legacy_report = self.evaluate()["comparison"]["scenarios"]["offline"]
        contract = self.goodput_contract()
        scenario = contract["scenarios"]["offline"]
        scenario["goodput_primary"] = scenario["primary"]
        scenario["primary"] = {**scenario["primary"], "metric": "output_token_throughput"}
        save(self.contract, contract)
        initial = self.evaluate()
        self.assertEqual(initial["recommendation"], "adopt")
        measured = initial["comparison"]["scenarios"]["offline"]
        for name in ("primary", "guardrails"):
            self.assertEqual(measured[name], legacy_report[name])
        # Faster total completion can coexist with a late first payload. The
        # synthetic timing change keeps each original SSE, token count and T.
        for name in ("candidate0", "candidate1"):
            path = self.runs[name] / "requests.jsonl"
            row = load(path)
            first = row["send_start_ts"] + 3
            row.update(first_output_ts=first, output_event_ts=[first, first, row["last_output_ts"]])
            save(path, row)
        result = self.evaluate()
        measured = result["comparison"]["scenarios"]["offline"]
        self.assertEqual(result["recommendation"], "revert")
        self.assertEqual(measured["primary"]["status"], "passed")
        self.assertEqual(measured["goodput_primary"]["status"], "regressed")
        self.assertEqual(measured["goodput_primary"]["mean_relative_improvement"], -1)

    def test_absolute_goodput_crossing_from_zero_retains_other_gates(self):
        self.build()
        contract = self.goodput_contract(max_e2e_s=5)
        spec = contract["scenarios"]["offline"]
        raw = {**spec["primary"], "metric": "output_token_throughput"}
        floor = {"metric": "goodput", "direction": "higher", "min_value": 7 / 5}
        spec.update(primary=raw, goodput_primary=floor)
        save(self.contract, contract)
        report = self.evaluate()
        self.assertEqual(report["recommendation"], "adopt")
        result = report["comparison"]["scenarios"]["offline"]["goodput_primary"]
        self.assertEqual(result["status"], "passed")
        self.assertEqual([(p["baseline"], p["candidate"]) for p in result["paired_values"]], [(0, 7 / 5)] * 2)
        self.assertIsNone(result["confidence_interval"])
        self.assertNotIn("mean_relative_improvement", result)
        spec["guardrails"].append({"metric": "output_token_throughput", "max_value": 1})
        save(self.contract, contract)
        self.assertEqual(self.evaluate()["recommendation"], "revert")

    def test_absolute_goodput_needs_every_pair_to_cross_observed_target(self):
        self.build()
        contract = self.goodput_contract(max_ttft_s=10)
        spec = contract["scenarios"]["offline"]
        floor = {"metric": "goodput", "direction": "higher", "min_value": 1}
        spec["primary"] = floor
        for target in (7 / 10, 2):
            with self.subTest(target=target):
                floor["min_value"] = target
                save(self.contract, contract)
                report = self.evaluate()
                self.assertEqual(report["recommendation"], "defer")
                primary = report["comparison"]["scenarios"]["offline"]["primary"]
                self.assertEqual(len(primary["observed_violations"]), 2)
        floor["min_value"] = 1
        contract["goodput_policy"]["scenarios"]["offline"]["token_source"] = "server_content_tokens"
        save(self.contract, contract)
        report = self.evaluate()
        self.assertEqual(report["recommendation"], "defer")
        primary = report["comparison"]["scenarios"]["offline"]["primary"]
        self.assertEqual(primary["n_defined_pairs"], 0)
        self.assertEqual(len(primary["missing_or_undefined"]), 2)

    def test_absolute_goodput_contract_rejects_ambiguous_targets(self):
        contract = self.goodput_contract()
        for extra in ({"direction": "lower"}, {"min_value": 0}, {"min_value": -1},
                      {"metric": "request_throughput"}, {"min_relative_improvement": 0.01}):
            with self.subTest(extra=extra):
                contract["scenarios"]["offline"]["primary"] = {
                    "metric": "goodput", "direction": "higher", "min_value": 1, **extra}
                save(self.contract, contract)
                with self.assertRaises(ValueError):
                    gate.validate_policy(self.contract)

    def test_initial_absolute_goodput_target_still_applies_when_raw_goal_is_met(self):
        from scripts.campaign import initial_goal

        contract = self.goodput_contract()
        contract["scenarios"]["offline"]["primary"] = {"metric": "goodput", "direction": "higher", "min_value": 20}
        runs = [{"scenario": "offline", "metrics": {"output_token_throughput": 50, "goodput": g, "failure_rate": 0}}
                for g in (20, 0)]
        self.assertFalse(initial_goal(runs, contract, "offline", 50))
        runs[1]["metrics"]["goodput"] = 20
        self.assertTrue(initial_goal(runs, contract, "offline", 50))

    def test_goodput_policy_is_required_and_validated_per_scenario(self):
        valid = self.goodput_contract()
        gate.validate_policy(self.contract)
        for policy in (None, {"scenarios": {}}, {"scenarios": {"elsewhere": {"max_ttft_s": 1, "max_e2e_s": 2}}},
                       {"scenarios": {"offline": {"max_ttft_s": 1}}},
                       {"scenarios": {"offline": {"max_ttft_s": -1, "max_e2e_s": 2}}},
                       {"scenarios": {"offline": {"max_ttft_s": True, "max_e2e_s": 2}}},
                       {"scenarios": {"offline": {"max_ttft_s": 1, "max_e2e_s": 2, "min_request_score": .5}}}):
            with self.subTest(policy=policy):
                contract = {**valid, "goodput_policy": policy}
                if policy is None:
                    contract.pop("goodput_policy")
                save(self.contract, contract)
                with self.assertRaises(ValueError):
                    gate.validate_policy(self.contract)

    def test_tampered_raw_output_and_usage(self):
        self.build()
        run = self.runs["candidate1"]
        raw = run / "raw/00000000.output.json"
        original = raw.read_bytes()
        save(raw, {"content": "invented", "reasoning_content": "thought"})
        with self.assertRaisesRegex(ValueError, "retained SSE"):
            self.evaluate()
        raw.write_bytes(original)
        req = run / "requests.jsonl"
        row = load(req)
        row["actual_output_tokens"] += 1
        save(req, row)
        with self.assertRaisesRegex(ValueError, "actual_output_tokens"):
            self.evaluate()

    def test_undeployed_identity_and_mutated_sealed_artifacts_rejected(self):
        self.build()
        meta = load(self.runs["candidate1"] / "metadata.json")
        ref = Path(meta["config_evidence_artifacts"][0]["path"])
        with self.assertRaisesRegex(ValueError, "config differs"):
            gate.check_observation(ref, self.identities[0])
        with self.assertRaisesRegex(ValueError, "endpoint mismatch"):
            gate.check_observation(ref, self.identities[1], base_url="http://other")
        ref.write_text("{}")
        with self.assertRaisesRegex(ValueError, "file hash differs"):
            self.evaluate()

    def test_frozen_gold_rechecked_and_missing_samples_block_adoption(self):
        self.build()
        save(self.ref, {"r0": "unsupported"})
        with self.assertRaisesRegex(ValueError, "file hash differs"):
            self.evaluate()
        contract = load(self.contract)
        contract["quality_policy"]["scenarios"]["offline"]["reference"]["sha256"] = gate.file_hash(self.ref)
        save(self.contract, contract)
        result = self.evaluate()
        self.assertNotEqual(result["recommendation"], "adopt")
        self.assertFalse(any(q["passed"] for q in result["quality"].values()))

    def test_source_identity_enables_native_comparison_but_swapped_sides_fail(self):
        self.build(source_change=True)
        result = self.evaluate()
        self.assertEqual(result["recommendation"], "adopt")
        self.assertEqual(result["comparison"]["contract"]["implementation_change"], {
            side + "_sha256": identity["implementation_sha256"]
            for side, identity in zip(("baseline", "candidate"), self.identities)})
        self.identities.reverse()
        with self.assertRaisesRegex(ValueError, "another scenario or deployment"):
            self.evaluate()

    def test_configuration_change_on_a_fixed_verified_source(self):
        self.build(fixed_source=True)
        result = self.evaluate()
        self.assertEqual(result["recommendation"], "adopt")
        self.assertEqual(result["comparison"]["fixed_implementation_sha256"],
                         self.identities[0]["implementation_sha256"])
        self.assertEqual(result["runs"][1]["identity"], self.identities[1])
        self.assertNotIn("implementation_change", result["comparison"]["contract"])

    def test_partial_quality_scores_keep_failed_requests_in_denominator(self):
        _, run, _, validated, _ = self.fixture.collect(quality=None, modes=("normal", "broken_stream", "missing_usage"))
        gold = {"r0": "several tokens together", "r1": "", "r2": ""}
        policy = {**self.rule, "method": "exact_match_rate", "min_fraction": .3}
        result = gate.score_run(run.resolve(), validated, (policy, .3, gold), {})
        self.assertTrue(result["passed"])
        self.assertEqual(result["fraction"], 1 / 3)
        self.assertEqual(set(result["mismatch_or_failure_ids"]), {"r1", "r2"})
        self.assertFalse(gate.score_run(run.resolve(), validated, (policy, .4, gold), {})["passed"])
        self.assertGreater(validated["metrics"]["failure_rate"], .6)

    def test_all_failed_requests_never_pass_even_when_saved_outputs_match(self):
        _, run, _, checked, _ = self.fixture.collect(quality=None, modes=("broken_stream", "missing_usage"))
        work = load(run / "workload.json")
        gold = {item["request_id"]: load(run / ("raw/%08d.output.json" % i))["content"]
                for i, item in enumerate(work)}
        result = gate.score_run(run.resolve(), checked, (self.rule, .01, gold), {})
        self.assertEqual((checked["metrics"]["effective_output_tokens"], result["fraction"]), (0, 0))
        self.assertFalse(result["passed"])

    def test_saved_error_or_missing_terminal_frame_cannot_be_scored_as_success(self):
        self.build()
        sse = self.runs["candidate1"] / "raw/00000000.sse"
        original = sse.read_bytes()
        marker = b"data: [DONE]"
        self.assertIn(marker, original)
        for contents, message in [
            (original + b'data: {"error":{"message":"failure"}}\n\n', "after recorded DONE"),
            (original.replace(marker, b'data: {"error":{}}'), "server_error_event"),
            (original[:original.index(marker)], "stream_ended_without_done"),
        ]:
            with self.subTest(message=message):
                sse.write_bytes(contents)
                with self.assertRaisesRegex(ValueError, message):
                    self.evaluate()

    def python_policy(self, program, reference, threshold=1.0, timeout=5.0):
        script = self.root / "quality.py"
        script.write_text(program, encoding="utf-8")
        save(self.ref, reference)
        contract = load(self.contract)
        policy = {"method": "python_score_v1", "min_mean_score": threshold, "timeout_s": timeout,
                  "scorer": {"path": script.name, "sha256": gate.file_hash(script)},
                  "reference": {"path": self.ref.name, "sha256": gate.file_hash(self.ref)}}
        contract["quality_policy"]["scenarios"]["offline"] = policy
        save(self.contract, contract)
        return gate.quality_rules(self.contract, contract, {})["offline"]

    SCORER = """import json, sys
items = json.load(sys.stdin)["items"]
scores = {}
for item in items:
    if item["status"] == "success":
        score = int(item["output"]["content"].lower() in item["reference"]["accepted"])
    else:
        assert item["output"] is None
        score = 0
    scores[item["request_id"]] = score
print(json.dumps({"schema_version": 1, "scores": scores}))
"""

    def test_pinned_python_scorer_passes_alternatives_and_records_inputs(self):
        self.build()
        reference = {"r0": {"accepted": ["another correct phrasing", "several tokens together"]}}
        self.python_policy(self.SCORER, reference)
        result = self.evaluate()
        self.assertEqual(result["recommendation"], "adopt")
        for quality in result["quality"].values():
            self.assertTrue(quality["passed"])
            self.assertEqual(quality["scores"], {"r0": 1.0})
            self.assertEqual(quality["mean_score"], 1.0)
            self.assertEqual(quality["scorer"]["sha256"], gate.file_hash(self.root / "quality.py"))
            self.assertEqual(quality["python"]["sha256"], gate.file_hash(sys.executable))
            item = {"request_id": "r0", "status": "success", "prompt_text": "normal", "reference": reference["r0"],
                    "output": {"content": "several tokens together", "reasoning_content": "thought"}}
            self.assertEqual(quality["scorer_input_sha256"],
                             gate.validate_run.canonical_hash({"schema_version": 1, "items": [item]}))
            self.assertEqual(quality["item_sha256"]["r0"], gate.validate_run.canonical_hash(item))
        self.python_policy(self.SCORER, {"r0": {"accepted": ["different answer"]}})
        result = self.evaluate()
        self.assertNotEqual(result["recommendation"], "adopt")
        self.assertFalse(any(q["passed"] for q in result["quality"].values()))

    def test_python_goodput_uses_individual_score_threshold(self):
        self.build()
        self.python_policy('print(\'{"schema_version":1,"scores":{"r0":0.75}}\')', {"r0": "reference"}, .5)
        self.goodput_contract()
        with self.assertRaises(ValueError):
            gate.validate_policy(self.contract)
        for score, tokens, recommendation in ((.75, 7, "adopt"), (.76, 0, "defer")):
            with self.subTest(score=score):
                self.goodput_contract(min_request_score=score)
                result = self.evaluate()
                self.assertEqual(result["recommendation"], recommendation)
                self.assertTrue(all(q["passed"] for q in result["quality"].values()))
                for measured in result["goodput"].values():
                    self.assertEqual(measured["qualifying_output_tokens"], tokens)
                    self.assertEqual(measured["excluded_request_ids"]["quality"], [] if tokens else ["r0"])

    def test_scorer_failure_credit_coverage_timeout_and_code_identity(self):
        _, run, _, checked, _ = self.fixture.collect(quality=None, modes=("normal", "broken_stream"))
        ref = {k: {"accepted": ["several tokens together"]} for k in ("r0", "r1")}
        rule = self.python_policy(self.SCORER, ref, .5)
        result = gate.score_run(run, checked, rule, {})
        self.assertTrue(result["passed"])
        self.assertEqual(result["scores"], {"r0": 1.0, "r1": 0.0})
        (self.root / "quality.py").write_text("raise RuntimeError()")
        with self.assertRaisesRegex(ValueError, "scorer source changed"):
            gate.score_run(run, checked, rule, {})
        with self.assertRaisesRegex(ValueError, "file hash differs"):
            gate.validate_policy(self.contract)
        for program, error, limit in [
            ("raise RuntimeError('fixture')", "exited with code", 5),
            ("import time; time.sleep(5)", "timed out", .05),
            ('print(\'{"schema_version":1,"scores":{"r0":1}}\')', "result IDs", 5),
            ('print(\'{"schema_version":1,"scores":{"r0":1,"r1":1}}\')', "credit to a failed", 5),
            ('print(\'{"schema_version":1,"scores":{"r0":2,"r1":0}}\')', "must be in", 5),
        ]:
            with self.subTest(error=error):
                rule = self.python_policy(program, ref, .5, limit)
                with self.assertRaisesRegex(ValueError, error):
                    gate.score_run(run, checked, rule, {})

    def test_shipped_example_matches_workload_and_pinned_gold(self):
        contract = ROOT / "assets/round-exact.example.json"
        pinned, = gate.validate_policy(contract)
        gold = load(Path(pinned["path"]))
        rows = (ROOT / "assets/round-exact.workload.jsonl").read_text().splitlines()
        self.assertEqual(set(gold), {json.loads(r)["request_id"] for r in rows})
        self.assertEqual(set(gold.values()), {"5", "28", "9"})

    def test_disallow_reused_run_and_prevalidated_file_substitution(self):
        self.build()
        trial = load(self.evidence)
        trial["pairs"][0]["candidate"] = trial["pairs"][1]["candidate"]
        save(self.evidence, trial)
        with self.assertRaisesRegex(ValueError, "run_id reused"):
            self.evaluate()
        trial["pairs"][0]["candidate"] += "/validated.json"
        save(self.evidence, trial)
        with self.assertRaisesRegex(ValueError, "original run directory"):
            self.evaluate()

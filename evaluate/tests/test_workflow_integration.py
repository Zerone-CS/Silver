"""Offline integration: temporary artifacts and a loopback-only HTTP server.

Real client timings are never used to assert an optimization gain. Positive
and negative performance decisions use explicitly synthetic timestamp fixtures
that still pass through the real raw-run validator before comparison.
"""

import contextlib
import copy
import importlib.util
import hashlib
import io
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Load by path so both unittest discovery and direct execution work without
# requiring tests to be an installed package.
mock_fixture = load_module("workflow_mock_fixture", ROOT / "tests/test_benchmark.py")
benchmark = load_module("workflow_benchmark", ROOT / "scripts/benchmark.py")
prepare = load_module("workflow_prepare", ROOT / "scripts/prepare_metadata.py")
validator = load_module("workflow_validator", ROOT / "scripts/validate_run.py")
comparison = load_module("workflow_comparison", ROOT / "scripts/compare_trials.py")


class WorkflowIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), mock_fixture.MockHandler)
        cls.server.daemon_threads = True
        cls.server.lock = threading.Lock()
        cls.server.received = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/v1" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.counter = 0

    def write_json(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def invoke(self, main, argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(argv)

    def collect(self, purpose="performance", quality="pass", modes=("normal",), implementation=None, verified=True):
        """Exercise prepare, the real loopback collector, and validator CLIs."""
        self.counter += 1
        directory = self.root / ("collection-%02d" % self.counter)
        directory.mkdir()
        inputs = {}
        for name in ("config", "environment", "metric-contract"):
            inputs[name] = self.write_json(directory / (name + ".json"),
                                          {"fixture": "offline workflow", "kind": name})
        evidence_log = directory / "effective-config.txt"
        evidence_log.write_text("Synthetic config evidence for loopback test only.\n", encoding="utf-8")
        config_evidence = self.write_json(directory / "config-evidence.json", {
            "verified": True, "scope": "loopback fixture only",
            "evidence": [evidence_log.name],
        })
        metadata_path = directory / "metadata-input.json"
        argv = ["--scenario", "offline", "--profiler-enabled", "off",
                "--run-purpose", purpose, "--config-evidence", str(config_evidence),
                "--output", str(metadata_path)]
        for name, path in inputs.items():
            argv += ["--" + name, str(path)]
        if implementation is not None:
            identity = {"source_sha256": hashlib.sha256(implementation.encode()).hexdigest()}
            identity_path = self.write_json(directory / "implementation.json", identity)
            identity_sha = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            argv += ["--implementation", str(identity_path)]
            if verified:
                probe = directory / "loaded-source.txt"
                probe.write_text(implementation)
                evidence_path = self.write_json(directory / "implementation-evidence.json", {
                    "implementation_sha256": identity_sha, "verified": True, "scope": "loopback fixture",
                    "evidence": [probe.name],
                })
                argv += ["--implementation-evidence", str(evidence_path)]
        if quality is not None:
            quality_report = {
                "status": quality, "scope": "loopback fixture only; no real-model quality claim",
                "method": "explicit synthetic test declaration",
            }
            if implementation is not None:
                quality_report["implementation_sha256"] = identity_sha
            quality_path = self.write_json(directory / "quality-fixture.json", quality_report)
            argv += ["--quality-report", str(quality_path)]
        self.assertEqual(self.invoke(prepare.main, argv), 0)
        workload = [{"request_id": "r%d" % index, "prompt_text": mode,
                     "output_tokens": 64} for index, mode in enumerate(modes)]
        dataset = directory / "dataset.jsonl"
        dataset.write_text("".join(json.dumps(item) + "\n" for item in workload), encoding="utf-8")
        run_dir = directory / "run"
        benchmark_status = self.invoke(benchmark.main, [
            "--base-url", self.url, "--model", "offline-mock", "--dataset", str(dataset),
            "--metadata", str(metadata_path), "--run-dir", str(run_dir),
            "--concurrency", "2", "--workers", "2", "--timeout", "2",
        ])
        validated_path = directory / "validated.json"
        self.assertEqual(self.invoke(validator.main, [
            "--run-dir", str(run_dir), "--output", str(validated_path),
        ]), 0)
        report = json.loads(validated_path.read_text(encoding="utf-8"))
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual((run_dir / "metadata.json").read_bytes(), metadata_path.read_bytes())
        self.assertEqual(json.loads((run_dir / "workload.json").read_text(encoding="utf-8")), workload)
        self.assertEqual(report["manifest"]["run_purpose"], purpose)
        for name in ("manifest.json", "requests.jsonl", "metadata.json", "workload.json"):
            self.assertTrue((run_dir / name).is_file())
            self.assertIn(name, report["source_hashes"])
        records = [json.loads(line) for line in
                   (run_dir / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
        return benchmark_status, run_dir, validated_path, report, records

    def compare_paths(self, pairs, label, change=None):
        contract = {
            "schema_version": 1, "min_pairs": 2, "confidence": 0.95,
            "bootstrap_samples": 100, "seed": 7,
            "scenarios": {"offline": {
                "primary": {"metric": "output_token_throughput", "direction": "higher",
                            "min_relative_improvement": 0.02},
                "guardrails": [{"metric": "failure_rate", "max_value": 0.0}],
            }},
        }
        if change is not None:
            contract["implementation_change"] = change
        contract_path = self.write_json(self.root / (label + ".contract.json"), contract)
        trial_path = self.write_json(self.root / (label + ".trial.json"), {
            "schema_version": 1,
            "pairs": [{"scenario": "offline", "baseline": str(base), "candidate": str(candidate)}
                      for base, candidate in pairs],
        })
        output = self.root / (label + ".comparison.json")
        status = self.invoke(comparison.main, [
            "--contract", str(contract_path), "--trial", str(trial_path), "--output", str(output),
        ])
        return status, json.loads(output.read_text(encoding="utf-8"))

    def test_real_chain_keeps_failed_time_and_excludes_partial_output_tokens(self):
        status, _, _, report, records = self.collect(modes=("normal", "broken_stream", "missing_usage"))
        self.assertEqual(status, 1)
        metrics = report["metrics"]
        self.assertEqual((metrics["n_requests"], metrics["n_success"], metrics["n_failed"]), (3, 1, 2))
        self.assertEqual(metrics["effective_output_tokens"], 7)
        by_id = {item["request_id"]: item for item in records}
        self.assertEqual(by_id["r1"]["actual_output_tokens"], 7)
        self.assertEqual(by_id["r1"]["final_status"], "error")
        self.assertFalse(by_id["r1"]["stream_done_received"])
        self.assertIsNone(by_id["r2"]["actual_output_tokens"])
        self.assertFalse(by_id["r2"]["final_usage_received"])
        wall = report["manifest"]["end_ts"] - report["manifest"]["start_ts"]
        self.assertEqual(metrics["wall_s"], wall)
        self.assertAlmostEqual(metrics["output_token_throughput"], 7 / wall)
        all_residence = sum(item["end_ts"] - item["send_start_ts"] for item in records)
        self.assertAlmostEqual(metrics["summed_request_residence_s"], all_residence)
        self.assertAlmostEqual(metrics["in_flight_integral_request_s"], all_residence)
        self.assertGreater(all_residence, by_id["r0"]["end_ts"] - by_id["r0"]["send_start_ts"])

    def assert_real_gate_blocks(self, purpose, quality, expected_issue, label):
        pairs = []
        for _ in range(2):
            baseline = self.collect()
            candidate = self.collect(purpose=purpose, quality=quality)
            self.assertEqual((baseline[0], candidate[0]), (0, 0))
            pairs.append((baseline[2], candidate[2]))
        status, report = self.compare_paths(pairs, label)
        self.assertEqual(status, 1)
        self.assertTrue(report["input_valid"])
        self.assertEqual(report["recommendation"], "defer")
        for pair in report["pairs"]:
            self.assertIn(expected_issue, pair["eligibility_issues"])
        self.assertEqual(report["scenarios"]["offline"]["primary"]["n_defined_pairs"], 0)

    def test_real_chain_diagnostic_runs_cannot_promote(self):
        self.assert_real_gate_blocks("diagnostic", "pass", "candidate: run_purpose=diagnostic", "diagnostic")

    def test_real_chain_unevaluated_quality_cannot_promote(self):
        self.assert_real_gate_blocks("performance", None, "candidate: quality_status=not_evaluated", "quality")

    def test_real_chain_rejects_contradictory_success_completion_flags(self):
        _, run_dir, _, _, records = self.collect()
        for flag in ("stream_done_received", "final_usage_received"):
            with self.subTest(flag=flag):
                contradictory = copy.deepcopy(records)
                contradictory[0][flag] = False
                (run_dir / "requests.jsonl").write_text(
                    "".join(json.dumps(item) + "\n" for item in contradictory), encoding="utf-8")
                report = validator.inspect_run(run_dir)
                self.assertFalse(report["valid"])
                self.assertEqual(report["metrics"], {})
                self.assertTrue(any(flag in error for error in report["errors"]))

    def synthetic_raw(self, template_dir, name, wall, failed=False):
        """Independent synthetic raw records, explicitly not measured repeats."""
        directory = self.root / name
        directory.mkdir()
        for filename in ("metadata.json", "workload.json"):
            (directory / filename).write_bytes((template_dir / filename).read_bytes())
        manifest = json.loads((template_dir / "manifest.json").read_text(encoding="utf-8"))
        record = json.loads((template_dir / "requests.jsonl").read_text(encoding="utf-8"))
        run_id = "synthetic-test-" + str(uuid.uuid4())
        manifest.update(run_id=run_id, start_ts=100.0, end_ts=100.0 + wall,
                        offline_fixture="SYNTHETIC timestamps; never a real performance result")
        record.update(run_id=run_id, planned_send_ts=100.0, send_start_ts=100.0,
                      response_open_ts=100.0 + wall * 0.1, first_output_ts=100.0 + wall * 0.2,
                      last_output_ts=100.0 + wall * 0.8, end_ts=100.0 + wall,
                      finish_reason_ts=100.0 + wall * 0.85, final_usage_ts=100.0 + wall * 0.9,
                      stream_done_ts=100.0 + wall * 0.95,
                      output_event_ts=[100.0 + wall * 0.2, 100.0 + wall * 0.5, 100.0 + wall * 0.8])
        if failed:
            record.update(final_status="error", error="synthetic_stream_ended_without_done",
                          stream_done_received=False, stream_done_ts=None)
        self.write_json(directory / "manifest.json", manifest)
        (directory / "requests.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        output = directory / "validated.json"
        self.assertEqual(self.invoke(validator.main, ["--run-dir", str(directory), "--output", str(output)]), 0)
        return output

    def synthetic_comparison(self, fail_candidate=False):
        _, template, _, _, _ = self.collect()
        pairs = []
        for index in range(2):
            base = self.synthetic_raw(template, "synthetic-b%d" % index, 10.0)
            candidate = self.synthetic_raw(template, "synthetic-c%d" % index, 5.0,
                                           failed=fail_candidate and index == 0)
            pairs.append((base, candidate))
        return self.compare_paths(pairs, "synthetic")

    def test_implementation_ab_runs_through_prepare_collect_validate_and_compare(self):
        base = self.collect(implementation="base source")
        candidate = self.collect(implementation="changed source")
        change = {side + "_sha256": run[3]["manifest"]["implementation_sha256"]
                  for side, run in (("baseline", base), ("candidate", candidate))}
        for side, run in (("baseline", base), ("candidate", candidate)):
            m = run[3]["manifest"]
            self.assertTrue(m["implementation_verified"])
            self.assertEqual(m["quality_implementation_sha256"], change[side + "_sha256"])
            artifact = m["implementation_evidence"]["artifacts"][0]
            self.assertEqual(artifact["sha256"], hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest())
        pairs = [(self.synthetic_raw(base[1], "base-" + str(n), 10),
                  self.synthetic_raw(candidate[1], "candidate-" + str(n), 5)) for n in range(2)]
        code, report = self.compare_paths(pairs, "implementation", change)
        self.assertEqual((code, report["recommendation"]), (0, "adopt"), report)
        code, report = self.compare_paths(pairs, "undeclared")
        self.assertEqual((code, report["input_valid"]), (2, False))
        code, report = self.compare_paths([(b, a) for a, b in pairs], "swapped", change)
        self.assertEqual((code, report["input_valid"]), (2, False))

    def test_missing_implementation_evidence_or_quality_cannot_promote(self):
        base = self.collect(implementation="base")
        for label, opts, reason in (("unknown_impl", {"verified": False}, "implementation has not been verified"),
                                    ("unknown_quality", {"quality": None}, "quality_status=not_evaluated")):
            candidate = self.collect(implementation="candidate", **opts)
            change = {"baseline_sha256": base[3]["manifest"]["implementation_sha256"],
                      "candidate_sha256": candidate[3]["manifest"]["implementation_sha256"]}
            pairs = [(self.synthetic_raw(base[1], label + "-base-" + str(n), 10),
                      self.synthetic_raw(candidate[1], label + "-candidate-" + str(n), 5)) for n in range(2)]
            code, report = self.compare_paths(pairs, label, change)
            self.assertEqual((code, report["recommendation"], report["input_valid"]), (1, "defer", True))
            self.assertIn(reason, " ".join(report["pairs"][0]["eligibility_issues"]))

    def test_collector_source_map_and_digest_must_agree(self):
        _, run_dir, _, report, _ = self.collect()
        original = report["manifest"]
        for mutate in (lambda m: m["benchmark_source_hashes"].update({"implementation_identity.py": "0" * 64}),
                       lambda m: m.pop("benchmark_hash_algorithm"), lambda m: m.pop("benchmark_source_hashes")):
            changed = copy.deepcopy(original)
            mutate(changed)
            self.write_json(run_dir / "manifest.json", changed)
            invalid = validator.inspect_run(run_dir)
            self.assertFalse(invalid["valid"])
            self.assertTrue(any("benchmark_" in message for message in invalid["errors"]))

    def test_implementation_metadata_and_manifest_must_match_including_field_presence(self):
        _, run_dir, _, report, _ = self.collect(implementation="base")
        original = report["manifest"]
        for key in ("implementation_verified", "implementation_evidence", "quality_implementation_sha256"):
            with self.subTest(key=key):
                changed = dict(original)
                del changed[key]
                self.write_json(run_dir / "manifest.json", changed)
                actual = validator.inspect_run(run_dir)
                self.assertFalse(actual["valid"])
                self.assertFalse(actual["metrics"])

    def test_synthetic_raw_runs_validate_and_eligible_effect_can_promote(self):
        status, report = self.synthetic_comparison()
        self.assertEqual(status, 0)
        self.assertEqual(report["recommendation"], "adopt")
        self.assertEqual(report["mechanism_status"], "unresolved")
        self.assertEqual(report["scenarios"]["offline"]["primary"]["mean_relative_improvement"], 1.0)

    def test_synthetic_failed_run_is_rejected_by_zero_failure_guardrail(self):
        status, report = self.synthetic_comparison(fail_candidate=True)
        self.assertEqual(status, 1)
        self.assertEqual(report["recommendation"], "revert")
        guardrail = report["scenarios"]["offline"]["guardrails"][0]
        self.assertEqual(guardrail["status"], "violated")
        self.assertEqual(guardrail["observed_violations"][0]["candidate_value"], 1.0)


if __name__ == "__main__":
    unittest.main()

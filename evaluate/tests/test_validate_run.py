import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_run.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("validate_run", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def manifest():
    policy = {"concurrency": 1, "timeout_s": 20, "stream": True}
    return {
        "schema_version": 1, "run_id": "run-1", "scenario": "single",
        "measurement_mode": "finite_batch", "start_ts": 0.0, "end_ts": 10.0,
        "run_purpose": "performance",
        "clock": "client_perf_counter", "profiler_enabled": False,
        "workload_sha256": "a" * 64, "config_sha256": "b" * 64,
        "environment_sha256": "c" * 64, "metric_contract_sha256": "d" * 64,
        "benchmark_sha256": "e" * 64, "quality_status": "pass",
        "effective_config_verified": True, "client_measurement_policy": policy,
        "client_measurement_policy_sha256": MODULE.canonical_hash(policy),
        "metadata_sha256": "f" * 64,
    }


def request():
    return {
        "run_id": "run-1", "request_id": "r1", "planned_send_ts": 0.0,
        "send_start_ts": 0.0, "response_open_ts": 1.0,
        "first_output_ts": 2.0, "last_output_ts": 9.0, "end_ts": 10.0,
        "final_status": "success", "finish_reason": "length",
        "requested_max_tokens": 20, "actual_output_tokens": 16,
        "actual_prompt_tokens": 32, "token_count_source": "server_usage",
        "output_event_ts": [2.0, 9.0], "error": None,
        "stream_done_received": True, "final_usage_received": True,
    }


class ValidateRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / "run"
        self.run.mkdir()

    def write(self, data=None, records=None):
        records = [request()] if records is None else records
        workload = [{"request_id": item.get("request_id", "r%d" % index),
                     "prompt_text": "hello", "output_tokens": item.get("requested_max_tokens", 20)}
                    for index, item in enumerate(records)]
        supplied = data is not None
        data = dict(manifest() if data is None else data)
        metadata = {key: data[key] for key in
                    ("scenario", "config_sha256", "environment_sha256", "metric_contract_sha256",
                     "quality_status", "effective_config_verified", "profiler_enabled", "run_purpose") if key in data}
        raw_metadata = json.dumps(metadata).encode()
        if not supplied or data.get("metadata_sha256") == "f" * 64:
            data["metadata_sha256"] = hashlib.sha256(raw_metadata).hexdigest()
        if not supplied or data.get("workload_sha256") == "a" * 64:
            data["workload_sha256"] = MODULE.canonical_hash(workload)
        (self.run / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        (self.run / "workload.json").write_text(json.dumps(workload), encoding="utf-8")
        (self.run / "metadata.json").write_bytes(raw_metadata)
        (self.run / "requests.jsonl").write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    def cli(self, output=None):
        return subprocess.run([sys.executable, str(SCRIPT), "--run-dir", str(self.run),
                               "--output", str(output or self.root / "report.json")],
                              capture_output=True, text=True)

    def test_full_window_and_single_request_inflight(self):
        self.write()
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.root / "report.json").read_text())
        metrics = report["metrics"]
        self.assertEqual(metrics["observed_mean_in_flight"], 1.0)
        self.assertEqual(metrics["in_flight_integral_request_s"], 10.0)
        self.assertEqual(metrics["summed_request_residence_s"], 10.0)
        self.assertEqual(metrics["output_token_throughput"], 1.6)
        self.assertEqual(metrics["ttft_p99_s"], 2.0)
        self.assertEqual(metrics["e2e_p99_s"], 10.0)
        self.assertEqual(report["source_hashes"]["requests.jsonl"],
                         hashlib.sha256((self.run / "requests.jsonl").read_bytes()).hexdigest())

    def test_optional_stream_timing_cannot_be_partial_or_out_of_order_in_valid_records(self):
        valid = dict(request(), finish_reason_ts=9.0, final_usage_ts=9.1, stream_done_ts=9.5)
        errors, _, metrics = MODULE.validate(manifest(), [valid])
        self.assertFalse(errors)
        self.assertEqual(metrics["e2e_p99_s"], 10.0)
        for change, expected in (
            ({"final_usage_ts": None}, "match the observed stream"),
            ({"stream_done_ts": 13.0}, "inside response lifetime"),
            ({"stream_done_ts": 9.0}, "nondecreasing"),
            ({"finish_reason_ts": 8.5}, "follow the last nonempty"),
        ):
            with self.subTest(change=change):
                errors, _, _ = MODULE.validate(manifest(), [dict(valid, **change)])
                self.assertIn(expected, str(errors))

    def test_failed_partial_output_keeps_time_but_not_tokens(self):
        first = request()
        first.update(end_ts=5.0, last_output_ts=4.0, output_event_ts=[2.0, 4.0])
        failed = request()
        failed.update(request_id="r2", planned_send_ts=5.0, send_start_ts=5.0,
                      response_open_ts=6.0, first_output_ts=7.0, last_output_ts=9.0,
                      output_event_ts=[7.0, 9.0], actual_output_tokens=8,
                      final_status="timeout", finish_reason=None, error="read timeout",
                      stream_done_received=False)
        self.write(records=[first, failed])
        report = MODULE.inspect_run(self.run)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["metrics"]["effective_output_tokens"], 16)
        self.assertEqual(report["metrics"]["wall_s"], 10)
        self.assertEqual(report["metrics"]["failure_rate"], 0.5)
        self.assertEqual(report["metrics"]["e2e_p99_s"], 5)
        self.assertEqual(report["metrics"]["observed_mean_in_flight"], 1)

    def test_overlapping_requests_integrate_time_weighted_occupancy(self):
        second = request()
        second.update(request_id="r2", planned_send_ts=1.0, send_start_ts=1.0)
        self.write(records=[request(), second])
        report = MODULE.inspect_run(self.run)
        self.assertTrue(report["valid"], report["errors"])
        self.assertAlmostEqual(report["metrics"]["observed_mean_in_flight"], 1.9)
        self.assertEqual(report["metrics"]["e2e_p50_s"], 9.5)
        self.assertAlmostEqual(report["metrics"]["e2e_p99_s"], 9.99)

    def test_empty_successful_stream_with_zero_usage_has_no_ttft(self):
        zero = request()
        zero.update(actual_output_tokens=0, first_output_ts=None,
                    last_output_ts=None, output_event_ts=[], finish_reason="stop")
        self.write(records=[zero])
        report = MODULE.inspect_run(self.run)
        self.assertTrue(report["valid"], report["errors"])
        self.assertIsNone(report["metrics"]["ttft_p99_s"])
        self.assertEqual(report["metrics"]["output_token_throughput"], 0)
        self.assertEqual(report["metrics"]["e2e_sample_count"], 1)

    def test_invalid_request_values(self):
        updates = [
            {"actual_output_tokens": None}, {"actual_output_tokens": True},
            {"actual_output_tokens": 21}, {"actual_prompt_tokens": -1},
            {"send_start_ts": True}, {"end_ts": 11}, {"end_ts": float("nan")},
            {"output_event_ts": [9, 2]}, {"token_count_source": "requested_max_tokens"},
            {"finish_reason": None}, {"finish_reason": "tool_calls"},
            {"first_output_ts": None, "last_output_ts": None, "output_event_ts": []},
            {"actual_output_tokens": 0}, {"final_status": "timeout", "error": None},
            {"stream_done_received": False}, {"final_usage_received": False},
            {"stream_done_received": 1}, {"final_usage_received": None},
        ]
        for update in updates:
            with self.subTest(update=update):
                record = request()
                record.update(update)
                errors, _, metrics = MODULE.validate(manifest(), [record])
                self.assertTrue(errors)
                self.assertEqual(metrics, {})

    def test_failed_missing_counts_is_valid_and_does_not_invent_output(self):
        failed = request()
        failed.update(final_status="error", error="connection failed", finish_reason=None,
                      response_open_ts=None, first_output_ts=None, last_output_ts=None,
                      output_event_ts=[], actual_output_tokens=None,
                      actual_prompt_tokens=None, token_count_source=None,
                      stream_done_received=False, final_usage_received=False)
        errors, _, metrics = MODULE.validate(manifest(), [failed])
        self.assertFalse(errors)
        self.assertEqual(metrics["effective_output_tokens"], 0)
        self.assertEqual(metrics["failure_rate"], 1)
        self.assertIsNone(metrics["e2e_p99_s"])

    def test_manifest_validation(self):
        updates = [{"schema_version": True}, {"end_ts": float("inf")},
                   {"profiler_enabled": 0}, {"config_sha256": "bad"},
                   {"client_measurement_policy_sha256": "a" * 64},
                   {"client_measurement_policy": {}}, {"quality_status": "unknown"},
                   {"measurement_mode": "legacy"}, {"n_requests": 2},
                   {"run_purpose": "unknown"}, {"run_purpose": True}]
        for update in updates:
            with self.subTest(update=update):
                data = manifest()
                data.update(update)
                self.assertTrue(MODULE.validate(data, [request()])[0])

    def test_duplicate_ids_and_run_mismatch(self):
        self.assertTrue(MODULE.validate(manifest(), [request(), request()])[0])
        record = request()
        record["run_id"] = "other"
        self.assertTrue(MODULE.validate(manifest(), [record])[0])

    def test_legacy_quota_record_is_rejected(self):
        self.write(data={"throughput": 100}, records=[{"output_tokens": 16}])
        result = self.cli()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(json.loads((self.root / "report.json").read_text())["valid"])

    def test_nan_invalid_report_remains_standard_json(self):
        data = manifest()
        data["end_ts"] = float("nan")
        self.write(data=data)
        result = self.cli()
        self.assertEqual(result.returncode, 1, result.stderr)
        text = (self.root / "report.json").read_text()
        self.assertNotIn("NaN", text)
        self.assertFalse(json.loads(text)["valid"])

    def test_workload_and_metadata_hashes_are_recomputed(self):
        workload = [{"request_id": "r1", "prompt_text": "你好", "output_tokens": 20}]
        data = manifest()
        metadata = json.dumps({key: data[key] for key in
                               ("scenario", "config_sha256", "environment_sha256",
                                "metric_contract_sha256", "quality_status",
                                "effective_config_verified", "profiler_enabled", "run_purpose")}).encode()
        data["workload_sha256"] = MODULE.canonical_hash(workload)
        data["metadata_sha256"] = hashlib.sha256(metadata).hexdigest()
        self.write(data=data)
        (self.run / "workload.json").write_text(json.dumps(workload, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.run / "metadata.json").write_bytes(metadata)
        self.assertTrue(MODULE.inspect_run(self.run)["valid"])
        (self.run / "metadata.json").write_bytes(b'{"scenario":"different"}')
        self.assertFalse(MODULE.inspect_run(self.run)["valid"])

    def test_metadata_profiler_declaration_cannot_change_silently(self):
        data = manifest()
        metadata = {key: data[key] for key in
                    ("scenario", "config_sha256", "environment_sha256", "metric_contract_sha256",
                     "quality_status", "effective_config_verified", "profiler_enabled", "run_purpose")}
        metadata["profiler_enabled"] = True
        raw = json.dumps(metadata).encode()
        data["metadata_sha256"] = hashlib.sha256(raw).hexdigest()
        self.write(data=data)
        (self.run / "metadata.json").write_bytes(raw)
        report = MODULE.inspect_run(self.run)
        self.assertFalse(report["valid"])
        self.assertTrue(any("profiler_enabled" in error for error in report["errors"]))

    def test_protocol_flags_required_even_for_failed_records(self):
        for flag in ("stream_done_received", "final_usage_received"):
            for status in ("success", "error"):
                with self.subTest(flag=flag, status=status):
                    record = request()
                    record["final_status"] = status
                    if status == "error":
                        record["error"] = "transport_error"
                    del record[flag]
                    self.assertTrue(MODULE.validate(manifest(), [record])[0])
        failed = request()
        failed.update(final_status="error", error="missing_usage", actual_output_tokens=None,
                      actual_prompt_tokens=None, token_count_source=None,
                      final_usage_received=False, stream_done_received=True)
        errors, _, metrics = MODULE.validate(manifest(), [failed])
        self.assertFalse(errors)
        self.assertEqual(metrics["effective_output_tokens"], 0)
        failed["stream_done_received"] = "true"
        self.assertTrue(MODULE.validate(manifest(), [failed])[0])

    def test_run_purpose_is_required_and_metadata_must_agree(self):
        data = manifest()
        del data["run_purpose"]
        self.assertTrue(MODULE.validate(data, [request()])[0])
        data = manifest()
        data["run_purpose"] = "diagnostic"
        self.write(data=data)
        report = MODULE.inspect_run(self.run)
        self.assertTrue(report["valid"], report["errors"])
        self.assertTrue(any("Diagnostic run" in warning for warning in report["warnings"]))
        metadata_path = self.run / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["run_purpose"] = "performance"
        raw = json.dumps(metadata).encode()
        metadata_path.write_bytes(raw)
        saved_manifest = json.loads((self.run / "manifest.json").read_text())
        saved_manifest["metadata_sha256"] = hashlib.sha256(raw).hexdigest()
        (self.run / "manifest.json").write_text(json.dumps(saved_manifest))
        report = MODULE.inspect_run(self.run)
        self.assertFalse(report["valid"])
        self.assertTrue(any("metadata.json.run_purpose" in error for error in report["errors"]))

    def test_saved_originals_are_required(self):
        for filename in ("workload.json", "metadata.json"):
            with self.subTest(filename=filename):
                self.write()
                (self.run / filename).unlink()
                report = MODULE.inspect_run(self.run)
                self.assertFalse(report["valid"])
                self.assertEqual(report["metrics"], {})

    def test_workload_identity_and_quota_are_reconciled(self):
        for replacement in ({"request_id": "other", "prompt_text": "x", "output_tokens": 20},
                            {"request_id": "r1", "prompt_text": "x", "output_tokens": 19}):
            with self.subTest(replacement=replacement):
                data = manifest()
                data["workload_sha256"] = MODULE.canonical_hash([replacement])
                self.write(data=data)
                (self.run / "workload.json").write_text(json.dumps([replacement]))
                report = MODULE.inspect_run(self.run)
                self.assertFalse(report["valid"])

    def test_existing_output_and_input_paths_never_overwritten(self):
        self.write()
        original = (self.run / "manifest.json").read_bytes()
        result = self.cli(self.run / "manifest.json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual((self.run / "manifest.json").read_bytes(), original)
        report = self.root / "report.json"
        report.write_text("previous result")
        self.assertEqual(self.cli(report).returncode, 2)
        self.assertEqual(report.read_text(), "previous result")

    def test_malformed_json_and_duplicate_keys_are_input_error(self):
        self.write()
        for value in ('{"run_id":"a","run_id":"b"}', "not json"):
            (self.run / "manifest.json").write_text(value)
            self.assertEqual(self.cli().returncode, 2)
            self.assertFalse((self.root / "report.json").exists())

    def test_help_has_no_side_effects(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [self.run])


if __name__ == "__main__":
    unittest.main()

import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("skill_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        mode = payload["messages"][0]["content"]
        with self.server.lock:
            self.server.received.append((self.path, payload, dict(self.headers), time.perf_counter()))
        if mode == "slow_headers":
            time.sleep(0.35)
        elif mode == "normal":
            time.sleep(0.04)
        if mode == "http_error":
            self.send_response(503)
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            if mode == "slow_body":
                # Every byte arrives before the idle timeout. Only an absolute
                # deadline can stop the not-yet-complete SSE line promptly.
                for byte in b"data: " + b"x" * 40:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.035)
                return
            if mode == "slow_chunks":
                for _ in range(20):
                    self.event({"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]})
                    time.sleep(0.035)
                return
            self.event({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
            self.event({"choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}]})
            empty = mode in ("empty_zero", "empty_positive")
            if not empty:
                self.event({"choices": [{"index": 0, "delta": {"reasoning_content": "thought"}, "finish_reason": None}]})
                self.event({"choices": [{"index": 0, "delta": {"content": "several tokens"}, "finish_reason": None}]})
                self.event({"choices": [{"index": 0, "delta": {"content": " together"}, "finish_reason": None}]})
            if mode != "no_finish":
                reason = "tool_calls" if mode == "unsupported_finish" else "stop"
                self.event({"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]})
            if mode != "missing_usage":
                count = 0 if mode in ("empty_zero", "output_zero") else 7
                self.event({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": count, "total_tokens": 11 + count}})
            if mode == "broken_stream":
                return
            if mode == "delayed_done":
                time.sleep(0.15)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def event(self, value):
        self.wfile.write(b"data: " + json.dumps(value).encode("utf-8") + b"\n\n")
        self.wfile.flush()


class BenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # All traffic stays on a dynamically allocated loopback port.
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
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
        cls.thread.join(timeout=1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.metadata = {
            "scenario": "mock", "config_sha256": "a" * 64,
            "environment_sha256": "b" * 64, "metric_contract_sha256": "c" * 64,
            "quality_status": "not_evaluated", "effective_config_verified": False,
            "profiler_enabled": False, "run_purpose": "performance",
        }
        self.metadata_path = self.root / "metadata-input.json"
        self.metadata_path.write_text(json.dumps(self.metadata, indent=2), encoding="utf-8")
        self.dataset = self.root / "input.jsonl"
        self.run_dir = self.root / "run"

    def prepare(self, modes, offsets=None):
        workload = []
        for index, mode in enumerate(modes):
            workload.append({"request_id": "r%d" % index, "prompt_text": mode,
                             "output_tokens": 64,
                             "arrival_offset_ms": None if offsets is None else offsets[index]})
        self.dataset.write_text("\n".join(json.dumps(item) for item in workload) + "\n", encoding="utf-8")
        return workload

    def args(self, extra=()):
        return ["--base-url", self.url, "--model", "mock-model",
                "--dataset", str(self.dataset), "--run-dir", str(self.run_dir),
                "--metadata", str(self.metadata_path), "--workers", "2",
                "--concurrency", "2", "--timeout", "2"] + list(extra)

    def invoke(self, modes, extra=(), offsets=None):
        self.prepare(modes, offsets)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = benchmark.main(self.args(extra))
        manifest = json.loads((self.run_dir / "manifest.json").read_text())
        records = [json.loads(line) for line in (self.run_dir / "requests.jsonl").read_text().splitlines()]
        return status, manifest, records

    def test_orphan_implementation_fields_are_rejected_before_starting_run(self):
        self.prepare(["normal"])
        for orphan in ("implementation_verified", "quality_implementation_sha256", "implementation_evidence"):
            with self.subTest(field=orphan):
                self.metadata_path.write_text(json.dumps(dict(self.metadata, **{orphan: True})))
                with mock.patch("socket.getaddrinfo") as lookup, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(benchmark.main(self.args()), 2)
                    lookup.assert_not_called()
                self.assertFalse(self.run_dir.exists())

    def test_collector_digest_changes_when_only_the_loaded_helper_changes(self):
        collector = self.root / "collector"
        collector.mkdir()
        script = collector / "benchmark.py"
        helper = collector / "implementation_identity.py"
        shutil.copyfile(SCRIPT, script)
        shutil.copyfile(SCRIPT.with_name(helper.name), helper)
        self.prepare(["normal"])
        manifests = []
        for label in ("first", "second"):
            self.run_dir = self.root / label
            if label == "second":
                with helper.open("a", encoding="utf-8") as stream:
                    stream.write("\n# Local collector helper revision for this test.\n")
            call = subprocess.run([sys.executable, "-B", str(script), *self.args()],
                                  cwd=self.root, capture_output=True, text=True, timeout=10)
            self.assertEqual(call.returncode, 0, call.stderr)
            manifest = json.loads((self.run_dir / "manifest.json").read_text())
            expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (script, helper)}
            canonical_map = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(manifest["benchmark_hash_algorithm"], "sha256-canonical-source-map-v1")
            self.assertEqual(manifest["benchmark_source_hashes"], expected)
            self.assertEqual(manifest["benchmark_sha256"], hashlib.sha256(canonical_map).hexdigest())
            manifests.append(manifest)
        self.assertNotEqual(manifests[0]["benchmark_sha256"], manifests[1]["benchmark_sha256"])
        self.assertEqual(manifests[0]["benchmark_source_hashes"]["benchmark.py"],
                         manifests[1]["benchmark_source_hashes"]["benchmark.py"])

    def test_usage_not_requested_quota_and_chunks_not_tokens(self):
        status, manifest, records = self.invoke(["normal"])
        self.assertEqual(status, 0)
        record = records[0]
        self.assertEqual(record["actual_output_tokens"], 7)
        self.assertEqual(record["requested_max_tokens"], 64)
        self.assertEqual(record["actual_prompt_tokens"], 11)
        self.assertEqual(record["token_count_source"], "server_usage")
        self.assertEqual(len(record["output_event_ts"]), 3)
        self.assertGreater(record["first_output_ts"] - record["send_start_ts"], 0.025)
        self.assertLessEqual(record["response_open_ts"], record["first_output_ts"])
        self.assertLessEqual(record["last_output_ts"], record["end_ts"])
        self.assertLessEqual(manifest["start_ts"], record["send_start_ts"])
        self.assertLessEqual(record["end_ts"], manifest["end_ts"])
        self.assertFalse(manifest["effective_config_verified"])
        self.assertEqual(manifest["quality_status"], "not_evaluated")
        self.assertTrue((self.run_dir / record["raw_response_path"]).is_file())
        output = json.loads((self.run_dir / record["output_path"]).read_text())
        self.assertEqual(output, {"content": "several tokens together", "reasoning_content": "thought"})
        request = json.loads((self.run_dir / record["raw_request_path"]).read_text())
        self.assertEqual(request["stream_options"], {"include_usage": True})
        self.assertNotIn("ignore_eos", request)

    def test_missing_usage_remains_unknown_and_failure_retains_time(self):
        status, manifest, records = self.invoke(["missing_usage"])
        record = records[0]
        self.assertEqual(status, 1)
        self.assertEqual(record["error"], "missing_final_usage")
        self.assertIsNone(record["actual_output_tokens"])
        self.assertIsNone(record["token_count_source"])
        self.assertGreater(record["end_ts"], record["send_start_ts"])
        self.assertGreater(manifest["end_ts"], manifest["start_ts"])

    def test_retained_terminal_timestamps_localize_slow_done_without_moving_endpoint(self):
        status, _, records = self.invoke(["delayed_done"])
        self.assertEqual(status, 0)
        record, = records
        stamps = [record[k] for k in
                  ("last_output_ts", "finish_reason_ts", "final_usage_ts", "stream_done_ts", "end_ts")]
        self.assertEqual(stamps, sorted(stamps))
        tail_tool = SCRIPT.parents[2] / "diagnose" / "scripts" / "stream_tail.py"
        call = subprocess.run([sys.executable, "-B", str(tail_tool), "--run", str(self.run_dir),
                               "--long-tail-s", "0.1"], capture_output=True, text=True, timeout=10)
        self.assertEqual(call.returncode, 0, call.stdout + call.stderr)
        report = json.loads(call.stdout)
        self.assertEqual(report["terminal_timestamp_coverage"], {"complete": 1, "partial": 0, "legacy": 0})
        self.assertEqual(report["long_tail_requests"], ["r0"])
        phases = report["requests"][0]["client_seconds"]
        self.assertGreater(phases["final_usage_to_done"], 0.10)
        self.assertEqual(phases["e2e"], record["end_ts"] - record["send_start_ts"])

    def test_broken_stream_never_becomes_success_from_usage(self):
        status, _, records = self.invoke(["broken_stream"])
        record = records[0]
        self.assertEqual(status, 1)
        self.assertEqual(record["actual_output_tokens"], 7)
        self.assertEqual(record["error"], "stream_ended_without_done")
        self.assertFalse(record["stream_done_received"])

    def test_empty_output_with_zero_actual_usage_is_distinct_from_missing(self):
        status, _, records = self.invoke(["empty_zero"])
        record = records[0]
        self.assertEqual(status, 0)
        self.assertEqual(record["actual_output_tokens"], 0)
        self.assertIsNone(record["first_output_ts"])
        self.assertIsNone(record["last_output_ts"])
        self.assertEqual(record["output_event_ts"], [])

    def test_positive_usage_without_supported_payload_is_failure(self):
        status, _, records = self.invoke(["empty_positive"])
        self.assertEqual(status, 1)
        self.assertEqual(records[0]["error"], "positive_usage_without_output_payload")

    def test_payload_with_zero_usage_is_failure(self):
        status, _, records = self.invoke(["output_zero"])
        self.assertEqual(status, 1)
        self.assertEqual(records[0]["error"], "output_payload_with_zero_usage")

    def test_total_deadline_includes_slow_headers(self):
        start = time.perf_counter()
        status, _, records = self.invoke(["slow_headers"], ["--timeout", "0.12"])
        self.assertEqual(status, 1)
        self.assertEqual(records[0]["final_status"], "timeout")
        self.assertLess(time.perf_counter() - start, 0.6)

    def test_total_deadline_stops_byte_trickle_before_idle_timeout(self):
        start = time.perf_counter()
        status, _, records = self.invoke(["slow_body"], ["--timeout", "0.16"])
        self.assertEqual(status, 1)
        self.assertEqual(records[0]["final_status"], "timeout")
        self.assertLess(time.perf_counter() - start, 0.6)
        self.assertGreaterEqual(records[0]["end_ts"] - records[0]["send_start_ts"], 0.13)

    def test_total_deadline_stops_continuous_valid_chunks(self):
        status, _, records = self.invoke(["slow_chunks"], ["--timeout", "0.14"])
        self.assertEqual(status, 1)
        self.assertEqual(records[0]["final_status"], "timeout")
        self.assertGreater(len(records[0]["output_event_ts"]), 1)
        self.assertLess(records[0]["end_ts"] - records[0]["send_start_ts"], 0.5)

    def test_missing_finish_and_unsupported_finish_are_failures(self):
        status, _, records = self.invoke(["no_finish", "unsupported_finish"])
        self.assertEqual(status, 1)
        self.assertTrue(all(record["final_status"] == "error" for record in records))
        self.assertEqual({record["error"] for record in records},
                         {"usage_before_finish", "missing_or_unsupported_finish_reason"})

    def test_http_failure_is_retained_and_never_retried(self):
        with self.server.lock:
            previous = len(self.server.received)
        status, _, records = self.invoke(["http_error"])
        self.assertEqual(status, 1)
        self.assertEqual(records[0]["error"], "http_status_503")
        with self.server.lock:
            self.assertEqual(len(self.server.received) - previous, 1)

    def test_existing_directory_refused_before_sending(self):
        self.prepare(["normal"])
        self.run_dir.mkdir()
        marker = self.run_dir / "do-not-touch"
        marker.write_text("retained")
        with self.server.lock:
            previous = len(self.server.received)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(benchmark.main(self.args()), 2)
        self.assertEqual(marker.read_text(), "retained")
        with self.server.lock:
            self.assertEqual(len(self.server.received), previous)

    def test_actual_subset_hashes_and_metadata_original_bytes(self):
        workload = self.prepare(["normal", "empty_zero"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(benchmark.main(self.args(["--max-requests", "1"])), 0)
        manifest = json.loads((self.run_dir / "manifest.json").read_text())
        self.assertEqual(manifest["n_requests"], 1)
        self.assertEqual(json.loads((self.run_dir / "workload.json").read_text()), workload[:1])
        self.assertEqual(manifest["workload_sha256"], hashlib.sha256(benchmark.canonical_bytes(workload[:1])).hexdigest())
        self.assertEqual(manifest["metadata_sha256"], hashlib.sha256(self.metadata_path.read_bytes()).hexdigest())
        self.assertEqual((self.run_dir / "metadata.json").read_bytes(), self.metadata_path.read_bytes())
        self.assertEqual(manifest["benchmark_source_hashes"]["benchmark.py"], hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
        self.assertEqual(manifest["benchmark_sha256"], hashlib.sha256(benchmark.canonical_bytes(manifest["benchmark_source_hashes"])).hexdigest())
        self.assertEqual(manifest["client_measurement_policy_sha256"], hashlib.sha256(benchmark.canonical_bytes(manifest["client_measurement_policy"])).hexdigest())

    def test_policy_changes_with_effective_parameters(self):
        first = benchmark.parser().parse_args(self.args())
        second = benchmark.parser().parse_args(self.args(["--ignore-eos"]))
        self.assertNotEqual(benchmark.digest(benchmark.canonical_bytes(benchmark.measurement_policy(first))),
                            benchmark.digest(benchmark.canonical_bytes(benchmark.measurement_policy(second))))
        third = benchmark.parser().parse_args(self.args(["--run-dir", "/another/run"]))
        self.assertEqual(benchmark.measurement_policy(first), benchmark.measurement_policy(third))

    def test_ignore_eos_only_when_explicitly_requested(self):
        _, _, records = self.invoke(["normal"], ["--ignore-eos"])
        request = json.loads((self.run_dir / records[0]["raw_request_path"]).read_text())
        self.assertIs(request["ignore_eos"], True)

    def test_open_loop_preserves_schedule_and_reports_client_backlog(self):
        status, manifest, records = self.invoke(["normal", "normal"],
                                               ["--mode", "open_loop", "--workers", "1"],
                                               offsets=[0, 5])
        self.assertEqual(status, 0)
        by_id = {record["request_id"]: record for record in records}
        self.assertAlmostEqual(by_id["r1"]["planned_send_ts"] - manifest["start_ts"], 0.005)
        self.assertGreater(by_id["r1"]["send_lag_s"], 0.02)
        self.assertGreaterEqual(by_id["r1"]["send_start_ts"], by_id["r0"]["end_ts"])

    def test_closed_loop_refuses_to_ignore_arrival_schedule(self):
        self.prepare(["normal"], offsets=[0])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(benchmark.main(self.args()), 2)
        self.assertFalse(self.run_dir.exists())

    def test_explicit_metadata_declarations_required(self):
        self.prepare(["normal"])
        self.metadata.pop("profiler_enabled")
        self.metadata_path.write_text(json.dumps(self.metadata))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(benchmark.main(self.args()), 2)
        self.assertFalse(self.run_dir.exists())

    def test_diagnostic_run_purpose_is_preserved(self):
        self.metadata["run_purpose"] = "diagnostic"
        self.metadata_path.write_text(json.dumps(self.metadata), encoding="utf-8")
        status, manifest, _ = self.invoke(["normal"])
        self.assertEqual(status, 0)
        self.assertEqual(manifest["run_purpose"], "diagnostic")
        self.assertEqual(json.loads((self.run_dir / "metadata.json").read_text())["run_purpose"], "diagnostic")

    def test_missing_or_invalid_run_purpose_is_rejected_before_traffic(self):
        self.prepare(["normal"])
        with self.server.lock:
            previous = len(self.server.received)
        for purpose in (None, "debug", True):
            with self.subTest(purpose=purpose):
                if purpose is None:
                    self.metadata.pop("run_purpose", None)
                else:
                    self.metadata["run_purpose"] = purpose
                self.metadata_path.write_text(json.dumps(self.metadata), encoding="utf-8")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(benchmark.main(self.args()), 2)
                self.assertFalse(self.run_dir.exists())
        with self.server.lock:
            self.assertEqual(len(self.server.received), previous)

    def test_api_key_env_is_used_but_never_serialized(self):
        secret = "test-secret-unsafe-to-log"
        with mock.patch.dict(os.environ, {"BENCHMARK_TEST_KEY": secret}):
            status, _, _ = self.invoke(["normal"], ["--api-key-env", "BENCHMARK_TEST_KEY"])
        self.assertEqual(status, 0)
        for path in self.run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())
        with self.server.lock:
            self.assertEqual(self.server.received[-1][2]["Authorization"], "Bearer " + secret)

    def test_base_url_rejects_credentials_query_and_fragment(self):
        for url in ("http://user:secret@127.0.0.1/v1", "http://127.0.0.1/v1?api_key=secret", "http://127.0.0.1/#secret"):
            with self.subTest(url=url), self.assertRaises(benchmark.InputError):
                benchmark.parse_endpoint(url)

    def test_help_has_no_filesystem_or_network_side_effects(self):
        with mock.patch.object(benchmark.socket, "getaddrinfo", side_effect=AssertionError("network called")):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                benchmark.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(list(self.root.iterdir()), [self.metadata_path])


if __name__ == "__main__":
    unittest.main()

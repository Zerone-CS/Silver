import gzip
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluate.tests.test_benchmark import MockHandler


class Service(MockHandler):
    def do_POST(self):
        self.server.calls.append((self.path, self.headers.get("Authorization")))
        if self.path not in ("/start_profile", "/stop_profile"):
            return super().do_POST()
        status = self.server.control_codes.get(self.path, 200)
        if status == "connection_lost":
            self.close_connection = True
            return
        if self.path == "/stop_profile" and status == 200:
            self.server.export()
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


class VLLMProfileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        self.server.calls, self.server.received = [], []
        self.server.lock = threading.Lock()
        self.server.control_codes = {}
        self.server.export = lambda: None
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.exports = self.root / "exports"
        self.spec = {"schema_version": 1, "format": "chrome-trace-v1", "diagnostic_run": "diag",
                     "tool": {"name": "torch_npu.profiler", "version": "fixture"},
                     "window": {"clock": "fixture", "start_us": 2, "end_us": 8},
                     "rank_field": {"scope": "ascend-profiler-info", "path": "profiler_info_{rank}.json"},
                     "requirements": [{"id": "kernel", "field": "cat", "pattern": "^kernel$", "min_count": 1}],
                     "traces": {"0": "worker0/ASCEND_PROFILER_OUTPUT/trace_view.json",
                                "1": "worker1/ASCEND_PROFILER_OUTPUT/trace_view.json.gz"}}
        (self.root / "spec.json").write_text(json.dumps(self.spec))
        self.dataset("normal")
        self.metadata = {"scenario": "test", "config_sha256": "a" * 64, "environment_sha256": "b" * 64,
                         "metric_contract_sha256": "c" * 64, "profiler_enabled": True,
                         "run_purpose": "diagnostic", "quality_status": "not_evaluated", "effective_config_verified": False}
        (self.root / "meta.json").write_text(json.dumps(self.metadata))

    def dataset(self, prompt):
        row = {"request_id": "r1", "prompt_text": prompt, "output_tokens": 8}
        (self.root / "dataset.jsonl").write_text(json.dumps(row) + "\n")

    def emit(self, rank, worker):
        folder = self.exports / (worker + "_ascend_pt")
        trace = folder / "ASCEND_PROFILER_OUTPUT" / ("trace_view.json" + (".gz" if rank else ""))
        trace.parent.mkdir(parents=True)
        doc = [{"ph": "i", "name": "begin", "ts": 0}, {"ph": "X", "name": "step-" + str(rank),
                "ts": 3, "dur": 1, "cat": "kernel"}, {"ph": "i", "name": "end", "ts": 10}]
        data = json.dumps(doc).encode()
        trace.write_bytes(gzip.compress(data) if rank else data)
        (folder / ("profiler_info_%d.json" % rank)).write_text(json.dumps({"rank_id": rank}))
        return trace

    def run_adapter(self, wrap=False):
        adapter = ROOT / "diagnose/scripts/vllm_profile.py"
        argv = [sys.executable, "-B", str(adapter), "--spec", "spec.json", "--export-root", str(self.exports),
                "--base-url", "http://127.0.0.1:%d/v1" % self.server.server_port,
                "--api-key-env", "FIXTURE_PROFILE_KEY", "--export-timeout", "0.65", "--settle-seconds", "0.05",
                "--", "--model", "fixture", "--dataset", "dataset.jsonl", "--metadata", "meta.json",
                "--concurrency", "1", "--workers", "1"]
        if wrap:
            argv = [sys.executable, "-B", str(ROOT / "diagnose/scripts/profile.py"), "capture",
                    "--spec", "spec.json", "--out", "bundle", "--", *argv]
        return subprocess.run(argv, cwd=self.root, capture_output=True, text=True, timeout=8,
                              env=dict(os.environ, FIXTURE_PROFILE_KEY="fixture-credential"))

    def test_real_benchmark_cli_and_capture_use_new_rank_files(self):
        self.emit(0, "old")
        self.server.export = lambda: (self.emit(1, "fresh1"), self.emit(0, "fresh0"))
        result = self.run_adapter(wrap=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["valid"])
        self.assertEqual(set(report["coverage"]), {"0", "1"})
        for rank in ("0", "1"):
            dst = self.root / self.spec["traces"][rank]
            source = self.exports / ("fresh" + rank + "_ascend_pt") / "ASCEND_PROFILER_OUTPUT" / dst.name
            self.assertTrue(source.exists())
            self.assertEqual(dst.read_bytes(), source.read_bytes())
        self.assertEqual([p for p, _ in self.server.calls],
                         ["/start_profile", "/v1/chat/completions", "/stop_profile"])
        self.assertTrue(all(k == "Bearer fixture-credential" for _, k in self.server.calls))
        log = (self.root / "bundle/stdout.log").read_text()
        self.assertNotIn("fixture-credential", log)
        self.assertIn("fresh1_ascend_pt", log)

    def test_stale_output_cannot_cover_missing_rank(self):
        self.emit(0, "old")
        self.server.export = lambda: self.emit(1, "fresh")
        result = self.run_adapter()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing ranks: ['0']", result.stderr)
        self.assertFalse((self.root / self.spec["traces"]["0"]).exists())

    def test_two_new_workers_for_the_same_rank_fail(self):
        self.server.export = lambda: (self.emit(0, "first"), self.emit(0, "second"), self.emit(1, "fresh1"))
        result = self.run_adapter()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ambiguous", result.stderr)
        self.assertFalse((self.root / self.spec["traces"]["0"]).exists())

    def test_benchmark_failure_stops_profiler(self):
        self.dataset("http_error")
        result = self.run_adapter()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([p for p, _ in self.server.calls],
                         ["/start_profile", "/v1/chat/completions", "/stop_profile"])
        self.assertTrue((self.root / "diag/manifest.json").is_file())

    def test_stop_error_is_signalled_and_sources_not_promoted(self):
        self.server.control_codes["/stop_profile"] = 503
        result = self.run_adapter()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stop_profile returned HTTP 503", result.stderr)
        self.assertFalse((self.root / self.spec["traces"]["0"]).exists())

    def test_start_rejected_does_not_send_workload(self):
        self.server.control_codes["/start_profile"] = 404
        result = self.run_adapter()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("start_profile returned HTTP 404", result.stderr)
        self.assertEqual([p for p, _ in self.server.calls], ["/start_profile"])

    def test_lost_start_response_attempts_stop_before_returning_error(self):
        self.server.control_codes["/start_profile"] = "connection_lost"
        result = self.run_adapter()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([p for p, _ in self.server.calls], ["/start_profile", "/stop_profile"])

    def test_invalid_diagnostic_mode_rejected_before_live_control(self):
        self.metadata["profiler_enabled"] = False
        (self.root / "meta.json").write_text(json.dumps(self.metadata))
        result = self.run_adapter()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("metadata must declare", result.stderr)
        self.assertEqual(self.server.calls, [])


if __name__ == "__main__":
    unittest.main()

import gzip
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diagnose.scripts import profile
from evaluate.tests import test_validate_run as run_fixtures


class ProfileCaptureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        writer = run_fixtures.ValidateRunTest("test_full_window_and_single_request_inflight")
        writer.run = self.root / "source-run"
        writer.run.mkdir()
        self.diagnostic = run_fixtures.manifest()
        self.diagnostic.update(run_purpose="diagnostic", profiler_enabled=True)
        writer.write(self.diagnostic)
        self.spec = {
            "schema_version": 1, "format": "chrome-trace-v1",
            "tool": {"name": "torch_npu.profiler", "version": "fixture-1"},
            "diagnostic_run": "source-run", "traces": {"0": "a.json", "1": "b.json.gz"},
            "rank_field": {"scope": "event", "path": "args.rank"},
            "window": {"clock": "shared-trace-us", "start_event": "begin", "end_event": "end"},
            "requirements": [{"id": "kernel", "field": "cat", "pattern": "^kern(el)?$",
                              "min_count": 1}],
        }
        self.spec_path = self.root / "spec.json"
        self.bundle = self.root / "external" / "sealed"
        self.trace(0)
        self.trace(1)

    def trace(self, rank, left=10, right=90, kernel=45, identity=None):
        events = [{"ph": "i", "pid": 1, "tid": 1, "ts": left, "name": "begin",
                   "args": {"rank": rank if identity is None else identity}},
                  {"ph": "X", "pid": 1, "tid": 1, "ts": str(kernel), "dur": "2.5", "name": "MatMul", "cat": "kernel"},
                  {"ph": "i", "pid": 1, "tid": 1, "ts": right, "name": "end"}]
        doc = {"traceEvents": events, "displayTimeUnit": "ns"} if rank == 1 else events
        path = self.root / self.spec["traces"][str(rank)]
        data = json.dumps(doc).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(data) if path.suffix == ".gz" else data)

    def run_capture(self, *args):
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        return profile.capture(self.spec_path, self.bundle, *args)

    def test_import_replay_identity_and_hash(self):
        report = self.run_capture()
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["coverage"]["0"]["event_counts"], {"kernel": 1})
        self.assertEqual(report["run_id"], "run-1")
        self.assertEqual(report["environment_sha256"], "c" * 64)
        self.assertIsNone(report["implementation_sha256"])
        for key in ("manifest_sha256", "spec_sha256", "profile_policy_sha256"):
            self.assertRegex(report[key], "^[0-9a-f]{64}$")
        shutil.rmtree(self.root / "source-run")
        (self.root / "a.json").unlink()
        self.assertEqual(profile.verify_bundle(self.bundle), report)
        (self.bundle / "raw" / "rank-0.json").write_bytes(b"[]")
        self.assertIn("hash mismatch", str(profile.verify_bundle(self.bundle)["errors"]))

    def test_msprof_import_seals_and_replays_tool_version_evidence(self):
        self.spec["tool"] = {"name": "msprof", "version": "8.3.RC1", "version_evidence": "version.info"}
        evidence = self.root / "version.info"
        evidence.write_text("Version=8.3.RC1\n", encoding="utf-8")
        report = self.run_capture()
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["profile_policy"]["tool"]["version_evidence"], "archived_text")
        self.assertEqual(report["tool_version_evidence_sha256"], profile.file_hash(evidence))
        evidence.write_text("Version=8.3.RC17\n", encoding="utf-8")
        self.assertEqual(profile.verify_bundle(self.bundle), report)
        self.bundle = self.root / "wrong-version"
        self.assertIn("disagrees with the declared version", str(self.run_capture()["errors"]))

    def test_missing_rank_truncation_wrong_identity_wrong_window(self):
        tests = [
            (lambda: (self.root / "b.json.gz").unlink(), "missing export"),
            (lambda: (self.root / "b.json.gz").write_bytes((self.root / "b.json.gz").read_bytes()[:-4]), "rank 1:"),
            (lambda: (self.root / "a.json").write_bytes(b'[{"ph":"X"'), "rank 0:"),
            (lambda: self.trace(1, identity=0), "expected rank 1"),
            (lambda: self.trace(1, kernel=3), "kernel has 0 events"),
            (lambda: self.trace(1, left=100, right=200, kernel=150), "no common rank window"),
            (lambda: self.trace(1, left=50, right=95, kernel=52), "rank 0: kernel has 0 events"),
        ]
        for i, (damage, text) in enumerate(tests):
            with self.subTest(i=i):
                self.bundle = self.root / f"bundle{i}"
                self.trace(0)
                self.trace(1)
                damage()
                report = self.run_capture()
                self.assertFalse(report["valid"])
                self.assertIn(text, str(report["errors"]))

    def test_rejects_incomplete_or_nondiagnostic_benchmark(self):
        path = self.root / "source-run" / "manifest.json"
        data = json.loads(path.read_text())
        data["profiler_enabled"] = False
        path.write_text(json.dumps(data))
        report = self.run_capture()
        self.assertFalse(report["valid"])
        self.assertIn("profiler_enabled", str(report["errors"]))
        (self.bundle / "manifest.json").unlink()
        self.assertFalse(profile.verify_bundle(self.bundle)["valid"])

    def test_raw_replay_ignores_saved_success_report(self):
        self.run_capture()
        path = self.bundle / "diagnostic-run" / "requests.jsonl"
        path.write_bytes(b"{}")
        self.assertIn("requests.jsonl", str(profile.verify_bundle(self.bundle)["errors"]))

    def test_trace_field_must_bind_to_the_measured_run(self):
        self.spec["run_binding"] = {"scope": "event", "path": "args.run_id", "source": "run_id"}
        self.spec["traces"] = {"0": "a.json"}
        events = json.loads((self.root / "a.json").read_text())
        events[0]["args"]["run_id"] = "another-run"
        (self.root / "a.json").write_text(json.dumps(events))
        self.assertIn("association", str(self.run_capture()["errors"]))
        self.bundle = self.root / "matches"
        events[0]["args"]["run_id"] = self.diagnostic["run_id"]
        (self.root / "a.json").write_text(json.dumps(events))
        report = self.run_capture()
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["run_binding_status"], "trace_field_verified")

    def test_cli_command_capture_and_no_overwrite(self):
        self.spec.update(diagnostic_run="new-diag", traces={"0": "new.json"})
        self.spec_path.write_text(json.dumps(self.spec))
        copier = ("import shutil;shutil.copytree('source-run','new-diag');"
                  "shutil.copyfile('a.json','new.json');print('finished')")
        cmd = [sys.executable, "-B", str(ROOT / "diagnose/scripts/profile.py"), "capture",
               "--spec", str(self.spec_path), "--out", str(self.bundle), "--cwd", str(self.root),
               "--", sys.executable, "-c", copier]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("finished", (self.bundle / "stdout.log").read_text())
        self.assertTrue(json.loads(proc.stdout)["valid"])
        self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
        self.bundle = self.root / "stale"
        report = self.run_capture([sys.executable, "-c", "raise Exception('should not run')"])
        self.assertIn("fresh paths", str(report["errors"]))

    def test_failed_and_timed_out_commands_keep_logs(self):
        self.spec.update(diagnostic_run="new-diag", traces={"0": "new.json"})
        for index, (body, timeout, expected) in enumerate([
                ("import sys;sys.exit(7)", None, "exit code 7"),
                ("import time;time.sleep(9)", 0.1, "timed out")]):
            with self.subTest(index=index):
                self.bundle = self.root / f"attempt{index}"
                report = self.run_capture([sys.executable, "-c", body], self.root, timeout)
                self.assertFalse(report["valid"])
                self.assertIn(expected, str(report["errors"]))
                self.assertTrue((self.bundle / "stderr.log").is_file())

    def test_numeric_policy_freezes_duration_and_actual_coverage(self):
        self.spec["window"] = {"clock": "trace_us", "start_us": "30.1", "end_us": "75.1"}
        before = profile.profile_policy(self.spec)
        self.assertTrue(self.run_capture()["valid"])
        self.spec["window"].update(start_us="100.1", end_us="145.1")
        self.assertEqual(profile.profile_policy(self.spec), before)
        self.bundle = self.root / "wrong-window"
        self.assertIn("does not bracket", str(self.run_capture()["errors"]))

    def test_observed_extent_uses_overlap_and_predeclared_minimum_duration(self):
        self.spec["window"] = {"clock": "trace_us", "mode": "trace_extent", "min_duration_us": 40}
        self.trace(1, left=30, right=80)
        result = self.run_capture()
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["coverage"]["0"]["window_start_us"], "30")
        self.assertEqual(result["coverage"]["0"]["window_end_us"], "80")
        self.spec["window"]["min_duration_us"] = 60
        self.bundle = self.root / "too-short"
        self.assertIn("shorter than", str(self.run_capture()["errors"]))

    def test_native_rank_info_and_document_metadata(self):
        self.spec["traces"] = {"0": "worker/ASCEND_PROFILER_OUTPUT/trace_view.json"}
        self.spec["rank_field"] = {"scope": "ascend-profiler-info", "path": "profiler_info_{rank}.json"}
        self.trace(0)
        sidecar = self.root / "worker/profiler_info_0.json"
        sidecar.write_text('{"config":{}}')
        self.assertTrue(self.run_capture()["valid"])
        self.bundle = self.root / "mislabeled"
        sidecar.write_text('{"rank_id":1}')
        self.assertFalse(self.run_capture()["valid"])
        self.bundle = self.root / "kineto"
        self.spec["rank_field"] = {"scope": "document", "path": "distributedInfo.rank"}
        path = self.root / self.spec["traces"]["0"]
        events = json.loads(path.read_text())
        path.write_text(json.dumps({"traceEvents": events, "distributedInfo": {"rank": 0}}))
        self.assertTrue(self.run_capture()["valid"])


if __name__ == "__main__":
    unittest.main()

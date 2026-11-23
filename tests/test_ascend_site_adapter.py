"""Replay the single-engine diagnostic adapter with synthetic streams and native logs."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from diagnose.adapters.vllm_023_single_engine import site_export
from diagnose.scripts import step_sources, stream_tail
from evaluate.scripts import validate_run
from test_step_analysis_integration import create_run


def iteration(idx):
    return ("Iteration(%d): 1 context requests, 32 context tokens, 0 generation requests, "
            "0 generation tokens, iteration elapsed time: 5 ms\n") % idx


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


class AscendSiteAdapter(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.run = base / "A/run"
        create_run(self.run.parent, end=10, step_count=2, config_hash="b"*64)
        self.capture = "cap_a"
        metadata = site_export.json_read(self.run / "metadata.json")
        metadata["profile_capture_id"] = self.capture
        save(self.run / "metadata.json", metadata)
        self.manifest = site_export.json_read(self.run / "manifest.json")
        self.manifest["metadata_sha256"] = site_export.sha(self.run / "metadata.json")
        save(self.run / "manifest.json", self.manifest)
        self.probe = base / "probe"
        self.snap = self.probe / self.capture
        self.snap.mkdir(parents=True)
        for name in ("open", "close"):
            save(self.snap / (name + ".json"), {"container": "test-engine", "capture_id": self.capture})
        (self.snap / "before.log").write_text(iteration(0))
        (self.snap / "after.log").write_text("".join(iteration(i) for i in range(3)))
        self.raw = self.probe / "engine-1.jsonl"
        self.out = base / "evidence"
        # A worker may write schedules for multiple batches before the first one completes.
        self.events = [
            {"kind": "bridge", "capture_id": "warmup", "response_id": "other", "backend_id": "other"},
            {"kind": "bridge", "response_id": "chatcmpl-synthetic-A", "backend_id": "req"},
            {"kind": "schedule", "step_id": 1, "scheduled": {"req": 32}},
            {"kind": "schedule", "step_id": 2, "scheduled": {"req": 4}},
            {"kind": "complete", "step_id": 2},
            {"kind": "complete", "step_id": 1},
            {"kind": "commit", "step_id": 1, "backend_id": "req", "output_start": 0, "output_count": 16},
        ]

    def invoke(self, expect_issue=None):
        self.raw.write_text("".join(json.dumps(dict({"capture_id": self.capture}, **row)) + "\n"
                                    for row in self.events))
        result = subprocess.run([sys.executable, "-B", site_export.__file__, "prepare", "--root", self.probe,
                                 "--run", self.run, "--suite", ROOT, "--output", self.out],
                                capture_output=True, text=True)
        if expect_issue:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expect_issue, result.stderr)
            self.assertFalse((self.out / "provenance.json").exists())
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            return site_export.json_read(self.out / "report.json")

    def read_csv(self, name):
        with (self.out / name).open(encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def test_csv_rows_native_seal_and_fingerprints_reach_originals(self):
        report = self.invoke()
        self.assertEqual(report["coverage"]["status"], "complete")
        self.assertEqual(report["metrics"]["N"], 16)
        self.assertEqual(report["metrics"]["K"], 2)
        self.assertEqual(site_export.json_read(self.out / "provenance.json")["last_completed_step"], 1)
        engine = [json.loads(row) for row in (self.out / "engine.jsonl").read_text().splitlines()]
        for row in self.read_csv("inventory.csv"):
            for kind in ("schedule", "complete"):
                event = engine[int(row[kind+"_jsonl_line"]) - 1]
                self.assertEqual((event["kind"], event["capture_id"], str(event["step_id"])),
                                 (kind, row["capture"], row["step"]))
        for row in self.read_csv("bridges.csv"):
            event = engine[int(row["jsonl_line"]) - 1]
            self.assertEqual((event["response_id"], event["backend_id"]), (row["external"], row["backend"]))
        for row in self.read_csv("commits.csv"):
            event = engine[int(row["jsonl_line"]) - 1]
            self.assertEqual((event["step_id"], event["output_start"], event["output_count"]),
                             (int(row["step"]), int(row["start"]), int(row["count"])))
        seal, = self.read_csv("seal.csv")
        self.assertEqual((seal["expected_steps"], seal["native_first"], seal["native_last"]), ("2", "1", "2"))
        for name in ("before", "after"):
            self.assertEqual(seal[name+"_sha256"], site_export.sha(self.snap / (name+".log")))
        sources = {Path(r["path"]).name: r["sha256"] for r in report["sources"]}
        for name in site_export.EVIDENCE_FILES:
            self.assertEqual(sources[name], site_export.sha(self.out / name))
        with (self.out / "engine.jsonl").open("a") as f:
            f.write("\n")
        with self.assertRaisesRegex(ValueError, "fingerprint|hash"):
            step_sources.read_sources(self.out / "sources.json", self.run)

    def test_native_iteration_ids_require_complete_prefix_without_gaps_or_dummy(self):
        for ids in ([1, 2], [0, 2], [0, 1, 1], [0, 2, 1]):
            with self.subTest(ids=ids):
                (self.snap / "after.log").write_text("".join(iteration(i) for i in ids))
                with self.assertRaisesRegex(ValueError, "native iteration series"):
                    site_export.native(self.snap / "after.log")
        (self.snap / "after.log").write_text(iteration(0).rstrip() + " (dummy)\n")
        with self.assertRaisesRegex(ValueError, "dummy"):
            site_export.native(self.snap / "after.log")

    def test_stream_tail_correlates_legacy_client_and_clocked_engine_and_api_by_id(self):
        self.events.append({"kind": "finish", "step_id": 1, "backend_id": "req", "reason": "stop"})
        for idx, event in enumerate(self.events):
            event["monotonic_ns"] = 1000 + idx
        self.invoke()
        api = self.out / "api-events.json"
        save(api, {"schema_version": 1, "run_id": self.manifest["run_id"],
                   "manifest_sha256": site_export.sha(self.run / "manifest.json"),
                   "clock": "api-local-monotonic-ns",
                   "events": [{"response_id": "chatcmpl-synthetic-A", "last_output_written_ns": 250,
                               "done_written_ns": 1500}]})
        result = stream_tail.analyze(self.run, engine_evidence=self.out, api_evidence=api)
        self.assertEqual(result["terminal_timestamp_coverage"]["legacy"], 1)
        request, = result["requests"]
        self.assertEqual(request["client_seconds"]["finish_to_final_usage"], None)
        self.assertEqual(request["engine_ns"], {"admitted": 1001, "first_scheduled": 1002,
                                                  "first_output_committed": 1006,
                                                  "last_output_committed": 1006, "finished": 1007})
        self.assertEqual(request["api_ns"]["done_written"], 1500)
        self.assertEqual(result["cross_clock_alignment"], "identity_only")
        bad = site_export.json_read(api)
        bad["run_id"] = "outside this capture"
        save(api, bad)
        with self.assertRaisesRegex(ValueError, "does not match the benchmark"):
            stream_tail.analyze(self.run, engine_evidence=self.out, api_evidence=api)

    def test_native_and_probe_must_agree(self):
        (self.snap / "after.log").write_text("".join(iteration(i) for i in range(4)))
        self.invoke("independent native count")

    def test_admission_from_another_gate_rejected(self):
        self.events[1]["capture_id"] = "warmup"
        self.invoke("outside this gate")

    def test_committed_request_must_participate_in_same_step(self):
        self.events[-1]["backend_id"] = "other"
        self.invoke("was not scheduled")

    def test_gate_request_must_match_original_sse(self):
        self.events[1]["response_id"] = "other"
        self.invoke("all and only measured SSE request IDs")

    def test_failed_unadmitted_client_request_invalidates_isolation(self):
        failed = dict(json.loads((self.run/"requests.jsonl").read_text()), request_id="never-admitted",
                      response_open_ts=None, first_output_ts=None, last_output_ts=None, output_event_ts=[],
                      actual_output_tokens=None, actual_prompt_tokens=None, token_count_source=None,
                      stream_done_received=False, final_usage_received=False, final_status="error",
                      finish_reason=None, error="connection failed")
        with (self.run/"requests.jsonl").open("a") as f:
            f.write(json.dumps(failed) + "\n")
        jobs = site_export.json_read(self.run/"workload.json")
        jobs.append(dict(jobs[0], request_id=failed["request_id"]))
        save(self.run/"workload.json", jobs)
        self.manifest["workload_sha256"] = validate_run.canonical_hash(jobs)
        save(self.run/"manifest.json", self.manifest)
        self.invoke("all and only measured SSE request IDs")

    def test_zero_output_final_completion_withholds_full_window_factors(self):
        self.events[4:] = [self.events[5], self.events[6], self.events[4]]
        report = self.invoke()
        self.assertEqual(report["scope"], "partial")
        self.assertEqual(report["coverage"]["unmapped_output_tokens"], 0)
        for metric in ("K", "R", "Y_bar"):
            self.assertIsNone(report["metrics"][metric])
        self.assertEqual(report["metrics"]["N"], 16)


if __name__ == "__main__":
    unittest.main()

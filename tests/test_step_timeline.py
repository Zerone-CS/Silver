"""Replay a sealed diagnostic fixture; trace timestamps and IDs are synthetic."""
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diagnose.scripts import profile, step_timeline
from evaluate.tests import test_validate_run as fixtures


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def span(name, start, duration, step=None, tid=1, **args):
    if step is not None:
        args["step"] = step
    return {"ph": "X", "pid": 1, "tid": tid, "ts": start, "dur": duration,
            "name": name, "args": args}


class TimelineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        writer = fixtures.ValidateRunTest("test_full_window_and_single_request_inflight")
        writer.run = self.root / "run"
        writer.run.mkdir()
        manifest = fixtures.manifest()
        manifest.update(run_purpose="diagnostic", profiler_enabled=True)
        writer.write(manifest)
        self.run_id = manifest["run_id"]
        self.spec = {"schema_version": 1, "format": "chrome-trace-v1", "diagnostic_run": "run",
                     "tool": {"name": "torch_npu.profiler", "version": "synthetic-test"},
                     "traces": {"0": "0.json", "1": "1.json.gz"},
                     "rank_field": {"scope": "document", "path": "rank"},
                     "window": {"clock": "same_clock", "start_us": 10, "end_us": 90},
                     "run_binding": {"scope": "document", "path": "run", "source": "run_id"},
                     "requirements": [{"id": "operator", "field": "name", "pattern": "^kernel$", "min_count": 1}]}
        self.mapping = {"schema_version": 1, "tool": self.spec["tool"], "step_field": "args.step",
                        "replica_by_rank": {"0": "dp0", "1": "dp0"},
                        "correlation": {"producer_field": "args.push", "consumer_field": "args.pop"},
                        "rules": [{"stage": stage, "field": "name", "pattern": name} for stage, name in
                                  (("scheduler", "schedule"), ("runner", "runner"), ("device", "kernel"),
                                   ("wait", "wait"), ("communication", "allreduce"))]}
        self.mapping_path = self.root / "mapping.json"
        self.bundle = self.root / "bundle"
        self.events = {"0": [span("schedule", 11, 4, 1), span("runner", 15, 15, 1, push="abc"),
                             span("kernel", 20, 12, tid=2, pop="abc"), span("allreduce", 50, 8, 2)],
                       "1": [span("kernel", 19, 9, 1), span("wait", 27, 8, 1), span("allreduce", 52, 7, 2)]}

    def capture(self):
        for rank, filename in self.spec["traces"].items():
            events = [{"name": "boundary", "ph": "i", "ts": 0}, *self.events[rank],
                      {"name": "boundary", "ph": "i", "ts": 100}]
            data = json.dumps({"traceEvents": events, "rank": rank, "run": self.run_id}).encode()
            (self.root / filename).write_bytes(gzip.compress(data) if filename.endswith("gz") else data)
        report = profile.capture(save(self.root / "spec.json", self.spec), self.bundle)
        self.assertTrue(report["valid"], report)

    def align(self):
        proof = self.root / "calibration.log"
        proof.write_text("synthetic same-source timestamps, both ranks", encoding="utf-8")
        receipt = save(self.root / "clock.json", {
            "run_id": self.run_id, "clock": "same_clock", "ranks": ["0", "1"], "method": "fixture",
            "evidence": {"path": proof.name, "sha256": profile.file_hash(proof)}})
        self.mapping["clock_alignment"] = {"path": receipt.name, "sha256": profile.file_hash(receipt)}

    def analyze(self, **kw):
        save(self.mapping_path, self.mapping)
        return step_timeline.analyze(self.bundle, self.mapping_path, self.run_id, **kw)

    def test_aligned_overlap_long_steps_and_original_evidence(self):
        self.capture()
        self.align()
        report = self.analyze(step_keys={("dp0", "1"), ("dp0", "2")})
        self.assertEqual(report["status"], "observed")
        self.assertEqual(report["coverage"]["attributed_spans"], 7)
        first = report["steps"][0]
        # Per-step spans occupy [11,35) with overlapping host, device, and rank activity.
        self.assertEqual(first["aligned"], {"sum_span_us": "48", "wall_union_us": "24", "overlap_wall_us": "13",
                                           "observed_extent_us": "24", "spans": 5})
        self.assertEqual(first["last_observed_span"]["source"], {"file": "raw/rank-1.json.gz", "event_index": 2})
        self.assertEqual(report["longest_steps"][0]["step_id"], "1")
        consumer = next(s for s in first["longest_spans"] if s["join"]["type"] == "rank_local_correlation")
        self.assertEqual(consumer["join"]["producer_event_index"], 2)
        self.assertEqual(consumer["source"]["event_index"], 3)
        self.assertTrue(report["sources"])

    def test_missing_clock_suppresses_cross_lane_timings(self):
        self.capture()
        report = self.analyze()
        self.assertEqual(report["status"], "partial")
        self.assertIsNone(report["steps"][0]["aligned"])
        self.assertIsNone(report["steps"][0]["last_observed_span"])
        self.assertEqual(report["longest_steps"], [])
        self.assertTrue(report["steps"][0]["lanes"])
        self.assertEqual(report["steps"][0]["stages"]["device"]["sum_span_us"], "21")

    def test_missing_ambiguous_and_conflicting_correlations_stay_unassigned(self):
        self.events["0"].extend([span("runner", 54, 2, 2, push="abc"), span("kernel", 10, 2),
                                 span("runner", 11, 2, 1, push="def"), span("kernel", 12, 2, 2, pop="def")])
        self.capture()
        report = self.analyze(step_keys={("dp0", "1"), ("dp0", "3")})
        counts = report["coverage"]["event_counts"]
        for reason in ("ambiguous_correlation", "missing_step_key", "outside_inventory", "conflicting_step_key"):
            self.assertGreater(counts[reason], 0)
        self.assertEqual(report["coverage"]["unobserved_step_keys"], [("dp0", "3")])

    def test_window_clipping_and_step_replica_identity(self):
        self.mapping["replica_by_rank"]["1"] = "dp1"
        self.events["1"].append(span("kernel", 0, 12, 3))
        self.capture()
        report = self.analyze()
        self.assertEqual(report["coverage"]["observed_steps"], 5)
        clipped = next(s for s in report["steps"] if s["step_id"] == "3")
        self.assertEqual(clipped["lanes"][0]["wall_union_us"], "2")
        self.assertTrue(clipped["longest_spans"][0]["clipped"])

    def test_nested_synchronous_spans_preserve_begin_and_end_references(self):
        self.events["1"].extend([
            {"ph": "B", "pid": 2, "tid": 4, "ts": 60, "name": "runner", "args": {"step": 3}},
            {"ph": "B", "pid": 2, "tid": 4, "ts": 62, "name": "schedule", "args": {"step": 3}},
            {"ph": "E", "pid": 2, "tid": 4, "ts": 65},
            {"ph": "E", "pid": 2, "tid": 4, "ts": 68},
            {"ph": "B", "pid": 2, "tid": 5, "ts": 81, "name": "runner", "args": {"step": 4}},
            {"ph": "E", "pid": 2, "tid": 8, "ts": 82},
        ])
        self.capture()
        record = self.analyze()
        step = next(s for s in record["steps"] if s["step_id"] == "3")
        self.assertEqual(step["lanes"][0]["wall_union_us"], "8")
        self.assertEqual(step["lanes"][0]["sum_span_us"], "11")
        self.assertEqual(step["longest_spans"][0]["source"]["event_index"], 4)
        self.assertEqual(step["longest_spans"][0]["source"]["end_event_index"], 7)
        self.assertEqual(record["coverage"]["event_counts"]["selected_non_x_events"], 1)

    def test_unknown_run_wrong_mapping_and_modified_trace_fail(self):
        self.capture()
        with self.assertRaisesRegex(ValueError, "manifest hash"):
            self.analyze(manifest_sha256="f" * 64)
        with self.assertRaisesRegex(ValueError, "run ID"):
            step_timeline.analyze(self.bundle, self.mapping_path, "other-run")
        self.mapping["tool"] = dict(self.mapping["tool"], version="another-version")
        with self.assertRaisesRegex(ValueError, "tool/version"):
            self.analyze()
        with (self.bundle / "raw/rank-0.json").open("ab") as handle:
            handle.write(b" ")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.analyze()

    def test_unlinked_import_and_invalid_clock_proof(self):
        del self.spec["run_binding"]
        self.capture()
        report = self.analyze()
        self.assertEqual((report["status"], report["steps"]), ("unavailable", []))
        self.align()
        (self.root / "calibration.log").write_text("changed")
        with self.assertRaisesRegex(ValueError, "synchronization evidence"):
            self.analyze()


if __name__ == "__main__":
    unittest.main()

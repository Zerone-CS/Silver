"""Token conservation, window closure and physical duplicate regressions."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diagnose.scripts import step_ledger
from evaluate.tests import test_validate_run as fixtures


class StepLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.run = self.base / "run"
        self.run.mkdir()
        self.writer = fixtures.ValidateRunTest("test_full_window_and_single_request_inflight")
        self.writer.run = self.run
        self.manifest = fixtures.manifest()
        self.manifest.update(run_purpose="diagnostic", profiler_enabled=True)
        self.success = fixtures.request()
        self.failed = dict(self.success, request_id="r2", final_status="timeout", finish_reason=None,
                           actual_output_tokens=8, error="deadline", stream_done_received=False)
        self.writer.write(self.manifest, [self.success, self.failed])
        self.raw = self.base / "steps.csv"
        self.raw.write_text("step,owner\n17,0\n18,0\n19,2\n", encoding="utf-8")
        self.seal = self.base / "counts.json"
        self.seal.write_text(json.dumps({"A": 2, "B": 1}))
        self.source = {"path": str(self.raw), "line": 2}
        base = {"replica": "A", "step_id": "17", "owner_rank": "0", "source": self.source}
        # B has independent logical step IDs from A, as in data-parallel serving.
        second = dict(base, step_id="18", source=dict(self.source, line=3))
        third = dict(base, replica="B", owner_rank="2", source=dict(self.source, line=4))
        self.observations = {
            "schema_version": 1, "run_id": "run-1",
            "manifest_sha256": step_ledger.file_hash(self.run / "manifest.json"),
            "scope": "full_run", "step_definition": "scheduler completion",
            "sources": [{"path": str(p), "sha256": step_ledger.file_hash(p)} for p in [self.raw, self.seal]],
            "inventory": [dict(base, duration_us=12.5), second, third],
            "expected_steps_by_replica": {"A": 2, "B": 1}, "inventory_evidence": {"path": str(self.seal)},
            "work": [dict(base, request_id="r1", prefill_tokens=42, draft_tokens=200, prefix_hit_tokens=2000),
                     dict(base, request_id="r2", num_preemptions=1)],
            "outputs": [dict(base, request_id="r1", output_start=4, output_count=12),
                        dict(base, request_id="r2", output_start=0, output_count=1),
                        dict(second, request_id="r1", output_start=0, output_count=4)],
            "unresolved": ["no calibrated device timestamps"],
        }

    def report(self):
        return step_ledger.build(self.run, self.observations)

    def test_concurrent_time_unique_deliveries_and_independent_replicas(self):
        report = self.report()
        m = report["metrics"]
        self.assertEqual((m["N"], m["T"], m["K"]), (16, 10, 3))
        self.assertAlmostEqual(m["R"] * m["Y_bar"], m["output_token_throughput"])
        self.assertEqual(report["coverage"]["status"], "complete")
        self.assertEqual(report["coverage"]["token_fraction"], 1)
        self.assertEqual(report["coverage"]["observed_steps_by_replica"], {"A": 2, "B": 1})
        self.assertEqual(report["terminal_statuses"], {"success": 1, "timeout": 1})
        self.assertEqual(report["finish_reasons"], {"length": 1})
        self.assertEqual(report["requests"][1]["observed_output_positions"], 1)
        self.assertEqual(report["requests"][1]["counted_output_tokens"], 0)
        self.assertEqual([r["effective_output_tokens_mapped"] for r in report["steps"]], [12, 4, 0])
        first = report["steps"][0]
        self.assertEqual(first["observed_request_ids"], ["r1", "r2"])
        self.assertEqual(first["work_observed_sums"]["prefix_hit_tokens"], 2000)
        self.assertIsNone(first["work_observed_sums"]["recomputed_tokens"])
        self.assertEqual(first["work_reporting_rows"]["num_preemptions"], 1)
        self.assertEqual(report["unresolved"], ["no calibrated device timestamps"])

    def test_partial_window_or_gaps_preserve_external_measurement(self):
        self.observations["outputs"].pop()
        self.observations["scope"] = "partial"
        report = self.report()
        self.assertEqual(report["metrics"]["output_token_throughput"], 1.6)
        self.assertEqual(report["coverage"]["mapped_output_tokens"], 12)
        self.assertEqual(report["coverage"]["unmapped_output_tokens"], 4)
        self.assertEqual(report["coverage"]["token_fraction"], .75)
        self.assertEqual(report["coverage"]["status"], "partial")
        for key in ["K", "R", "Y_bar"]:
            self.assertIsNone(report["metrics"][key])
        # Full-run declarations cannot compensate for a missing interval.
        self.observations["scope"] = "full_run"
        self.assertIsNone(self.report()["metrics"]["K"])

    def test_missing_inventory_and_a_missing_zero_output_step(self):
        base = copy.deepcopy(self.observations)
        for field in ["inventory_evidence", "expected_steps_by_replica"]:
            self.observations = copy.deepcopy(base)
            del self.observations[field]
            report = self.report()
            self.assertEqual(report["coverage"]["token_fraction"], 1)
            self.assertIsNone(report["metrics"]["K"])
        self.observations = base
        self.observations["inventory"].pop()
        report = self.report()
        self.assertEqual(report["coverage"]["status"], "partial")
        self.assertEqual(report["coverage"]["observed_steps"], 2)
        self.assertIsNone(report["metrics"]["Y_bar"])

    def test_duplicated_logical_work_or_token_positions_rejected(self):
        base = copy.deepcopy(self.observations)
        replacements = [
            ("inventory", dict(base["inventory"][0], owner_rank="1"), "duplicate logical"),
            ("outputs", dict(base["outputs"][0], output_start=10, output_count=2), "overlapping"),
            ("outputs", dict(base["outputs"][0], owner_rank="1"), "owner"),
            ("outputs", dict(base["outputs"][0], output_start=16, output_count=1), "exceeds"),
            ("outputs", dict(base["outputs"][0], request_id="missing"), "unknown benchmark"),
            ("outputs", dict(base["outputs"][0], step_id="missing"), "absent logical step"),
            ("work", base["work"][0], "duplicate request participation"),
        ]
        for name, record, error in replacements:
            with self.subTest(name=name, error=error):
                self.observations = copy.deepcopy(base)
                self.observations[name].append(record)
                with self.assertRaisesRegex(ValueError, error):
                    self.report()

    def test_zero_successful_output_has_no_fraction_and_no_invented_tokens(self):
        zero = dict(self.success, actual_output_tokens=0, first_output_ts=None, last_output_ts=None,
                    output_event_ts=[], finish_reason="stop")
        self.writer.write(self.manifest, [zero, self.failed])
        self.observations["manifest_sha256"] = step_ledger.file_hash(self.run / "manifest.json")
        self.observations["outputs"] = []
        report = self.report()
        self.assertEqual((report["metrics"]["N"], report["metrics"]["Y_bar"]), (0, 0))
        self.assertIsNone(report["coverage"]["token_fraction"])
        self.assertEqual(report["length_distribution"], [{"output_tokens": 0, "successful_requests": 1}])

    def test_receipts_binding_and_independent_count(self):
        base = copy.deepcopy(self.observations)
        for field, replacement, regex in [
            ("run_id", "wrong", "another run"),
            ("manifest_sha256", "0" * 64, "another run"),
            ("inventory_evidence", self.source, "independent"),
            ("expected_steps_by_replica", {"A": 1, "B": 1}, "exceeds"),
        ]:
            with self.subTest(field=field):
                self.observations = dict(base, **{field: replacement})
                with self.assertRaisesRegex(ValueError, regex):
                    self.report()
        self.raw.write_text("changed")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            step_ledger.build(self.run, base)

    def test_bench_only_and_reject_unprofiled_run(self):
        report = step_ledger.build(self.run)
        self.assertEqual(report["coverage"]["status"], "unavailable")
        self.assertIsNone(report["metrics"]["K"])
        self.assertEqual(report["metrics"]["N"], 16)
        self.manifest.update(profiler_enabled=False, run_purpose="performance")
        self.writer.write(self.manifest)
        with self.assertRaisesRegex(ValueError, "diagnostic"):
            step_ledger.build(self.run)


if __name__ == "__main__":
    unittest.main()

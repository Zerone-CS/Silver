"""Step identifiers are local to a run; only comparable output positions can be paired."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.step_analysis import compare, markdown_pair


def ledger(run_id, step_id, duration, ranges):
    step = {"replica": "r0", "step_id": step_id, "duration_us": duration,
            "source": {"path": run_id, "line": 2}, "effective_output_tokens_mapped": 4,
            "output_ranges": [{"request_id": "r", "output_start": start, "output_count": count,
                               "counted_in_N": True} for start, count in ranges]}
    identity = dict.fromkeys(("workload_sha256", "environment_sha256", "metric_contract_sha256",
                              "benchmark_sha256", "client_measurement_policy_sha256",
                              "scenario", "measurement_mode", "model", "base_url"), "same")
    return {"run_id": run_id, "manifest_sha256": run_id, "step_definition": "completed schedule",
            "steps": [step], "identity": identity, "profile": None, "goodput": None, "unresolved": [],
            "metrics": {"N": 4, "T": 2.0, "output_token_throughput": 2.0, "K": 1, "R": 0.5, "Y_bar": 4,
                        "n_requests": 1, "n_success": 1, "n_failed": 0, "failure_rate": 0,
                        "ttft_p99_s": 0.25, "e2e_p99_s": 1.0},
            "coverage": {"status": "complete", "token_fraction": 1.0}}


class StepComparisonTests(unittest.TestCase):
    def setUp(self):
        self.a = ledger("run-A", "2", 10000, [(0, 2), (2, 2)])
        self.b = ledger("run-B", "93", 8000, [(0, 4)])

    def test_match_output_positions_across_record_boundaries_and_unrelated_step_ids(self):
        pair = compare(self.a, self.b)
        self.assertEqual(pair["matched_step_count"], 1)
        change = pair["largest_matched_step_duration_changes"][0]
        self.assertEqual(change["delta_us"], -2000)
        self.assertEqual(change["candidate"]["source"], {"path": "run-B", "line": 2})
        text = markdown_pair({"baseline": self.a, "candidate": self.b, "comparison": pair})
        self.assertIn("| r0 / 2 | r0 / 93 | -2000 |", text)

    def test_partial_ledger_or_changed_step_definition_cannot_generate_step_deltas(self):
        incomplete = deepcopy(self.b)
        incomplete["coverage"]["status"] = "partial"
        different = deepcopy(self.b)
        different["step_definition"] = "completed runner invocation"
        for candidate in (incomplete, different):
            self.assertEqual(compare(self.a, candidate)["matched_step_count"], 0)
        self.assertIsNone(compare(self.a, different)["rows"]["R"]["delta"])

    def test_same_ordinal_with_different_request_positions_does_not_match(self):
        self.b["steps"][0]["step_id"] = self.a["steps"][0]["step_id"]
        self.b["steps"][0]["output_ranges"][0]["request_id"] = "different"
        self.assertEqual(compare(self.a, self.b)["matched_step_count"], 0)

    def test_changed_case_and_identical_run_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "independent runs"):
            compare(self.a, self.a)
        self.b["identity"]["environment_sha256"] = "changed"
        with self.assertRaisesRegex(ValueError, "case mismatch"):
            compare(self.a, self.b)

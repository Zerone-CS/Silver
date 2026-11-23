from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts import experience
from scripts import experience_study as study


class ExperienceStudyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        self.study = self.work / "study"
        self.library = self.work / "history.sqlite"
        self.original_workload = [
            {"request_id": "case-01", "prompt_text": "Copy the shipping label A", "output_tokens": 8},
        ]
        self.write("workload.jsonl", self.original_workload)
        self.write("brief.json", {"baseline_goodput_tps": 12.0})
        self.spec = {
            "schema_version": 1, "seed": 57, "agent_model": "same-agent", "tools": ["experience.search"],
            "agent_settings": {"reasoning": "medium"}, "budget_npu_seconds": 720, "max_candidates": 2,
            "trajectories_per_arm": 1, "families": [{
                "id": "packing", "workload": "workload.jsonl", "brief": "brief.json",
                "context": {"hardware": "910C", "workload_family": "packing"},
                "candidates": ["seq8", "apc"], "baseline_goodput_tps": 12.0,
                "baseline_npu_seconds": 100, "minimum_next_npu_seconds": 90,
                "minimum_confirm_npu_seconds": 200,
            }],
        }
        self.history_workload = "a" * 64
        self.interval = 0

    def write(self, name, value):
        path = self.work / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def call(self, command, *args, trajectory=None):
        output = io.StringIO()
        flags = [command, "--study", str(self.study), *(str(arg) for arg in args)]
        if trajectory is not None:
            flags.extend(("--trajectory", trajectory))
        with redirect_stdout(output):
            study.main(flags)
        return json.loads(output.getvalue())

    def archive_history(self):
        record = {"campaign_id": "historical", "round": 1, "decision": "adopt",
                  "scope": "an earlier workload", "scenario": "prior",
                  "evidence": {"assessments": [{
                      "case_identity": {"prior": {"workload_sha256": self.history_workload}},
                      "recommendation": "adopt", "comparison": {"input_valid": True, "recommendation": "adopt"}
                  }]}}
        experience.save(self.library, self.work, record, {}, {"hardware": "910C"})

    def init(self):
        self.archive_history()
        self.call("init", "--spec", self.write("spec.json", self.spec), "--library", self.library)
        return {row["arm"]: row["trajectory"] for row in self.call("dispatch")["assignments"]}

    def choice(self, trajectory, candidate, used=()):
        return self.call("choose", "--choice", self.write("choice.json", {
            "candidate": candidate, "record_ids_used": list(used), "reasoning": "Choose carefully",
            "prediction": "Effective tokens will change", "falsifier": "No measured change",
        }), trajectory=trajectory)

    def receipt(self, candidate, stage, decision, seconds, run_id, goodput=45.0, overlapping=False):
        name = "receipt-" + run_id
        start = 10 if overlapping else self.interval
        end = start + (seconds * 10 ** 9)
        self.interval = max(end, self.interval)
        charge = self.write(name + "-charge.json", {
            "start_mono_ns": start, "released_mono_ns": end, "seconds": seconds,
            "real_card_cleanup": True, "measurements": [{"run_id": run_id}],
        })
        assessment = self.write(name + "-assessment.json", {
            "phase": stage, "recommendation": decision, "goodput_tps_median": goodput,
            "guards": {"throughput": "passed"},
        })
        return self.write(name + ".json", {
            "schema_version": 1, "candidate": candidate, "stage": stage, "decision": decision,
            "goodput_tps": goodput, "charge": charge.name, "assessment": assessment.name,
        })

    def verify_rejection(self, function, *args, **kwargs):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            function(*args, **kwargs)
        self.assertEqual(exc.exception.code, 2)

    def test_blind_packets_confirmed_benefit_and_censoring(self):
        ids = self.init()
        history = self.call("packet", trajectory=ids["history"])
        control = self.call("packet", trajectory=ids["control"])
        self.assertEqual(history, self.call("packet", trajectory=ids["history"]))
        self.assertEqual(set(history), set(control))
        self.assertNotIn("arm", history)
        self.assertEqual(control["experience.search"]["records"], [])
        record = history["experience.search"]["records"][0]["record_id"]
        self.verify_rejection(self.choice, ids["control"], "seq8", [record])
        self.choice(ids["history"], "seq8")  # The treatment may decline to use retrieved history.
        self.choice(ids["control"], "seq8")
        invalid_confirm = self.receipt("seq8", "confirm", "adopt", 100, "early")
        self.verify_rejection(self.call, "observe", "--receipt", invalid_confirm, trajectory=ids["history"])
        screen = self.receipt("seq8", "screen", "adopt", 101, "H-screen")
        seen = self.call("observe", "--receipt", screen, trajectory=ids["history"])
        self.assertEqual(seen, self.call("observe", "--receipt", screen, trajectory=ids["history"]))
        overlapping = self.receipt("seq8", "screen", "revert", 120, "overlap", overlapping=True)
        self.verify_rejection(self.call, "observe", "--receipt", overlapping, trajectory=ids["control"])
        reject = self.receipt("seq8", "screen", "revert", 101, "C-screen")
        self.call("observe", "--receipt", reject, trajectory=ids["control"])
        followup = self.call("packet", trajectory=ids["control"])
        self.assertEqual(followup["results"][0]["assessment_feedback"], {"guards": {"throughput": "passed"}})
        self.choice(ids["control"], "stop")
        confirm = self.receipt("seq8", "confirm", "adopt", 181, "H-confirm", goodput=42.0)
        self.call("observe", "--receipt", confirm, trajectory=ids["history"])
        self.call("export", "--output", self.work / "out")
        report = json.loads((self.work / "out" / "report.json").read_text())
        rows = {row["arm"]: row for row in report["trajectories"]}
        self.assertEqual(rows["history"]["first_success_npu_seconds"], 382)
        self.assertEqual(rows["history"]["final_goodput_tps"], 42.0)
        self.assertEqual(rows["history"]["records_returned"], [record])
        self.assertEqual(rows["history"]["records_cited"], [])
        self.assertIsNone(rows["control"]["first_success_npu_hours"])
        self.assertTrue(rows["control"]["right_censored"])
        self.assertEqual(rows["control"]["failed_candidates"], 1)
        self.assertEqual(rows["control"]["final_goodput_tps"], 12.0)
        pair = report["paired_comparisons"][0]
        self.assertTrue(pair["same_candidate_sequence"])
        self.assertIsNone(pair["delta_first_success_npu_seconds"])
        self.assertEqual(pair["delta_final_goodput_tps"], 30.0)

    def test_historical_workloads_cannot_be_reused(self):
        self.history_workload = hashlib.sha256(experience.encode(self.original_workload).encode()).hexdigest()
        self.archive_history()
        self.verify_rejection(self.call, "init", "--spec", self.write("spec.json", self.spec), "--library", self.library)
        self.assertFalse(self.study.exists())

    def test_missing_confirmation_budget_is_recorded(self):
        self.spec["budget_npu_seconds"] = 300
        ids = self.init()
        using = ids["history"]
        self.call("packet", trajectory=using)
        self.choice(using, "seq8")
        screen = self.receipt("seq8", "screen", "adopt", 100, "independent-screen")
        self.call("observe", "--receipt", screen, trajectory=using)
        self.call("finish", "--reason", "budget", "--note", "confirmation cannot start", trajectory=using)
        only = next(row for row in self.call("report")["trajectories"] if row["trajectory"] == using)
        self.assertEqual(only["stop_reason"], "budget")
        self.assertTrue(only["right_censored"])
        self.assertEqual(only["failed_candidates"], 0)

    def test_multilease_stage_charges_only_actual_intervals(self):
        ids = self.init()
        trajectory = ids["history"]
        self.call("packet", trajectory=trajectory)
        self.choice(trajectory, "seq8")
        clock = {"host": "ascend", "boot_id": "boot-1", "devices": ["davinci13"]}
        leases = []
        for index, start in enumerate((100, 300)):
            proof = self.write(f"release-{index}.json", {
                **clock, "free": True, "observed_mono_ns": (start + 29) * 10 ** 9,
            })
            lease = self.write(f"action-{index}.json", {
                **clock, "start_mono_ns": start * 10 ** 9, "end_mono_ns": (start + 30) * 10 ** 9,
                "seconds": 30, "real_card_cleanup": True,
                "proof_sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
                "proof_path": "/original/site/" + proof.name,
            })
            leases.append(lease.name)
        aggregate = self.write("aggregate.json", {
            "leases": leases, "seconds": 60, "measurements": [{"run_id": "candidate-own"}],
        })
        decision = self.write("decision.json", {"recommendation": "defer", "goodput_tps_median": 21})
        observation = self.write("observation.json", {"schema_version": 1, "candidate": "seq8", "stage": "screen",
                                 "decision": "defer", "goodput_tps": 21, "charge": aggregate.name,
                                 "assessment": decision.name})
        collected = self.call("observe", "--receipt", observation, trajectory=trajectory)
        self.assertEqual(collected["npu_seconds"], 60)
        self.assertEqual(collected["site_clock"], clock)


if __name__ == "__main__":
    unittest.main()

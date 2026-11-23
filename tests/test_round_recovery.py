"""Replay an interrupted formal batch through the public Round CLI."""

import hashlib
import json
from pathlib import Path
import unittest

if __package__:
    from . import test_campaign_integration as integration
else:
    import test_campaign_integration as integration


class RoundRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = integration.CampaignIntegrationTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def trial(self, name):
        root = self.fixture.root
        pairs = [
            {"scenario": "offline", "baseline": str(root / "baseline" / f"{name}-{i}"),
             "candidate": str(root / "candidate" / f"{name}-{i}")}
            for i in range(2)
        ]
        return integration.write(root / (name + ".json"), {"schema_version": 1, "pairs": pairs})

    def stamp(self, target):
        output = target.with_suffix(".metadata.json")
        self.fixture.campaign_cmd("stamp", "--round", 1, "--metadata", target.parent / "performance.json",
                                  "--run-dir", target, "--output", output)
        return output

    def test_partial_batch_replaced_and_complete_unfavorable_batch_must_be_assessed(self):
        f = self.fixture
        # Make the measured outcome unambiguously unfavorable, even if throughput improves.
        contract = json.loads(f.contract.read_text())
        contract["scenarios"]["offline"]["guardrails"].append({"metric": "e2e_p99_s", "max_value": 0.0})
        integration.write(f.contract, contract)
        f.begin("--max-retries", 2)
        f.profile("baseline")
        change = f.root / "change.txt"
        change.write_text("Set delay to zero. Restore baseline delay when the latency guardrail fails.")
        f.campaign_cmd("plan", "--round", 1, "--hypothesis", "the mock delay limits throughput",
                       "--prediction", "completion is faster", "--config", f.root / "candidate/config.json",
                       "--change", change)
        f.profile("candidate")

        original = self.trial("first")
        original_bytes = original.read_bytes()
        f.campaign_cmd("schedule", "--round", 1, "--evidence", original)
        first_pair = json.loads(original_bytes)["pairs"][0]
        partial = Path(first_pair["baseline"])
        metadata = self.stamp(partial).read_bytes()
        partial.mkdir()
        (partial / "metadata.json").write_bytes(metadata)
        # The collector stopped before publishing its final manifest.
        overlap = self.trial("overlap")
        pairs = json.loads(overlap.read_bytes())
        pairs["pairs"][0]["candidate"] = first_pair["candidate"]  # old, as-yet uncreated slot
        integration.write(overlap, pairs)
        reason = "collector interrupted before final manifest"
        error = f.campaign_cmd("retry", "--round", 1, "--reason", reason, "--evidence", overlap, fail=True)
        self.assertIn("new directories", error)
        self.assertEqual(f.campaign_cmd("status")["active"]["schedule"]["path"], str(original.resolve()))

        resumed = self.trial("resumed")
        state = f.campaign_cmd("retry", "--round", 1, "--reason", reason, "--evidence", resumed)
        self.assertEqual(state["next_action"], "assess")
        self.assertEqual(state["active"]["schedule"]["path"], str(resumed.resolve()))
        # Even on the second retry, the first batch's empty slots remain retired.
        f.campaign_cmd("retry", "--round", 1, "--reason", "worker failed to start",
                       "--evidence", overlap, fail=True)
        self.assertEqual(f.campaign_cmd("status")["active"]["schedule"]["path"], str(resumed.resolve()))
        for pair in json.loads(resumed.read_text())["pairs"]:
            for side in ("candidate", "baseline"):
                target = Path(pair[side])
                f.collect(side, target.name, metadata=self.stamp(target))

        error = f.campaign_cmd("retry", "--round", 1, "--reason", "request another sample",
                               "--evidence", self.trial("unused"), fail=True)
        self.assertIn("record its measured outcome", error)
        self.assertEqual(f.campaign_cmd("status")["active"]["schedule"]["path"], str(resumed.resolve()))
        self.assertEqual(f.campaign_cmd("assess", "--round", 1, "--evidence", resumed)["next_action"], "restore")
        # The last collection above left the fixture serving baseline.
        health = f.health("baseline", "restored.json")
        f.campaign_cmd("close", "--round", 1, "--decision", "revert", "--observation", "latency guardrail failed",
                       *health)
        evidence = f.campaign_cmd("export", "--round", 1)["evidence"]
        self.assertEqual(len(evidence["assessments"]), 1)
        self.assertEqual(evidence["assessments"][0]["recommendation"], "revert")
        self.assertEqual(len(evidence["invalid_attempts"]), 1)
        attempt = evidence["invalid_attempts"][0]
        self.assertEqual(attempt["reason"], reason)
        self.assertIn("manifest.json", attempt["error"])
        self.assertIn(str(partial.resolve()), attempt["paths"])
        self.assertEqual(attempt["schedule"], {"path": str(original.resolve()),
                                              "sha256": hashlib.sha256(original_bytes).hexdigest()})
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual((partial / "metadata.json").read_bytes(), metadata)


if __name__ == "__main__":
    unittest.main()

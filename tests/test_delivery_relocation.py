"""Replay original formal evidence after removing its source directory."""

import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import evidence_bundle
from scripts import ascend_910c
import test_round_evidence as fixture


class DeliveryRelocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.RoundEvidenceTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixture.RoundEvidenceTests.tearDownClass()

    def test_full_round_replays_after_original_path_disappears(self):
        original = fixture.RoundEvidenceTests()
        original.setUp()
        self.addCleanup(original.doCleanups)
        original.goodput_contract()
        original.build()
        base = original.root.resolve()
        base_id, candidate_id = [
            fixture.save(base / (side + "-identity.json"), ident)
            for side, ident in zip(("baseline", "candidate"), original.identities)
        ]
        expected = fixture.save(base / "assessment.json", original.evaluate())
        run_list = fixture.load(original.evidence)
        for pair in run_list["pairs"]:
            for side in ("baseline", "candidate"):
                pair[side] = str(base / pair[side])
        fixture.save(original.evidence, run_list)
        spec = {
            "schema_version": 1, "roots": [{"origin": str(base), "source": str(base)}],
            "checks": [{"name": "qualification", "contract": str(original.contract.resolve()),
                        "pairs": str(original.evidence.resolve()), "baseline_identity": str(base_id),
                        "candidate_identity": str(candidate_id), "expected_report": str(expected)}],
        }
        with TemporaryDirectory() as temporary, patch.object(evidence_bundle, "assessor_identity",
                                                                return_value={"release_id": "fixture"}):
            dest = Path(temporary).resolve()
            config = fixture.save(dest / "pack.json", spec)
            alias = dest / "source-alias"
            alias.symlink_to(base, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "invalid evidence copy source"):
                evidence_bundle.pack(config, alias / "nested-bundle")
            summary = evidence_bundle.pack(config, dest / "bundle")
            self.assertEqual(summary["checks"], [{"name": "qualification", "recommendation": "adopt", "runs": 4}])
            hidden = base.with_name(base.name + "-hidden")
            base.rename(hidden)
            try:
                moved = dest / "other" / "archive"
                moved.parent.mkdir()
                shutil.move(dest / "bundle", moved)
                self.assertEqual(evidence_bundle.replay(moved), summary)
                contract = moved / "payload/root-1/contract.json"
                contract.write_bytes(contract.read_bytes() + b"\n")
                with self.assertRaisesRegex(ValueError, "archive bytes changed"):
                    evidence_bundle.replay(moved)
            finally:
                hidden.rename(base)


class ReservationTests(unittest.TestCase):
    def test_mapped_devices_are_rejected_even_before_engine_starts(self):
        def container(name, devices, environment, runtime):
            return {"Name": name, "State": {"Running": True}, "Mounts": [],
                    "HostConfig": {"Devices": [{"PathOnHost": dev} for dev in devices], "Runtime": runtime},
                    "Config": {"Env": environment}}
        records = [
            container("/mapped", ["/dev/davinci13"], [], "runc"),
            container("/range", [], ["ASCEND_VISIBLE_DEVICES=0-7,10"], "ascend"),
            container("/other", ["/dev/davinci12"], [], "runc"),
        ]
        with patch.object(ascend_910c, "cmd", side_effect=lambda *args: json.dumps(records) if args[1] == "inspect" else "x y"):
            with self.assertRaisesRegex(RuntimeError, "/mapped"):
                ascend_910c.verify_container_reservation(type("Site", (), {"die": 13}))
            with self.assertRaisesRegex(RuntimeError, "/range"):
                ascend_910c.verify_container_reservation(type("Site", (), {"die": 10}))
            ascend_910c.verify_container_reservation(type("Site", (), {"die": 11}))


if __name__ == "__main__":
    unittest.main()

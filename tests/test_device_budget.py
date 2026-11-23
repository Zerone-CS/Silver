"""Check budget admission and device release provenance around recovery."""

import json
from pathlib import Path
import tempfile
import unittest

from scripts.device_budget import Budget


class DeviceBudgetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.budget = Budget.initialize(self.root / "budget.sqlite", 100)

    def proof(self, name, at, **patch):
        path = self.root / name
        report = {"host": "ascend", "boot_id": "boot-one", "devices": ["die0"],
                  "observed_mono_ns": at * 10**9, "free": True, **patch}
        path.write_text(json.dumps(report))
        return path

    def claim(self, name, at, maximum, **config):
        args = {"host": "ascend", "boot_id": "boot-one", "devices": ["die0"], **config}
        return self.budget.claim(name, args["host"], args["boot_id"], args["devices"],
                                 at * 10**9, maximum, args.get("reserve_seconds", 0),
                                 args.get("cleanup", False))

    def test_reservation_and_reconciliation(self):
        with self.assertRaisesRegex(ValueError, "insufficient"):
            self.claim("too-expensive", 1, 81, reserve_seconds=20)
        self.assertFalse(self.claim("trial", 2, 80, reserve_seconds=20)["existing"])
        self.assertTrue(self.claim("trial", 2, 80, reserve_seconds=20)["existing"])
        with self.assertRaisesRegex(ValueError, "different claim"):
            self.claim("trial", 3, 80, reserve_seconds=20)
        with self.assertRaisesRegex(ValueError, "reconcile"):
            self.claim("another", 4, 10)
        with self.assertRaisesRegex(ValueError, "same host boot"):
            self.budget.release("trial", 15 * 10**9,
                                self.proof("wrong.json", 12, boot_id="boot-two"), "recovered")
        with self.assertRaisesRegex(ValueError, "not observed during"):
            self.budget.release("trial", 15 * 10**9, self.proof("future.json", 16), "recovered")
        proof = self.proof("released.json", 14)
        receipt = self.budget.release("trial", 15 * 10**9, proof, "recovered")
        self.assertEqual(receipt["die_seconds"], 13)
        self.assertEqual(self.budget.release("trial", 15 * 10**9, proof, "recovered"), receipt)
        with self.assertRaisesRegex(ValueError, "already recorded"):
            self.budget.release("trial", 16 * 10**9, proof, "recovered")
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.claim("older", 14, 20)
        audited = self.budget.audit()
        self.assertEqual(audited["remaining_die_seconds"], 87)
        self.assertEqual(audited["pending"], [])
        proof.write_text("{}")
        with self.assertRaisesRegex(ValueError, "proof changed"):
            self.budget.audit()

    def test_cleanup_receipt_counts_actual_multi_die_overrun(self):
        self.claim("trial", 1, 45, devices=["die1", "die0"], reserve_seconds=10)
        receipt = self.budget.release(
            "trial", 53 * 10**9, self.proof("clean.json", 52, devices=["die0", "die1"]), "timed_out"
        )
        self.assertTrue(receipt["action_overrun"])
        self.assertEqual(receipt["die_seconds"], 104)
        with self.assertRaisesRegex(ValueError, "insufficient"):
            self.claim("new", 55, 1)
        self.claim("clean", 55, 10, cleanup=True)
        self.budget.release("clean", 59 * 10**9, self.proof("restored.json", 58), "restored")
        self.assertEqual(self.budget.audit()["overrun_die_seconds"], 8)

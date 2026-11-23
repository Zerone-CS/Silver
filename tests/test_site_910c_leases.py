"""Exercise the site worker's lease lifecycle with an isolated synthetic service."""

from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import site_910c
from scripts.device_budget import Budget


class FakeSite:
    name = "fixture-site"

    def __init__(self, root):
        self.root = root
        self.settings = SimpleNamespace(die=13)
        self.active = None
        self.was_interrupted = False
        (root / "deployments").mkdir()

    @contextmanager
    def deployment(self, key, action):
        (self.root / "deployments" / action).mkdir()
        self.active = action
        try:
            yield SimpleNamespace(key=key)
        finally:
            if not self.was_interrupted:
                self.active = None

    def stop(self, action, directory):
        if self.active is not None:
            assert action == self.active and directory.name == action
            self.active = None


class SiteLeaseTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.site = FakeSite(self.root)
        self.budget = Budget.initialize(self.root / "device.sqlite", 500)
        mocks = [
            patch.object(site_910c, "lock_card", side_effect=lambda _: nullcontext()),
            patch.object(site_910c, "machine", return_value=("fixture-host", "fixture-boot", ["davinci13"])),
            patch.object(site_910c.host, "verify_container_reservation"),
            patch.object(site_910c.host, "card_snapshot", return_value={"processes_on_card": {}}),
            patch.object(site_910c.host, "optional_container", side_effect=lambda _: self.site.active),
        ]
        for mock in mocks:
            mock.start()
            self.addCleanup(mock.stop)

    def request(self, action):
        return SimpleNamespace(action=action, config_key="A", scenario="fixture", budget=self.budget.path,
                               max_seconds=30, cleanup_seconds=10, reserve_die_seconds=30)

    def test_success_records_measurement_and_verifiable_release(self):
        receipt = site_910c.with_device(self.site, self.request("ordinary"), lambda live: {"config": live.key})
        self.assertEqual(receipt["measurement"], {"config": "A"})
        self.assertIsNone(receipt["error"])
        ledger = self.budget.audit()
        self.assertEqual(ledger["pending"], [])
        self.assertGreater(ledger["used_die_seconds"], 0)

    def test_interrupted_action_blocks_admission_until_exclusive_reconciliation(self):
        def interrupt(_):
            self.site.was_interrupted = True
            raise RuntimeError("worker interrupted with deployment still live")

        with self.assertRaisesRegex(RuntimeError, "deployment still exists"):
            site_910c.with_device(self.site, self.request("first"), interrupt)
        self.assertEqual(self.budget.status()["pending"], ["first"])
        with self.assertRaisesRegex(RuntimeError, "needs reconciliation"):
            site_910c.with_device(self.site, self.request("second"), lambda _: {})
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            site_910c.reconcile(self.site, self.budget, "other")
        recovered = site_910c.reconcile(self.site, self.budget, "first")
        self.assertEqual(recovered["outcome"], "reconciled_interruption")
        self.site.was_interrupted = False
        second = site_910c.with_device(self.site, self.request("second"), lambda _: {})
        self.assertEqual(second["outcome"], "collected")
        ledger = self.budget.audit()
        self.assertEqual(ledger["pending"], [])
        self.assertEqual(len(ledger["leases"]), 2)
        first, second = ledger["leases"]
        self.assertLessEqual(first["end_mono_ns"], second["start_mono_ns"])

    def test_profile_descendant_cannot_write_after_site_deadline(self):
        spec = self.root / "profile.json"
        spec.write_text(json.dumps({
            "schema_version": 1, "format": "chrome-trace-v1", "tool": {"name": "test", "version": "1"},
            "diagnostic_run": "diagnostic", "traces": {"0": "trace.json"},
            "rank_field": {"scope": "event", "path": "args.rank"},
            "window": {"clock": "fixture", "mode": "trace_extent", "min_duration_us": 1},
            "requirements": [{"id": "fixture", "field": "name", "pattern": "op", "min_count": 1}],
        }), encoding="utf-8")
        started = self.root / "child-started"
        escaped = self.root / "child-escaped"
        script = self.root / "child.py"
        script.write_text("import time\nfrom pathlib import Path\n"
                          f"Path({str(started)!r}).touch()\ntime.sleep(1)\n"
                          f"Path({str(escaped)!r}).touch()\n", encoding="utf-8")
        with self.assertRaises(TimeoutError), site_910c.work_deadline(0.45):
            site_910c.invoke(ROOT / "diagnose/scripts/profile.py", "capture", "--spec", spec,
                             "--out", self.root / "bundle", "--", sys.executable, "-B", script)
        self.assertTrue(started.is_file())
        time.sleep(0.75)
        self.assertFalse(escaped.exists())


if __name__ == "__main__":
    unittest.main()

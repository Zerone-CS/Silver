"""Check the cross-batch timing gate using synthetic measurement windows."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluate.scripts import benchmark
from scripts.trial_plan import confirmation_order


def batch(*intervals, host="client", boot="one"):
    return {"slots": [{"order_window": {"clock": "host_monotonic_ns", "hostname": host, "boot_id": boot,
                                        "start_ns": start, "end_ns": end}} for start, end in intervals]}


class ConfirmationClockTests(unittest.TestCase):
    def test_whole_screen_must_finish_before_confirmation(self):
        screen = batch((2, 4), (10, 14))
        self.assertEqual(confirmation_order(screen, batch((14, 16), (17, 20)), required=True), "verified")
        for confirm in (batch((1, 2)), batch((4, 8), (15, 16)), batch((12, 15))):
            with self.subTest(confirm=confirm), self.assertRaisesRegex(ValueError, "screen must finish"):
                confirmation_order(screen, confirm, required=True)

    def test_timestamps_from_separate_boots_or_clients_cannot_be_ordered(self):
        screen = batch((10, 20))
        for confirm in (batch((25, 30), boot="two"), batch((25, 30), host="another")):
            with self.subTest(confirm=confirm), self.assertRaisesRegex(ValueError, "clock domains"):
                confirmation_order(screen, confirm, required=True)

    def test_legacy_missing_clock_identity_is_explicit(self):
        for screen in (batch((1, 2), host=None, boot=None), {"slots": [{}]}, None):
            with self.subTest(screen=screen):
                confirm = batch((3, 4))
                self.assertEqual(confirmation_order(screen, confirm, required=False), "unverified")
                with self.assertRaisesRegex(ValueError, "identified clock"):
                    confirmation_order(screen, confirm, required=True)

    def test_collector_identifies_linux_boot_or_leaves_domain_unknown(self):
        with patch.object(sys, "platform", "linux"), patch("socket.gethostname", return_value="client"):
            with patch.object(Path, "read_text", return_value="test-boot\n"):
                self.assertEqual(benchmark.client_clock_domain(),
                                 {"clock": "host_monotonic_ns", "hostname": "client", "boot_id": "test-boot"})
            with patch.object(Path, "read_text", side_effect=FileNotFoundError):
                self.assertIsNone(benchmark.client_clock_domain())
        with patch.object(sys, "platform", "darwin"):
            self.assertIsNone(benchmark.client_clock_domain())

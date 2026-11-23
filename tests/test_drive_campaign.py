import fcntl
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts/drive_campaign.py"
CONTROL = ROOT / "scripts/campaign.py"


class DriveCampaignTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.campaign = self.create_campaign("campaign")
        self.lock = self.root / "devices.lock"
        self.calls = self.root / "worker.jsonl"
        self.worker = self.root / "worker.py"
        self.worker.write_text(textwrap.dedent("""\
            import json
            from pathlib import Path
            import subprocess
            import sys
            import time

            mode, log, control = sys.argv[1:]
            status = json.load(sys.stdin)
            with open(log, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(status) + "\\n")
            if mode == "hold":
                release = Path(log).with_suffix(".release")
                until = time.monotonic() + 4
                while not release.exists() and time.monotonic() < until:
                    time.sleep(0.02)
            if mode in ("stop", "hold"):
                subprocess.run([sys.executable, "-B", control, "stop", "--campaign",
                                status["campaign"], "--reason", "explicit fixture stop"], check=True)
            elif mode == "timeout":
                marker = str(Path(log).with_suffix(".late"))
                subprocess.Popen([sys.executable, "-c",
                                  "import sys,time,pathlib; time.sleep(0.9); pathlib.Path(sys.argv[1]).touch()",
                                  marker])
                print("child started", flush=True)
                time.sleep(20)
            elif mode == "failed":
                sys.exit(7)
            """), encoding="utf-8")

    def create_campaign(self, name, deadline=None):
        folder = self.root / name
        folder.mkdir()
        state = {"campaign_id": name, "count": 0, "max_rounds": 2,
                 "deadline": deadline, "stopped": None, "stop_requested": None,
                 "baseline": {"config_sha256": "a" * 64, "implementation_sha256": None},
                 "deployed": None}
        db = sqlite3.connect(folder / "campaign.sqlite")
        try:
            db.executescript("CREATE TABLE campaign(id INTEGER PRIMARY KEY, state TEXT);"
                             "CREATE TABLE rounds(id INTEGER PRIMARY KEY, data TEXT);"
                             "CREATE TABLE events(id INTEGER PRIMARY KEY, at REAL, round INTEGER, action TEXT, data TEXT);")
            db.execute("INSERT INTO campaign VALUES(1,?)", (json.dumps(state),))
            db.commit()
        finally:
            db.close()
        return folder

    def argv(self, mode, *, campaign=None, timeout=5, lock=None, resume=None):
        cmd = [sys.executable, "-B", str(DRIVER), "--campaign", str(campaign or self.campaign),
               "--resource-lock", str(lock or self.lock), "--worker-timeout", str(timeout)]
        if resume is not None:
            cmd.extend(["--resume-after", resume])
        return [*cmd, "--", sys.executable, "-B", str(self.worker), mode, str(self.calls), str(CONTROL)]

    def launch(self, mode, **kwargs):
        return subprocess.run(self.argv(mode, **kwargs), capture_output=True, text=True, timeout=10)

    def records(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_worker_receives_current_action_and_stopped_campaign_is_idempotent(self):
        result = self.launch("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        final = json.loads(result.stdout)
        self.assertEqual(final["next_action"], "stopped")
        self.assertEqual(final["stop_reason"], "explicit fixture stop")
        requests = self.records()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["next_action"], "begin")
        self.assertEqual(requests[0]["campaign"], str(self.campaign.resolve()))
        self.assertEqual(requests[0]["round"], 1)
        again = self.launch("failed")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.records(), requests)

    def test_worker_with_no_progress_stops_and_retains_attempt(self):
        result = self.launch("noop")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without a state transition", result.stderr)
        self.assertEqual(len(self.records()), 1)
        attempts = list((self.campaign / "driver").glob("*/request.json"))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(json.loads(attempts[0].read_text())["next_action"], "begin")
        self.assertTrue((attempts[0].parent / "stdout").exists())

    def test_other_campaign_holding_shared_resource_lock_prevents_dispatch(self):
        other = self.create_campaign("other")
        with self.lock.open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.launch("stop", campaign=other)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lock", result.stderr)
        self.assertFalse(self.records())
        released = self.launch("stop", campaign=other)
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(len(self.records()), 1)

    def test_expired_campaign_with_no_active_round_stops_without_a_worker(self):
        expired = self.create_campaign("expired", deadline=time.time() - 10)
        result = self.launch("failed", campaign=expired)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["stop_reason"], "deadline")
        self.assertFalse(self.records())

    def test_ambiguous_failure_does_not_repeat_external_action_on_restart(self):
        failed = self.launch("failed")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(len(self.records()), 1)
        resumed = self.launch("failed")
        self.assertNotEqual(resumed.returncode, 0)
        self.assertEqual(len(self.records()), 1, "the same unacknowledged action ran twice")
        attempt, = (self.campaign / "driver").glob("*/request.json")
        mismatch = self.launch("stop", resume="wrong-attempt")
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertEqual(len(self.records()), 1)
        confirmed = self.launch("stop", resume=attempt.parent.name)
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        self.assertEqual(len(self.records()), 2)

    def test_competing_driver_cannot_bypass_campaign_lock_using_a_different_resource(self):
        with subprocess.Popen(self.argv("hold"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as first:
            try:
                until = time.monotonic() + 3
                while not self.calls.exists() and time.monotonic() < until:
                    time.sleep(0.02)
                self.assertTrue(self.calls.is_file(), "first worker never started")
                conflict = self.launch("stop", lock=self.root / "other-hardware.lock")
                self.assertNotEqual(conflict.returncode, 0)
                self.assertIn("lock", conflict.stderr)
                self.assertEqual(len(self.records()), 1)
            finally:
                self.calls.with_suffix(".release").touch()
                out, err = first.communicate(timeout=5)
        self.assertEqual(first.returncode, 0, out + err)

    def test_worker_retains_device_lock_when_driver_dies(self):
        other = self.create_campaign("queued")
        with subprocess.Popen(self.argv("hold"), stdout=subprocess.PIPE, stderr=subprocess.PIPE) as driver:
            try:
                until = time.monotonic() + 3
                while not self.calls.exists() and time.monotonic() < until:
                    time.sleep(0.02)
                self.assertTrue(self.calls.is_file(), "worker never started")
                driver.kill()
                driver.communicate(timeout=2)
                competing = self.launch("stop", campaign=other)
                self.assertNotEqual(competing.returncode, 0)
                self.assertIn("lock", competing.stderr)
                self.assertEqual(len(self.records()), 1)
            finally:
                self.calls.with_suffix(".release").touch()
                if driver.poll() is None:
                    driver.kill()
                driver.communicate(timeout=2)
                # The surviving worker is local and releases only after it finishes the controller call.
                until = time.monotonic() + 4
                with self.lock.open("a") as lease:
                    while time.monotonic() < until:
                        try:
                            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            time.sleep(0.02)

    def test_timeout_terminates_worker_and_its_child(self):
        timed = self.launch("timeout", timeout=0.45)
        self.assertNotEqual(timed.returncode, 0)
        self.assertEqual(len(self.records()), 1)
        stdout, = (self.campaign / "driver").glob("*/stdout")
        self.assertIn("child started", stdout.read_text())
        receipt = json.loads((stdout.parent / "exit.json").read_text())
        self.assertTrue(receipt["interrupted"])
        self.assertNotEqual(receipt["exit_code"], 0)
        self.assertEqual(json.loads((stdout.parent.parent / "pending.json").read_text())["attempt"],
                         stdout.parent.name)
        time.sleep(1)
        self.assertFalse(self.calls.with_suffix(".late").exists(), "worker descendant survived the timeout")


if __name__ == "__main__":
    unittest.main()

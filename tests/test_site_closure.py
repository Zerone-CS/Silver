"""Test the closed-round/cleanup boundary using synthetic service observations."""
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import campaign, experience, site_910c, site_round
from scripts.device_budget import Budget


class SiteClosureTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.campaign = self.root / "campaign"
        self.campaign.mkdir()
        self.budget = Budget.initialize(self.root / "budget.sqlite", 120)
        self.action = "round-001-closure"
        self.directory = self.root / "deployments" / self.action
        self.directory.mkdir(parents=True)
        self.claim(self.action)
        self.site = SimpleNamespace(root=self.root, settings=object(), stop=Mock())

    def claim(self, action, cleanup=True):
        return self.budget.claim(action, "host", "boot", ["davinci13"], time.monotonic_ns(), 10, cleanup=cleanup)

    def release(self, directory, action, outcome, **extra):
        ns = time.monotonic_ns()
        path = directory / f"device-release-{ns}.json"
        path.write_text(json.dumps({"action_id": action, "host": "host", "boot_id": "boot",
                                    "devices": ["davinci13"], "free": True, "observed_mono_ns": ns,
                                    "site_evidence": {"processes_on_card": {}}, **extra}))
        return self.budget.release(action, ns + 1, path, outcome)

    def cmd(self, name, *args):
        opts = campaign.parser().parse_args([name, "--campaign", str(self.campaign), *map(str, args)])
        with closing(sqlite3.connect(self.campaign / "campaign.sqlite")) as db, db:
            return campaign.transition(db, opts)

    def record_closure(self):
        config = {"parameter": 4}
        side = {"config_sha256": campaign.validate_run.canonical_hash(config), "implementation_sha256": None}
        endpoint = {"base_url": "http://localhost:9000/v1", "model": "test"}
        instance = "fixture-id:started:11"
        obs = self.directory / "round-observation.json"
        obs.write_text(json.dumps({"schema_version": 1, "effective_config": config,
                                   "instance_id": instance, "implementation_sha256": None, **endpoint,
                                   "round_probe": {"round": 1, "campaign_id": "fixture", "challenge": "token"}}))
        health = self.directory / "round-health.json"
        health.write_text(json.dumps({"campaign_id": "fixture", "round": 1, "challenge": "token", "at": time.time(),
                                      "request": {"messages": [{"role": "user", "content": "Reply OK. Request token"}]},
                                      "deployment": campaign.fingerprint(obs), "base_url": endpoint["base_url"],
                                      "instance_id": instance, "http_status": 200, "response": {
                                          "id": "id", "model": "test", "choices": [{"finish_reason": "stop",
                                              "message": {"role": "assistant", "content": "OK"}}]}}))
        (self.directory / "container.json").write_text(json.dumps({"label": self.action, "id": "fixture-id"}))
        (self.directory / "closure-context.json").write_text(json.dumps({"round": 1, "campaign": str(self.campaign)}))
        state = {"campaign_id": "fixture", "count": 1, "stopped": None, "stop_requested": None, "deadline": None,
                 "device_budget": {"path": str(self.budget.path), "minimum_next_die_seconds": 10},
                 "endpoint": endpoint, "max_rounds": 1, "goal_tps": None, "max_retries": 1}
        row = {"schema_version": 1, "campaign_id": "fixture", "scope": "test", "round": 1, "phase": "restore",
               "acceptance_protocol": {"endpoint": endpoint},
               "challenge": "token", "challenge_time": 0, "last_probe": campaign.fingerprint(obs),
               "last_health": campaign.fingerprint(health), "baseline": side, "candidate": side,
               "evidence": {"profiles": {}, "assessments": [], "evidence_files": []}}
        with closing(sqlite3.connect(self.campaign / "campaign.sqlite")) as db, db:
            db.execute("CREATE TABLE campaign(id INTEGER PRIMARY KEY,state TEXT)")
            db.execute("CREATE TABLE rounds(id INTEGER PRIMARY KEY,data TEXT)")
            db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,at REAL,round INTEGER,action TEXT,data TEXT)")
            db.execute("INSERT INTO campaign VALUES (1,?)", (json.dumps(state),))
            db.execute("INSERT INTO rounds VALUES (1,?)", (json.dumps(row),))
        return self.cmd("close", "--round", 1, "--decision", "revert", "--deployment", obs, "--health", health,
                        "--observation", "restored fixture", "--site-action", self.action)

    def test_pending_to_reconciled_to_archived(self):
        status = self.record_closure()
        self.assertEqual((status["round"], status["next_action"]), (1, "attach_release"))
        self.assertEqual(self.cmd("export", "--round", 1)["site_release"]["status"], "pending")
        from scripts import evidence_bundle, recovery_archive
        pending = self.cmd("export", "--round", 1, "--recovery-only")
        self.assertEqual(recovery_archive.audit(pending, Path)["device_release"], "pending")
        mismatch = dict(pending["acceptance_protocol"], model_snapshot_sha256="a" * 64)
        with self.assertRaisesRegex(ValueError, "closure model differs"):
            recovery_archive.audit(dict(pending, acceptance_protocol=mismatch), Path)
        with self.assertRaisesRegex(ValueError, "release is pending"):
            experience.export_round(self.campaign, 1)
        with self.assertRaisesRegex(ValueError, "lease is pending"):
            self.cmd("attach-release", "--round", 1)
        with patch.object(site_910c, "lock_card", return_value=nullcontext()), \
             patch.object(site_910c, "machine", return_value=("host", "boot", ["davinci13"])), \
             patch.object(site_910c.host, "card_snapshot", return_value={"processes_on_card": {}}):
            first = site_910c.reconcile(self.site, self.budget, self.action)
            self.assertEqual(first, site_910c.reconcile(self.site, self.budget, self.action))
        self.site.stop.assert_called_once()
        export = self.cmd("export", "--round", 1)
        self.assertEqual(export["site_release"]["lease"], first)
        self.assertEqual(export["site_release"]["proof"]["sha256"], first["proof_sha256"])
        recovery = self.cmd("export", "--round", 1, "--recovery-only")
        source = self.root / "recovery.json"
        source.write_text(json.dumps(recovery))
        with TemporaryDirectory() as output, patch.object(evidence_bundle, "assessor_identity",
                                                        return_value={"release_id": "fixture"}):
            bundle = Path(output).resolve() / "recovery"
            recovery_archive.pack(source, [self.root], bundle)
            self.assertEqual(evidence_bundle.replay(bundle)["device_release"], "released")
        proof = Path(export["site_release"]["proof"]["path"])
        proof.write_bytes(proof.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "changed round evidence"):
            self.cmd("export", "--round", 1, "--recovery-only")
        self.assertEqual(self.cmd("status")["next_action"], "stopped")
        self.assertEqual(len([e for e in self.cmd("history") if e["action"] == "attach-release"]), 1)

    def test_mismatched_release_proof_cannot_finish_round(self):
        self.record_closure()
        self.release(self.directory, self.action, "collected", action_id="other")
        with self.assertRaisesRegex(ValueError, "does not match this closure"):
            self.cmd("attach-release", "--round", 1)
        self.assertEqual(self.cmd("status")["site_release"]["status"], "pending")

    def test_fresh_attempt_requires_verified_release_and_obeys_cap(self):
        driver = object.__new__(site_round.Round)
        driver.budget, driver.site, driver.max_retries = self.budget, self.site, 1
        with self.assertRaisesRegex(RuntimeError, "reconcile pending"):
            driver.attempt(self.action)
        self.release(self.directory, self.action, "failed")
        second, suffix = driver.attempt(self.action)
        self.assertEqual((second, suffix), (self.action + "-attempt-2", "-attempt-2"))
        directory = self.directory.parent / second
        directory.mkdir()
        self.claim(second)
        self.release(directory, second, "failed")
        with self.assertRaisesRegex(ValueError, "attempt limit"):
            driver.attempt(self.action)
        self.assertEqual(len(list(self.directory.glob("device-release-*.json"))), 1)

    def test_diagnostic_retry_preserves_trace_paths_and_registered_policy(self):
        self.release(self.directory, self.action, "failed")
        driver = object.__new__(site_round.Round)
        self.site.settings = SimpleNamespace(packages={"torch-npu": "fixture"})
        driver.site, driver.budget, driver.max_retries = self.site, self.budget, 1
        driver.label, driver.round_dir = "round-001", self.campaign / "round-001"
        driver.request = lambda action, side: SimpleNamespace(action=action)
        driver.requirements = [{"id": "kernel", "field": "name", "pattern": "test", "min_count": 1}]
        request, spec = driver.profile("baseline")
        directory = self.directory.parent / request.action
        directory.mkdir()
        self.claim(request.action, cleanup=False)
        self.release(directory, request.action, "collected")
        (directory / "action-receipt.json").write_text("{}")
        self.assertEqual(driver.profile("baseline")[0].action, request.action)
        (directory / "profile-rejection.json").write_text("{}")
        retry, spec2 = driver.profile("baseline")
        self.assertNotEqual(retry.action, request.action)
        self.assertNotEqual(retry.run, request.run)
        self.assertNotEqual(spec["traces"], spec2["traces"])
        self.assertEqual(site_round.profile_policy(spec), site_round.profile_policy(spec2))
        self.assertTrue(request.profile_spec.is_file())


if __name__ == "__main__":
    unittest.main()

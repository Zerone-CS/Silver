import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_metadata.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("prepare_metadata", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class PrepareMetadataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.argv = ["--scenario", "smoke", "--profiler-enabled", "off",
                     "--run-purpose", "diagnostic"]
        for name in ("config", "environment", "metric-contract"):
            p = self.root / (name + ".json")
            p.write_text(json.dumps({"purpose": "offline_fixture"}))
            self.argv += ["--" + name, str(p)]
        self.out = self.root / "metadata.json"
        self.argv += ["--output", str(self.out)]

    def test_default_does_not_fabricate_verified_quality(self):
        self.assertEqual(mod.main(self.argv), 0)
        obj = json.loads(self.out.read_text())
        self.assertEqual(obj["quality_status"], "not_evaluated")
        self.assertIs(obj["effective_config_verified"], False)
        self.assertEqual(obj["run_purpose"], "diagnostic")
        self.assertEqual(len(obj["config_sha256"]), 64)

    def test_refuses_existing_output(self):
        self.out.write_text("preserve")
        self.assertEqual(mod.main(self.argv), 2)
        self.assertEqual(self.out.read_text(), "preserve")

    def test_explicit_evidence_is_hashed(self):
        quality = self.root / "quality.json"
        quality.write_text(json.dumps({"status": "pass", "scope": "ten prompts only", "method": "exact_match"}))
        log = self.root / "startup.log"
        log.write_text("fictional fixture only")
        evidence = self.root / "effective.json"
        evidence.write_text(json.dumps({"verified": True, "scope": "fixture", "evidence": [log.name]}))
        self.assertEqual(mod.main(self.argv + ["--quality-report", str(quality), "--config-evidence", str(evidence)]), 0)
        obj = json.loads(self.out.read_text())
        self.assertTrue(obj["effective_config_verified"])
        self.assertEqual(obj["quality_scope"], "ten prompts only")
        self.assertEqual(len(obj["config_evidence_artifacts"][0]["sha256"]), 64)

    def test_implementation_evidence_must_match_identity_and_refer_to_real_artifacts(self):
        identity = self.root / "source.json"
        identity.write_text('{"package":"build-B"}')
        proof = self.root / "proof.json"
        argv = self.argv + ["--implementation", str(identity), "--implementation-evidence", str(proof)]
        content = {"verified": True, "scope": "actually loaded module", "evidence": ["probe.log"],
                   "implementation_sha256": "f" * 64}
        proof.write_text(json.dumps(content))
        (self.root / "probe.log").write_text("offline fixture probe")
        self.assertEqual(mod.main(argv), 2)
        self.assertFalse(self.out.exists())
        content["implementation_sha256"] = mod.identity_hash(json.loads(identity.read_text()))
        content["evidence"] = ["absent-probe.log"]
        proof.write_text(json.dumps(content))
        self.assertEqual(mod.main(argv), 2)
        self.assertFalse(self.out.exists())

    def test_old_or_other_implementations_quality_report_cannot_certify_candidate(self):
        identity = self.root / "source.json"
        identity.write_text('{"package":"build-B"}')
        quality = self.root / "quality.json"
        argv = self.argv + ["--implementation", str(identity), "--quality-report", str(quality)]
        for extra in ({}, {"implementation_sha256": mod.identity_hash({"package": "build-A"})}):
            with self.subTest(extra=extra):
                quality.write_text(json.dumps(dict(status="pass", scope="fixture", method="exact", **extra)))
                self.assertEqual(mod.main(argv), 2)
                self.assertFalse(self.out.exists())

    def test_wrong_boolean_rejected(self):
        evidence = self.root / "effective.json"
        evidence.write_text(json.dumps({"verified": "true", "scope": "fixture", "evidence": ["startup.log"]}))
        self.assertEqual(mod.main(self.argv + ["--config-evidence", str(evidence)]), 2)
        self.assertFalse(self.out.exists())

    def test_nonfinite_rejected(self):
        (self.root / "config.json").write_text('{"x":1e999}')
        self.assertEqual(mod.main(self.argv), 2)
        self.assertFalse(self.out.exists())

    def test_duplicate_key_rejected(self):
        (self.root / "config.json").write_text('{"x":1,"x":2}')
        self.assertEqual(mod.main(self.argv), 2)


if __name__ == "__main__":
    unittest.main()

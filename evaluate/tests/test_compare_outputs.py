import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_outputs.py"


class CompareOutputsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reference = self.root / "reference.json"
        self.candidate = self.root / "candidate.json"
        self.output = self.root / "report.json"
        self.reference.write_text(json.dumps({"p1": "你好", "p2": "world"}), encoding="utf-8")
        self.candidate.write_bytes(self.reference.read_bytes())

    def cli(self, reference=None, candidate=None, output=None):
        return subprocess.run([sys.executable, str(SCRIPT),
                               "--reference", str(reference or self.reference),
                               "--candidate", str(candidate or self.candidate),
                               "--output", str(output or self.output)],
                              capture_output=True, text=True)

    def test_exact_match_only_claims_smoke_scope(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.output.read_text())
        self.assertEqual(report["status"], "exact_match_smoke_pass")
        self.assertEqual(report["quality_status"], "not_evaluated")
        self.assertEqual(report["n_exact_match"], 2)
        self.assertEqual(report["source_hashes"]["reference"],
                         hashlib.sha256(self.reference.read_bytes()).hexdigest())

    def test_swapped_arguments_never_modify_either_input(self):
        self.candidate.write_text(json.dumps({"p1": "different", "p2": "world"}))
        originals = {path: path.read_bytes() for path in (self.reference, self.candidate)}
        result = self.cli(reference=self.candidate, candidate=self.reference)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(self.output.read_text())["mismatched_prompt_ids"], ["p1"])
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_output_cannot_overwrite_reference_or_candidate(self):
        for path in (self.reference, self.candidate):
            with self.subTest(path=path):
                original = path.read_bytes()
                self.assertEqual(self.cli(output=path).returncode, 2)
                self.assertEqual(path.read_bytes(), original)

    def test_existing_output_cannot_be_overwritten(self):
        self.output.write_text("precious result")
        self.assertEqual(self.cli().returncode, 2)
        self.assertEqual(self.output.read_text(), "precious result")

    def test_same_path_symlink_and_hardlink_inputs_are_rejected(self):
        self.assertEqual(self.cli(candidate=self.reference).returncode, 2)
        for name, make_link in (("sym.json", os.symlink), ("hard.json", os.link)):
            alias = self.root / name
            make_link(self.reference, alias)
            self.assertEqual(self.cli(candidate=alias).returncode, 2)
        self.assertFalse(self.output.exists())

    def test_output_symlink_is_never_followed(self):
        self.output.symlink_to(self.reference)
        original = self.reference.read_bytes()
        self.assertEqual(self.cli().returncode, 2)
        self.assertEqual(self.reference.read_bytes(), original)

    def test_missing_and_unexpected_ids_are_difference(self):
        self.candidate.write_text(json.dumps({"p2": "world", "p3": "extra"}))
        self.assertEqual(self.cli().returncode, 1)
        report = json.loads(self.output.read_text())
        self.assertEqual(report["missing_prompt_ids"], ["p1"])
        self.assertEqual(report["unexpected_prompt_ids"], ["p3"])
        self.assertFalse(report["passed"])

    def test_bad_input_maps_fail_without_output(self):
        for text in ("{}", "[]", '{"p1":3}', '{"": "x"}',
                     '{"p1":"a","p1":"b"}', "not-json"):
            with self.subTest(text=text):
                self.candidate.write_text(text)
                self.assertEqual(self.cli().returncode, 2)
                self.assertFalse(self.output.exists())

    def test_missing_input_is_error(self):
        self.candidate.unlink()
        self.assertEqual(self.cli().returncode, 2)
        self.assertFalse(self.output.exists())

    def test_help_has_no_side_effects(self):
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()

"""Offline tests of snapshot syntax/structure and read-only behavior."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_kb_snapshot.py"
SPEC = importlib.util.spec_from_file_location("check_kb_snapshot", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def card(name="--tensor-parallel-size", **changes):
    value = {
        "name": name, "type": "cli", "category": "parallelism", "scope": "both",
        "source_file": ["vllm/engine/arg_utils.py:825"],
        "value_type": "int", "default": 1, "valid_choices": None,
        "cli_example": "--tensor-parallel-size 2", "deprecated": False,
        "performance_impact": "high", "performance_scope": ["throughput", "memory"],
        "impact_detail": "Controls tensor parallelism and collective communication.",
        "usage_locations": [{"file": "vllm/config/parallel.py:1", "context": "validation"}],
        "related_parameters": [{"name": "--pipeline-parallel-size", "relation": "parallel layout"}],
        "constraints": ["Check the model's head partition constraints."],
        "tuning_advice": {
            "summary": "Measure feasible layouts.",
            "suggested_values": [{"scenario": "small model", "value": 1, "reason": "fits one device"}],
            "caveats": ["Deployment-specific result."], "quick_guide": "Compare under the same load.",
        },
        "analysis_date": "2026-10-27",
    }
    value.update(changes)
    return value


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.params = self.root / "params"
        self.params.mkdir()

    def write_card(self, data=None, filename=None):
        data = card() if data is None else data
        if filename is None:
            filename = CHECK.sanitize_filename(data["name"]) + ".yaml"
        path = self.params / filename
        path.write_text(json.dumps(data), encoding="utf-8")  # JSON is valid YAML.
        return path

    def manifest(self, **changes):
        value = {
            "output_yaml_count": 1,
            "stage1_coarse_filter": {"passed": 1, "skipped": 0},
            "stage2_deep_analysis": {
                "analyzed": 1, "confirmed_perf_related": 1,
                "performance_high": 1, "performance_medium": 0, "performance_low": 0,
                "confirmed_not_perf": 0, "errors": 0,
            },
            "source_commits": {"vllm": "ced6857", "vllm-ascend": "8fde6d4e3"},
        }
        value.update(changes)
        path = self.root / "manifest.yaml"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def inspect(self, manifest=None):
        return CHECK.inspect_snapshot(self.params, manifest)

    def codes(self, report, severity="errors"):
        return {entry["code"] for entry in report[severity]}

    def cli(self, output, manifest=None):
        args = ["--params-dir", str(self.params), "--output", str(output)]
        if manifest is not None:
            args += ["--manifest", str(manifest)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return CHECK.main(args)

    def test_actual_full_structure_and_hashes(self):
        path = self.write_card()
        manifest = self.manifest()
        report = self.inspect(manifest)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["counts"]["yaml_files"], 1)
        self.assertEqual(report["counts"]["structurally_valid_cards"], 1)
        self.assertEqual(report["hashes"]["cards"][path.name], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(len(report["hashes"]["snapshot_sha256"]), 64)
        self.assertTrue(any("not verified" in value for value in report["limits"]))

    def test_actual_legal_nested_dots_and_environment_case_are_preserved(self):
        self.write_card(card("additional_config.enable_prefill_cache", type="nested", scope="vllm-ascend"))
        self.write_card(card("VLLM_ASCEND_ENABLE_FLASHCOMM", type="env", scope="vllm-ascend"))
        report = self.inspect()
        self.assertEqual(report["status"], "pass")
        self.assertNotIn("filename_mismatch", self.codes(report))

    def test_only_actual_required_fields_are_required(self):
        full = card()
        self.write_card({key: full[key] for key in CHECK.REQUIRED})
        self.assertEqual(self.inspect()["status"], "pass")

    def test_empty_and_missing_directories_fail(self):
        self.assertIn("empty_snapshot", self.codes(self.inspect()))
        report = CHECK.inspect_snapshot(self.root / "missing")
        self.assertEqual(report["status"], "fail")
        self.assertIn("params_directory", self.codes(report))

    def test_broken_card_is_not_skipped_and_all_cards_are_examined(self):
        self.write_card()
        (self.params / "broken.yaml").write_text("name: [unterminated", encoding="utf-8")
        report = self.inspect()
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["counts"]["yaml_files"], 2)
        self.assertEqual(report["counts"]["parsed_mappings"], 1)
        self.assertIn("broken.yaml", report["hashes"]["cards"])
        self.assertIn("card_parse", self.codes(report))

    def test_duplicate_yaml_key_is_rejected(self):
        (self.params / "example.yaml").write_text("name: --example\nname: --other\n", encoding="utf-8")
        report = self.inspect()
        self.assertIn("card_parse", self.codes(report))
        self.assertIn("duplicate mapping key", report["errors"][0]["message"])

    def test_non_mapping_and_invalid_utf8_are_rejected(self):
        (self.params / "list.yaml").write_text("- one\n- two\n", encoding="utf-8")
        (self.params / "bytes.yaml").write_bytes(b"\xff\xfe")
        report = self.inspect()
        self.assertEqual(report["status"], "fail")
        self.assertEqual(len(report["errors"]), 2)

    def test_missing_fields_are_all_reported(self):
        self.write_card({"name": "--example"})
        report = self.inspect()
        messages = "\n".join(item["message"] for item in report["errors"])
        for field in CHECK.REQUIRED:
            if field != "name":
                self.assertIn(field + " is required", messages)

    def test_enums_and_nested_structures_are_checked(self):
        self.write_card(card(type="flag", scope="ascend", performance_impact="extreme", source_file="file.py:1", usage_locations=["bad"], constraints=[1], tuning_advice={"suggested_values": [{"scenario": "test", "reason": 1}]}))
        report = self.inspect()
        self.assertEqual(report["status"], "fail")
        self.assertGreaterEqual(len(report["errors"]), 8)

    def test_model_accepts_none_impact_both_scope_and_quantization(self):
        self.write_card(card(performance_impact="none", scope="both", category="quantization"))
        self.assertEqual(self.inspect()["status"], "pass")

    def test_duplicate_parameter_name_fails(self):
        self.write_card()
        self.write_card(card(), "duplicate.yaml")
        self.assertIn("duplicate_name", self.codes(self.inspect()))

    def test_sanitized_collision_fails_and_alias_is_only_review_warning(self):
        self.write_card(card("--foo-bar"))
        self.write_card(card("foo_bar", type="env"), "different.yaml")
        report = self.inspect()
        self.assertIn("filename_collision", self.codes(report))
        self.assertIn("filename_mismatch", self.codes(report))
        self.assertIn("possible_parameter_alias", self.codes(report, "warnings"))

    def test_filename_mismatch_is_not_silently_renamed(self):
        path = self.write_card(card(), "wrong.yaml")
        before = path.read_bytes()
        self.assertIn("filename_mismatch", self.codes(self.inspect()))
        self.assertEqual(path.read_bytes(), before)

    def test_output_count_mismatch_fails(self):
        self.write_card()
        report = self.inspect(self.manifest(output_yaml_count=2))
        self.assertEqual(report["status"], "fail")
        self.assertIn("output_count_mismatch", self.codes(report))

    def test_analysis_and_impact_count_differences_are_warnings(self):
        self.write_card()
        manifest = self.manifest(stage2_deep_analysis={"analyzed": 3, "confirmed_perf_related": 2, "performance_high": 2, "errors": 1})
        report = self.inspect(manifest)
        self.assertEqual(report["status"], "pass")
        for code in ("analyzed_vs_written", "perf_vs_written", "impact_count_mismatch", "analysis_errors_declared", "partial_analysis"):
            self.assertIn(code, self.codes(report, "warnings"))

    def test_manifest_bool_count_and_broken_yaml_fail(self):
        self.write_card()
        self.assertIn("manifest_structure", self.codes(self.inspect(self.manifest(output_yaml_count=True))))
        path = self.root / "bad-manifest.yaml"
        path.write_text("broken: [", encoding="utf-8")
        self.assertIn("manifest_parse", self.codes(self.inspect(path)))

    def test_cli_refuses_overwrite_and_preserves_input_bytes(self):
        path = self.write_card()
        manifest = self.manifest()
        before = {str(p): p.read_bytes() for p in (path, manifest)}
        output = self.root / "report.json"
        self.assertEqual(self.cli(output, manifest), 0)
        original_output = output.read_bytes()
        self.assertEqual(self.cli(output, manifest), 2)
        self.assertEqual(output.read_bytes(), original_output)
        self.assertEqual(before, {str(p): p.read_bytes() for p in (path, manifest)})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["manifest.yaml", "params", "report.json"])

    def test_output_must_not_overwrite_input_or_symlink(self):
        path = self.write_card()
        before = path.read_bytes()
        self.assertEqual(self.cli(path), 2)
        self.assertEqual(path.read_bytes(), before)
        link = self.root / "report.json"
        link.symlink_to(self.root / "not-yet-existing")
        self.assertEqual(self.cli(link), 2)
        self.assertFalse((self.root / "not-yet-existing").exists())

    def test_fail_report_is_written_with_exit_two(self):
        output = self.root / "failed.json"
        self.assertEqual(self.cli(output), 2)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "fail")

    def test_help_works_without_site_packages_or_pyyaml(self):
        result = subprocess.run([sys.executable, "-S", str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--params-dir", result.stdout)
        self.assertIn("--output", result.stdout)

    def test_missing_pyyaml_returns_clear_fail_report(self):
        self.write_card()
        with mock.patch.object(CHECK, "_import_yaml", side_effect=ImportError("not installed")):
            report = self.inspect()
        self.assertEqual(report["status"], "fail")
        self.assertIn("dependency_missing", self.codes(report))


if __name__ == "__main__":
    unittest.main()

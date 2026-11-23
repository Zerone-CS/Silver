"""Offline integrity regressions through the real knowledge builder.

Only the LLM client boundary is replaced. The CLI, source preflight, Stage 1,
Stage 2 scheduler, YAML validation/writes, progress and manifest remain real.
Synthetic responses and source repositories never contact a model or service.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

import yaml


KB = Path(__file__).resolve().parents[1] / "kb"
sys.path.insert(0, str(KB))
from parse_params.stage2_analyzer import PromptBuilder, YAMLWriter


def parameter(name="--cache-blocks"):
    return {
        "name": name,
        "type": "cli",
        "scope": "vllm",
        "category": "memory",
        "description": "Number of cache blocks allocated by the scheduler",
    }


def card(param=None, impact="high"):
    param = param or parameter()
    data = {key: param[key] for key in ("name", "type", "scope")}
    data["performance_impact"] = impact
    if impact == "none":
        data["skip_reason"] = "Synthetic source proves this option is unused."
        return data
    data.update({
        "category": "memory",
        "source_file": ["vllm/engine/arg_utils.py"],
        "value_type": "int",
        "default": 16,
        "valid_choices": "positive integers",
        "performance_scope": ["throughput", "memory"],
        "impact_detail": "This synthetic option bounds the cache allocation.",
    })
    return data


def yaml_text(value):
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


class YAMLWriterIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.writer = YAMLWriter(self.root / "params", logging.getLogger(__name__))

    def parsed(self, value):
        result = self.writer.validate_and_parse(yaml_text(value), parameter())
        self.assertTrue(result["valid"], result.get("error"))
        self.assertEqual(result["status"], "ok")
        return result["data"]

    def test_matching_card_and_explicit_none_are_valid(self):
        for impact in ("high", "none"):
            with self.subTest(impact=impact):
                result = self.writer.validate_and_parse(yaml_text(card(impact=impact)), parameter())
                self.assertTrue(result["valid"], result.get("error"))
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["performance_impact"], impact)
                self.assertEqual(result["data"].name, parameter()["name"])

    def test_wrong_or_missing_identity_is_rejected_including_none(self):
        wrong_values = {"name": "--another-cache", "type": "env", "scope": "vllm-ascend"}
        for impact in ("high", "none"):
            for field, wrong in wrong_values.items():
                for kind in ("wrong", "missing", "empty"):
                    with self.subTest(impact=impact, field=field, kind=kind):
                        value = card(impact=impact)
                        if kind == "missing":
                            del value[field]
                        else:
                            value[field] = wrong if kind == "wrong" else ""
                        result = self.writer.validate_and_parse(yaml_text(value), parameter())
                        self.assertFalse(result["valid"])
                        self.assertEqual(result["status"], "error")
                        self.assertIsNone(result.get("data"))
                        self.assertTrue(result.get("error"))
        self.assertFalse(self.writer.output_dir.exists())

    def test_missing_impact_is_not_inferred_as_no_performance_effect(self):
        for impact in ("high", "none"):
            with self.subTest(original_impact=impact):
                value = card(impact=impact)
                del value["performance_impact"]
                result = self.writer.validate_and_parse(yaml_text(value), parameter())
                self.assertFalse(result["valid"])
                self.assertEqual(result["status"], "error")

    def test_incomplete_input_identity_cannot_validate_a_response(self):
        for field in ("name", "type", "scope"):
            with self.subTest(field=field):
                incomplete = parameter()
                del incomplete[field]
                result = self.writer.validate_and_parse(yaml_text(card()), incomplete)
                self.assertFalse(result["valid"])
                self.assertEqual(result["status"], "error")

    def test_prompt_includes_requested_scope_for_identity_validation(self):
        prompt = PromptBuilder(logging.getLogger(__name__)).build_user_prompt(
            parameter(), "synthetic definition", "synthetic usage",
        )
        self.assertIn("Name: --cache-blocks", prompt)
        self.assertIn("Type: cli", prompt)
        self.assertIn("Scope: vllm", prompt)

    def test_repeated_write_preserves_existing_card_bytes(self):
        original = self.parsed(card())
        target = self.writer.write_yaml(original)
        before = target.read_bytes()
        replacement = card()
        replacement["impact_detail"] = "A second task must not replace the first card."
        with self.assertRaises(OSError):
            self.writer.write_yaml(self.parsed(replacement))
        self.assertEqual(target.read_bytes(), before)

    def test_existing_symlink_cannot_replace_its_target(self):
        model = self.parsed(card())
        target = self.writer.write_yaml(model)
        target.unlink()
        victim = self.root / "other-card.yaml"
        before = b"existing unrelated card\n"
        victim.write_bytes(before)
        target.symlink_to(victim)
        with self.assertRaises(OSError):
            self.writer.write_yaml(model)
        self.assertTrue(target.is_symlink())
        self.assertEqual(victim.read_bytes(), before)

    def test_concurrent_creators_cannot_overwrite_each_other(self):
        model = self.parsed(card())
        gate = threading.Barrier(2)

        def attempt():
            gate.wait(timeout=10)
            try:
                return self.writer.write_yaml(model)
            except OSError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(attempt) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        written = [result for result in results if isinstance(result, Path)]
        rejected = [result for result in results if isinstance(result, OSError)]
        self.assertEqual(len(written), 1, results)
        self.assertEqual(len(rejected), 1, results)
        self.assertEqual(yaml.safe_load(written[0].read_text())["name"], parameter()["name"])


# Running the real CLI in a child also checks its actual exit status. The audit
# guard catches accidental socket use even if the client mock stops matching.
OFFLINE_CLI = r'''
import json
from pathlib import Path
import re
import runpy
import sys

def forbid_network(event, args):
    if event.startswith("socket.") and event in {
        "socket.connect", "socket.connect_ex", "socket.getaddrinfo",
        "socket.bind", "socket.sendto",
    }:
        raise AssertionError("network forbidden in offline builder regression")

sys.addaudithook(forbid_network)
sys.path.insert(0, sys.argv.pop(1))
responses = json.loads(Path(sys.argv.pop(1)).read_text())
calls = Path(sys.argv.pop(1))
from parse_params.stage2_analyzer import LLMClient

def offline_init(self, logger):
    self.logger = logger

async def offline_analyze(self, system_prompt, user_prompt):
    match = re.search(r"^Name: (.+)$", user_prompt, re.MULTILINE)
    if match is None:
        raise AssertionError("parameter identity missing from the model prompt")
    name = match.group(1)
    with calls.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(name) + "\n")
    response = responses[name]
    if isinstance(response, dict) and "raise" in response:
        raise RuntimeError(response["raise"])
    return response

LLMClient.__init__ = offline_init
LLMClient.analyze = offline_analyze
runpy.run_module("parse_params", run_name="__main__")
'''


class BuilderCLIIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="knowledge integrity ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = self.root / "sources"
        self.input = self.root / "parameters.json"
        self.responses = self.root / "responses.json"
        self.calls = self.root / "model-calls.jsonl"
        self.output = self.root / "new snapshot"

    def configure(self, params, responses):
        sources = {
            "vllm/vllm/config/cache.py": "class CacheConfig:\n    block_size: int = 16\n",
            "vllm/vllm/engine/arg_utils.py": (
                "def add_cli_args(parser):\n" + "".join(
                    "    parser.add_argument(" + repr(p["name"]) + ", type=int, default=16)\n"
                    for p in params
                ) + "    return parser\n"
            ),
            "vllm/vllm/envs.py": "CACHE_BLOCKS = 16\n",
            "ascend/vllm_ascend/ascend_config.py": "class AscendConfig:\n    enable_graph: bool = True\n",
            "ascend/vllm_ascend/envs.py": "ASCEND_CACHE_BLOCKS = 16\n",
            "ascend/docs/source/test.md": "Synthetic offline source fixture.\n",
        }
        for relative, content in sources.items():
            path = self.sources / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.input.write_text(json.dumps(params), encoding="utf-8")
        self.responses.write_text(json.dumps(responses), encoding="utf-8")

    def run_builder(self, resume=False):
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("ANTHROPIC_", "VLLM_", "LLM_"))
            and key not in ("PYTHONPATH", "PYTHONHOME")
        }
        environment.update({
            "ANTHROPIC_API_KEY": "offline-placeholder-not-a-secret",
            "ANTHROPIC_MODEL": "offline-integrity-fixture",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        })
        command = [
            sys.executable, "-B", "-c", OFFLINE_CLI,
            str(KB), str(self.responses), str(self.calls),
            "--input", str(self.input), "--output", str(self.output),
            "--vllm-src", str(self.sources / "vllm"),
            "--vllm-ascend-src", str(self.sources / "ascend"),
            "--concurrency", "2",
        ]
        if resume:
            command.append("--resume")
        result = subprocess.run(
            command, cwd=self.root, env=environment,
            capture_output=True, text=True, timeout=60,
        )
        self.assertTrue((self.output / "manifest.yaml").is_file(), result.stderr)
        manifest = yaml.safe_load((self.output / "manifest.yaml").read_text())
        return result, manifest

    def called_names(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def json_log(self, name):
        path = self.output / "logs" / name
        return json.loads(path.read_text()) if path.exists() else []

    def assert_counts(self, manifest, *, analyzed, perf, none, errors, skipped):
        stage2 = manifest["stage2_deep_analysis"]
        self.assertEqual(stage2["analyzed"], analyzed)
        self.assertEqual(stage2["confirmed_perf_related"], perf)
        self.assertEqual(stage2["confirmed_not_perf"], none)
        self.assertEqual(stage2["errors"], errors)
        self.assertEqual(manifest["output_yaml_count"], perf)
        self.assertEqual(manifest["output_skipped_count"], skipped)
        self.assertEqual(stage2["performance_high"] + stage2["performance_medium"] + stage2["performance_low"], perf)

    def test_successful_build_runs_full_pipeline_and_exits_zero(self):
        impacts = ("high", "medium", "low", "none")
        params = [parameter("--cache-" + impact) for impact in impacts]
        self.configure(params, {p["name"]: yaml_text(card(p, impact)) for p, impact in zip(params, impacts)})
        result, manifest = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_counts(manifest, analyzed=4, perf=3, none=1, errors=0, skipped=1)
        self.assertCountEqual(self.called_names(), [p["name"] for p in params])
        self.assertEqual(len(self.json_log("stage2_skipped.json")), 1)
        self.assertEqual(self.json_log("analysis_errors.json"), [])
        output_names = [yaml.safe_load(path.read_text())["name"] for path in (self.output / "params").glob("*.yaml")]
        self.assertCountEqual(output_names, [p["name"] for p in params[:3]])

    def test_all_failures_exit_nonzero_without_becoming_skips_or_none(self):
        params = [parameter("--cache-bad-yaml"), parameter("--cache-wrong-name"), parameter("--cache-empty"), parameter("--quiet")]
        params[-1].update(category="observability", description="Log verbosity")
        wrong = card(params[1])
        wrong["name"] = "--some-other-cache"
        self.configure(params, {
            params[0]["name"]: "[broken YAML",
            params[1]["name"]: yaml_text(wrong),
            params[2]["name"]: "",
        })
        result, manifest = self.run_builder()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Parse complete!", result.stdout + result.stderr)
        self.assert_counts(manifest, analyzed=3, perf=0, none=0, errors=3, skipped=1)
        self.assertEqual(self.json_log("stage2_skipped.json"), [])
        errors = self.json_log("analysis_errors.json")
        self.assertCountEqual([item["name"] for item in errors], [p["name"] for p in params[:3]])
        self.assertCountEqual(self.called_names(), [p["name"] for p in params[:3]])

    def configure_mixed(self):
        params = [parameter("--cache-good"), parameter("--cache-none"), parameter("--cache-failed"), parameter("--quiet")]
        params[-1].update(category="observability", description="Log verbosity")
        self.configure(params, {
            params[0]["name"]: yaml_text(card(params[0])),
            params[1]["name"]: yaml_text(card(params[1], "none")),
            params[2]["name"]: {"raise": "synthetic offline model failure"},
        })
        return params

    def test_partial_failure_preserves_success_and_fails_build(self):
        params = self.configure_mixed()
        result, manifest = self.run_builder()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assert_counts(manifest, analyzed=3, perf=1, none=1, errors=1, skipped=2)
        self.assertEqual([row["name"] for row in self.json_log("analysis_errors.json")], [params[2]["name"]])
        self.assertEqual([row["name"] for row in self.json_log("stage2_skipped.json")], [params[1]["name"]])

    def test_resume_keeps_prior_error_and_statistics_without_more_model_calls(self):
        self.configure_mixed()
        first, before = self.run_builder()
        self.assertNotEqual(first.returncode, 0, first.stderr)
        calls_before = self.called_names()
        card_bytes = {p.name: p.read_bytes() for p in (self.output / "params").glob("*.yaml")}
        result, after = self.run_builder(resume=True)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Parse complete!", result.stdout + result.stderr)
        self.assertEqual(after["stage2_deep_analysis"], before["stage2_deep_analysis"])
        self.assert_counts(after, analyzed=3, perf=1, none=1, errors=1, skipped=2)
        self.assertEqual(self.called_names(), calls_before)
        self.assertEqual({p.name: p.read_bytes() for p in (self.output / "params").glob("*.yaml")}, card_bytes)
        self.assertEqual(len(self.json_log("analysis_errors.json")), 1)
        self.assertEqual(len(self.json_log("stage2_skipped.json")), 1)

    def test_resume_success_and_explicit_none_keep_statistics_without_more_calls(self):
        params = [parameter("--cache-good"), parameter("--cache-none")]
        self.configure(params, {
            params[0]["name"]: yaml_text(card(params[0])),
            params[1]["name"]: yaml_text(card(params[1], "none")),
        })
        first, before = self.run_builder()
        self.assertEqual(first.returncode, 0, first.stderr)
        calls_before = self.called_names()
        result, after = self.run_builder(resume=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(after["stage2_deep_analysis"], before["stage2_deep_analysis"])
        self.assert_counts(after, analyzed=2, perf=1, none=1, errors=0, skipped=1)
        self.assertEqual(self.called_names(), calls_before)

    def test_resume_pending_work_preserves_historical_error_and_only_calls_pending(self):
        params = self.configure_mixed()
        first, before = self.run_builder()
        self.assertNotEqual(first.returncode, 0, first.stderr)
        calls_before = self.called_names()
        errors_before = self.json_log("analysis_errors.json")
        build_identity = (self.output / "build-inputs.json").read_bytes()

        # Construct a checkpoint after the none/error outcomes were persisted,
        # but before the successful parameter was called. Remove its terminal
        # record and card together; keep all input and builder identities fixed.
        # This exercises a resumed scheduler with both pending work and a prior
        # error, rather than the simpler all-parameters-already-terminal path.
        pending_name = params[0]["name"]
        progress_path = self.output / "progress.json"
        progress = json.loads(progress_path.read_text())
        del progress["stage2_results"][pending_name]
        for field in ("processed_params", "error_params"):
            progress[field] = [name for name in progress[field] if name != pending_name]
        progress["completed_stage2"] = len(progress["processed_params"])
        progress_path.write_text(json.dumps(progress), encoding="utf-8")
        target = next((self.output / "params").glob("*.yaml"))
        self.assertEqual(yaml.safe_load(target.read_text())["name"], pending_name)
        target.unlink()
        (self.output / "manifest.yaml").unlink()

        result, after = self.run_builder(resume=True)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Parse complete!", result.stdout + result.stderr)
        self.assertEqual(self.called_names(), calls_before + [pending_name])
        self.assertEqual(after["stage2_deep_analysis"], before["stage2_deep_analysis"])
        self.assert_counts(after, analyzed=3, perf=1, none=1, errors=1, skipped=2)
        self.assertEqual(self.json_log("analysis_errors.json"), errors_before)
        self.assertEqual((self.output / "build-inputs.json").read_bytes(), build_identity)
        self.assertEqual(yaml.safe_load(target.read_text())["name"], pending_name)

    def test_missing_or_corrupt_card_on_resume_cannot_become_confirmed_none(self):
        for corruption in ("missing", "invalid_yaml", "wrong_identity", "changed_content", "changed_impact"):
            with self.subTest(corruption=corruption):
                self.output = self.root / corruption
                self.calls = self.root / (corruption + "-calls.jsonl")
                param = parameter()
                self.configure([param], {param["name"]: yaml_text(card(param))})
                first, _ = self.run_builder()
                self.assertEqual(first.returncode, 0, first.stderr)
                target = next((self.output / "params").glob("*.yaml"))
                if corruption == "missing":
                    target.unlink()
                elif corruption == "invalid_yaml":
                    target.write_text("[broken YAML", encoding="utf-8")
                elif corruption == "wrong_identity":
                    wrong = card(param)
                    wrong["name"] = "--another-cache"
                    target.write_text(yaml_text(wrong), encoding="utf-8")
                else:
                    altered = yaml.safe_load(target.read_text())
                    if corruption == "changed_content":
                        altered["impact_detail"] = "A modified conclusion with the same identity and impact."
                    else:
                        altered["performance_impact"] = "low"
                    target.write_text(yaml_text(altered), encoding="utf-8")
                result, manifest = self.run_builder(resume=True)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                stage2 = manifest["stage2_deep_analysis"]
                self.assertEqual(stage2["errors"], 1)
                self.assertEqual(stage2["confirmed_not_perf"], 0)
                self.assertEqual(stage2["confirmed_perf_related"], 0)
                self.assertEqual(manifest["output_skipped_count"], 0)
                self.assertEqual(self.called_names(), [param["name"]])

    def test_legacy_processed_without_recorded_outcome_cannot_prove_none(self):
        param = parameter()
        self.configure([param], {param["name"]: yaml_text(card(param, "none"))})
        first, _ = self.run_builder()
        self.assertEqual(first.returncode, 0, first.stderr)
        progress_path = self.output / "progress.json"
        progress = json.loads(progress_path.read_text())
        # Preserve the old published progress shape. It recorded completion but
        # carried no successful-none evidence, so it cannot prove that judgment.
        legacy_keys = ("stage1_passed", "stage1_skipped", "processed_params", "error_params", "total_stage2", "completed_stage2")
        progress_path.write_text(json.dumps({key: progress[key] for key in legacy_keys}), encoding="utf-8")
        result, manifest = self.run_builder(resume=True)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assert_counts(manifest, analyzed=1, perf=0, none=0, errors=1, skipped=0)
        self.assertEqual(self.called_names(), [param["name"]])


if __name__ == "__main__":
    unittest.main()

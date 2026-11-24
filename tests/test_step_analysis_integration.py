"""Offline operator acceptance. Use --make-demo NEW_DIRECTORY to keep a synthetic A/B case."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluate.tests import test_validate_run as benchmark
from evaluate.scripts import benchmark as client

ANALYSIS = ROOT / "scripts" / "step_analysis.py"
ADAPTER = ROOT / "diagnose" / "scripts" / "step_sources.py"


def command(script, *args):
    return [sys.executable, "-B", str(script), *map(str, args)]


def write_csv(path, headers, rows):
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def prepare(base, mapping_name, spec_name):
    subprocess.run(command(ADAPTER, "prepare", "--run-dir", base / "run", "--mapping", base / mapping_name,
                           "--tool-version", "synthetic-engine-export/1", "--output", base / spec_name),
                   check=True, capture_output=True, text=True)


def write_response(run, request, response_id):
    (run / "raw").mkdir()
    parts = ("one two three four five six seven eight", " nine ten eleven twelve thirteen fourteen fifteen sixteen")
    frames = [{"choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}]} for part in parts]
    frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": request["finish_reason"]}]})
    frames.append({"choices": [], "usage": {"prompt_tokens": request["actual_prompt_tokens"],
                                            "completion_tokens": request["actual_output_tokens"]}})
    stream = "".join("data: " + json.dumps(dict(frame, id=response_id)) + "\n\n" for frame in frames)
    (run / request["raw_response_path"]).write_text(stream + "data: [DONE]\n\n", encoding="utf-8")
    output = {"content": "".join(parts), "reasoning_content": ""}
    (run / request["output_path"]).write_text(json.dumps(output), encoding="utf-8")


def create_run(base, end, step_count, config_hash):
    # One synthetic request emits multiple committed tokens per speculative scheduling iteration.
    run = base / "run"
    run.mkdir(parents=True)
    run_id = "synthetic-" + base.name
    manifest = benchmark.manifest()
    policy = dict(manifest["client_measurement_policy"], model="synthetic-model",
                  temperature=0.0, top_p=1.0, seed=42, ignore_eos=False)
    manifest.update(run_id=run_id, end_ts=end, run_purpose="diagnostic", profiler_enabled=True,
                    config_sha256=config_hash, base_url="http://synthetic.invalid/v1",
                    client_measurement_policy=policy, client_measurement_policy_sha256=benchmark.MODULE.canonical_hash(policy))
    request = dict(benchmark.request(), run_id=run_id, requested_max_tokens=16, end_ts=end,
                   http_status=200, raw_request_path="raw/00000000.request.json",
                   raw_response_path="raw/00000000.sse", output_path="raw/00000000.output.json",
                   last_output_ts=end - 1, output_event_ts=[2.0, end - 1])
    fixture = benchmark.ValidateRunTest()
    fixture.run = run
    fixture.write(manifest, [request])
    response_id = "chatcmpl-" + run_id
    write_response(run, request, response_id)
    job = json.loads((run / "workload.json").read_text(encoding="utf-8"))[0]
    payload = client.request_payload(job, argparse.Namespace(**policy))
    (run / request["raw_request_path"]).write_text(json.dumps(payload), encoding="utf-8")
    subprocess.run(command(ANALYSIS, "request-map", "--run", run, "--output", base / "request-map.csv"),
                   check=True, capture_output=True, text=True)

    write_csv(base / "steps.csv", ["run", "step"],
              [(run_id, idx) for idx in range(step_count)])
    tokens_per_step = request["actual_output_tokens"] // step_count
    write_csv(base / "commits.csv", ["run", "step", "serving_id", "first_position", "committed_count"],
              [(run_id, idx, response_id, idx * tokens_per_step, tokens_per_step) for idx in range(step_count)])
    write_csv(base / "window_counts.csv", ["run", "engine_reported_step_count"], [[run_id, step_count]])
    owner = {"replica": "0", "owner_rank": "0"}
    mapping = {
        "scope": "full_run", "step_definition": "One completed speculative scheduler iteration per replica",
        "owner_ranks": {"0": "0"}, "tables": [
            {"kind": "request_map", "path": "request-map.csv",
             "columns": {k: k for k in ("run_id", "manifest_sha256", "request_id", "response_id", "sse_sha256")}},
            {"kind": "inventory", "path": "steps.csv", "constants": owner,
             "columns": {"run_id": "run", "step_id": "step"}},
            {"kind": "outputs", "path": "commits.csv", "constants": owner,
             "columns": {"run_id": "run", "step_id": "step", "response_id": "serving_id",
                         "output_start": "first_position", "output_count": "committed_count"}},
            {"kind": "seal", "path": "window_counts.csv", "constants": {"replica": "0"},
             "columns": {"run_id": "run", "expected_steps": "engine_reported_step_count"}},
        ],
    }
    (base / "mapping.json").write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    prepare(base, "mapping.json", "sources.json")


def make_demo(folder):
    folder.mkdir(parents=True, exist_ok=False)
    create_run(folder / "A", end=10.0, step_count=4, config_hash="b" * 64)
    create_run(folder / "B", end=8.0, step_count=2, config_hash="c" * 64)


def compare_command(folder, report):
    a, b = folder / "A", folder / "B"
    return command(ANALYSIS, "compare", "--baseline", a / "run", "--candidate", b / "run",
                   "--baseline-sources", a / "sources.json", "--candidate-sources", b / "sources.json",
                   "--output", report)


class StepAnalysisIntegration(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name).resolve()
        self.demo = self.temp / "demo"
        make_demo(self.demo)
        self.a, self.b = self.demo / "A", self.demo / "B"
        self.output = self.temp / "report.json"

    def invoke(self, args, expect_success=True):
        result = subprocess.run(args, cwd=self.temp, capture_output=True, text=True)
        if expect_success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(self.output.read_text(encoding="utf-8"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        return result.stderr

    def run_command(self, run, spec=None):
        args = command(ANALYSIS, "run", "--run", run, "--output", self.output)
        return args if spec is None else args + ["--sources", str(spec)]

    def test_full_comparison_and_token_conservation(self):
        report = self.invoke(compare_command(self.demo, self.output))
        rows = report["comparison"]["rows"]
        expected = {"N": (16, 16), "T": (10, 8), "K": (4, 2), "R": (0.4, 0.25),
                    "Y_bar": (4, 8), "output_token_throughput": (1.6, 2)}
        for metric, (baseline, candidate) in expected.items():
            with self.subTest(metric=metric):
                self.assertAlmostEqual(report["baseline"]["metrics"][metric], baseline)
                self.assertAlmostEqual(report["candidate"]["metrics"][metric], candidate)
                self.assertAlmostEqual(rows[metric]["baseline"], baseline)
                self.assertAlmostEqual(rows[metric]["candidate"], candidate)
                self.assertAlmostEqual(rows[metric]["delta"], candidate - baseline)
                self.assertAlmostEqual(rows[metric]["ratio"], candidate / baseline)
        for name in ("baseline", "candidate"):
            side = report[name]
            self.assertEqual(side["coverage"]["status"], "complete")
            self.assertEqual(side["coverage"]["mapped_output_tokens"], 16)
            self.assertTrue(side["coverage"]["inventory_evidence"]["path"].endswith("window_counts.csv"))
            self.assertAlmostEqual(side["metrics"]["R"] * side["metrics"]["Y_bar"],
                                   side["metrics"]["output_token_throughput"])

    def test_run_without_an_engine_source_preserves_external_measurements(self):
        report = self.invoke(self.run_command(self.a / "run"))
        self.assertEqual(report["metrics"]["N"], 16)
        self.assertEqual(report["metrics"]["T"], 10)
        self.assertEqual(report["coverage"]["status"], "unavailable")
        self.assertTrue(all(report["metrics"][key] is None for key in ("K", "R", "Y_bar")))

    def test_partial_export_with_all_positions_keeps_step_factors_unknown(self):
        mapping = json.loads((self.a / "mapping.json").read_text(encoding="utf-8"))
        mapping["scope"] = "partial"
        (self.a / "partial.json").write_text(json.dumps(mapping), encoding="utf-8")
        prepare(self.a, "partial.json", "partial-sources.json")
        report = self.invoke(self.run_command(self.a / "run", self.a / "partial-sources.json"))
        self.assertEqual(report["coverage"]["status"], "partial")
        self.assertEqual(report["coverage"]["mapped_output_tokens"], 16)
        self.assertEqual(report["metrics"]["output_token_throughput"], 1.6)
        self.assertTrue(all(report["metrics"][key] is None for key in ("K", "R", "Y_bar")))

    def test_changed_manifest_of_same_run_id_rejects_the_original_binding(self):
        manifest = self.a / "run" / "manifest.json"
        # Same contents and run_id. Changed bytes invalidate the receipt bound by prepare.
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        self.assertIn("run", self.invoke(self.run_command(self.a / "run", self.a / "sources.json"), False).lower())

    def test_changed_raw_stream_invalidates_frozen_response_map(self):
        stream = self.a / "run" / "raw" / "00000000.sse"
        stream.write_bytes(b": changed after request map creation\n\n" + stream.read_bytes())
        self.invoke(self.run_command(self.a / "run", self.a / "sources.json"), False)

    def test_per_request_slo_goodput_uses_the_original_batch_window(self):
        report = self.invoke(compare_command(self.demo, self.output) + ["--max-e2e-s", "9"])
        for side, output, wall in (("baseline", 0, 10), ("candidate", 16, 8)):
            measured = report[side]["goodput"]
            self.assertEqual(measured["qualifying_output_tokens"], output)
            self.assertEqual(measured["wall_s"], wall)
            self.assertEqual(measured["output_tokens_per_s"], output / wall)

    def test_missing_visible_count_preserves_both_rates_in_diagnostic_comparison(self):
        report = self.invoke(compare_command(self.demo, self.output) +
                             ["--token-source", "server_content_tokens", "--first-output-channel", "content",
                              "--max-e2e-s", "10"])
        rows = report["comparison"]["rows"]
        self.assertIsNone(rows["goodput"]["baseline"])
        self.assertIsNone(rows["goodput"]["candidate"])
        self.assertEqual(rows["qualified_requests_per_s"]["baseline"], 0.1)
        self.assertEqual(rows["qualified_server_completion_tokens_per_s"]["candidate"], 2)

    def test_exact_quality_goodput_replays_content_and_retains_external_tokens(self):
        gold = self.temp / "gold.json"
        gold.write_text(json.dumps({"r1": "intentionally different answer"}), encoding="utf-8")
        reference = {"path": gold.name, "sha256": hashlib.sha256(gold.read_bytes()).hexdigest()}
        rule = {"method": "exact_match", "field": "content", "reference": reference}
        sample = json.loads((ROOT / "assets" / "round-exact.example.json").read_text(encoding="utf-8"))
        sample["scenarios"] = {"single": next(iter(sample["scenarios"].values()))}
        sample["quality_policy"] = {"scenarios": {"single": rule}}
        contract = self.temp / "contract.json"
        contract.write_text(json.dumps(sample), encoding="utf-8")
        report = self.invoke(self.run_command(self.a / "run", self.a / "sources.json")
                             + ["--quality-contract", str(contract)])
        self.assertEqual(report["metrics"]["N"], 16)
        self.assertEqual(report["goodput"]["qualifying_output_tokens"], 0)
        self.assertEqual(report["goodput"]["quality_evaluation"]["mismatch_or_failure_ids"], ["r1"])


if __name__ == "__main__":
    if "--make-demo" in sys.argv[1:]:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--make-demo", type=Path, required=True, metavar="NEW_DIRECTORY")
        target = parser.parse_args().make_demo.resolve()
        try:
            make_demo(target)
        except (FileExistsError, subprocess.CalledProcessError) as exc:
            parser.exit(2, str(getattr(exc, "stderr", None) or exc) + "\n")
        print("Synthetic offline A/B example. Run the following command to write report.json.")
        print(shlex.join(compare_command(target, target / "report.json")))
    else:
        unittest.main()

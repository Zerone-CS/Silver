"""Exercise public tools with a loopback service and synthetic Ascend trace exports."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "scripts/campaign.py"
PROFILE = ROOT / "diagnose/scripts/profile.py"
STEP_SOURCES = ROOT / "diagnose/scripts/step_sources.py"
EXPERIENCE = ROOT / "scripts/experience.py"
EVIDENCE = ROOT / "scripts/round_evidence.py"
EVALUATE = ROOT / "evaluate/scripts"
PLAN = ROOT / "scripts/trial_plan.py"
BUDGET = ROOT / "scripts/device_budget.py"


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class FixtureService(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path != "/fixture/deployment":
            self.send_error(404)
            return
        data = json.dumps(self.server.deployment).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request["stream"]:
            # A deliberate mock-service delay produces a repeatable A/B signal.
            time.sleep(self.server.delay)
            if self.server.profile_export:
                path, capture_id = self.server.profile_export
                write(path, {"capture_id": capture_id, "traceEvents": [
                    {"ph": "i", "ts": 0, "name": "start", "args": {"rank": 0}},
                    {"ph": "X", "ts": 3, "dur": 4 if self.server.delay else 2, "pid": 1, "tid": 1,
                     "name": "MatMul", "cat": "kernel", "args": {"logical_step": "0"}},
                    {"ph": "i", "ts": 10, "name": "end"}]})
            chunks = [
                {"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            media = "text/event-stream"
        else:
            body = json.dumps({"id": "fixture-health", "model": request["model"],
                               "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
            media = "application/json"
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class CampaignIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.campaign = self.root / "campaign"
        self.db = self.root / "experience.sqlite"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureService)
        self.server.delay = 0
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.dataset = write(self.root / "dataset.jsonl", {"request_id": "r1", "prompt_text": "OK?", "output_tokens": 8})
        self.environment = write(self.root / "environment.json", {"hardware": "loopback fixture"})
        self.metric = write(self.root / "metric.json", {"units": "server output tokens per second"})
        reference = write(self.root / "gold.json", {"r1": "OK"})
        rule = {"method": "exact_match", "field": "content", "reference": {
            "path": reference.name, "sha256": hashlib.sha256(reference.read_bytes()).hexdigest()}}
        self.contract = write(self.root / "contract.json", {
            "schema_version": 1, "min_pairs": 2, "confidence": 0.95, "seed": 17, "bootstrap_samples": 100,
            "scenarios": {"offline": {"primary": {"metric": "output_token_throughput", "direction": "higher",
                                                  "min_relative_improvement": 0.02},
                                      "guardrails": [{"metric": "failure_rate", "max_value": 0.0}]}},
            "quality_policy": {"scenarios": {"offline": rule}}})
        self.specs = {}
        for side, delay in (("baseline", 0.20), ("candidate", 0)):
            self.deployment(side, delay)

    def call(self, script, *args, fail=False):
        command = [sys.executable, "-B", str(script), *map(str, args)]
        result = subprocess.run(command, cwd=self.root, text=True, capture_output=True, timeout=20)
        if fail:
            self.assertNotEqual(result.returncode, 0, command)
            return result.stderr
        self.assertEqual(result.returncode, 0, f"{command}\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def campaign_cmd(self, name, *args, fail=False):
        output = self.call(CAMPAIGN, name, "--campaign", self.campaign, *args, fail=fail)
        return output if fail else json.loads(output)

    def deployment(self, side, delay):
        directory = self.root / side
        directory.mkdir()
        config = {"fixture_delay": delay}
        write(directory / "config.json", config)
        observed = write(directory / "observation.json", {
            "schema_version": 1, "instance_id": "fixture-" + side, "base_url": self.url,
            "model": "fixture-model", "effective_config": config, "implementation_sha256": None})
        log = directory / "service.log"
        log.write_text("Fixture service delay: " + str(delay), encoding="utf-8")
        report = write(directory / "config-evidence.json", {
            "verified": True, "scope": "loopback", "observation": observed.name,
            "evidence": [observed.name, log.name]})
        for purpose, mode in (("performance", "off"), ("diagnostic", "on")):
            self.call(EVALUATE / "prepare_metadata.py", "--scenario", "offline", "--config", directory / "config.json",
                      "--config-evidence", report, "--environment", self.environment, "--metric-contract", self.metric,
                      "--profiler-enabled", mode, "--run-purpose", purpose, "--output", directory / (purpose + ".json"))
        self.specs[side] = write(directory / "profile.json", {
            "schema_version": 1, "format": "chrome-trace-v1", "tool": {"name": "torch_npu.profiler", "version": "fixture"},
            "diagnostic_run": "diagnostic", "traces": {"0": "trace_view.json"},
            "run_binding": {"scope": "document", "path": "capture_id", "source": "profile_capture_id"},
            "rank_field": {"scope": "event", "path": "args.rank"},
            "window": {"clock": "fixture-us", "start_event": "start", "end_event": "end"},
            "requirements": [{"id": "kernel", "field": "cat", "pattern": "^kernel$", "min_count": 1}]})

    def collect(self, side, name, purpose="performance", metadata=None):
        directory = self.root / side
        self.server.deployment = json.loads((directory / "observation.json").read_text())
        self.server.delay = self.server.deployment["effective_config"]["fixture_delay"]
        self.server.profile_export = None
        if purpose == "diagnostic":
            self.server.profile_export = (directory / "trace_view.json", json.loads(metadata.read_text())["profile_capture_id"])
        run = directory / name
        before = time.monotonic_ns()
        self.call(EVALUATE / "benchmark.py", "--base-url", self.url, "--model", "fixture-model",
                  "--dataset", self.dataset, "--metadata", metadata or directory / (purpose + ".json"),
                  "--run-dir", run, "--concurrency", 1, "--workers", 1, "--timeout", 4)
        if getattr(self, "fixture_clock", False):
            # The test process brackets each separate collector invocation on
            # one clock. Older macOS Python uses process-local perf_counter().
            manifest_path = run / "manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            manifest["client_run_order"] = {"clock": "host_monotonic_ns", "hostname": "loopback",
                                            "boot_id": "fixture", "start_ns": before,
                                            "end_ns": time.monotonic_ns()}
            write(manifest_path, manifest)
        return run

    def begin(self, *extra):
        self.campaign_cmd("init", "--baseline-run", self.collect("baseline", "initial"), "--contract", self.contract,
                          "--scope", "loopback W5", "--max-rounds", 2, *extra)
        policy = write(self.root / "policy.json", json.loads(self.call(PROFILE, "policy", "--spec", self.specs["baseline"])))
        self.campaign_cmd("begin", "--profile-policy", policy, "--round", 1, "--scenario", "offline")

    def step_arguments(self, side):
        directory = self.root / side
        run = directory / "diagnostic"
        run_id = json.loads((run / "manifest.json").read_bytes())["run_id"]
        owner = {"replica": "0", "owner_rank": "0"}
        tables = []
        for name, columns, values, constants in (
            ("inventory", ["run_id", "step_id", "duration_us"], [run_id, "0", "4"], owner),
            ("outputs", ["run_id", "step_id", "request_id", "output_start", "output_count"],
             [run_id, "0", "r1", "0", "1"], owner),
            ("seal", ["run_id", "expected_steps"], [run_id, "1"], {"replica": "0"}),
        ):
            path = directory / (name + ".csv")
            path.write_text(",".join(columns) + "\n" + ",".join(values) + "\n", encoding="utf-8")
            tables.append({"kind": name, "path": path.name, "columns": {k: k for k in columns}, "constants": constants})
        mapping = write(directory / "table-map.json", {"scope": "full_run", "step_definition": "fixture scheduler step",
                        "owner_ranks": {"0": "0"}, "tables": tables})
        sources = directory / "sources.json"
        self.call(STEP_SOURCES, "prepare", "--run-dir", run, "--mapping", mapping,
                  "--tool-version", "fixture", "--output", sources)
        proof = directory / "clock.log"
        proof.write_text("Synthetic fixture writes all events on the fixture-us clock.", encoding="utf-8")
        clock = write(directory / "clock.json", {"run_id": run_id, "clock": "fixture-us", "ranks": ["0"],
                      "method": "fixture", "evidence": {"path": proof.name,
                      "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()}})
        timeline = write(directory / "timeline.json", {"schema_version": 1, "tool": {"name": "torch_npu.profiler",
                         "version": "fixture"}, "step_field": "args.logical_step", "replica_by_rank": {"0": "0"},
                         "rules": [{"stage": "device", "field": "cat", "pattern": "kernel"}],
                         "clock_alignment": {"path": clock.name, "sha256": hashlib.sha256(clock.read_bytes()).hexdigest()}})
        return sources, timeline, run

    def profile(self, side, with_steps=False):
        directory = self.root / side
        stamped = directory / "diagnostic-stamped.json"
        self.campaign_cmd("stamp", "--round", 1, "--metadata", directory / "diagnostic.json", "--output", stamped)
        self.collect(side, "diagnostic", "diagnostic", metadata=stamped)
        bundle = self.root / side / "bundle"
        if side == "candidate":
            trace = directory / "trace_view.json"
            fresh = trace.read_bytes()
            trace.write_bytes((self.root / "baseline/trace_view.json").read_bytes())
            stale = directory / "stale-bundle"
            self.call(PROFILE, "capture", "--spec", self.specs[side], "--out", stale, fail=True)
            self.campaign_cmd("profile", "--bundle", stale, "--round", 1, fail=True)
            self.assertEqual(self.campaign_cmd("status")["next_action"], "candidate_profile")
            trace.write_bytes(fresh)
        self.call(PROFILE, "capture", "--spec", self.specs[side], "--out", bundle)
        verified = json.loads(self.call(PROFILE, "verify", "--bundle", bundle))
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["run_binding_status"], "trace_field_verified")
        args = []
        if with_steps:
            sources, timeline, run = self.step_arguments(side)
            args = ["--step-sources", sources, "--timeline-map", timeline, "--diagnostic-run", run]
        if with_steps and side == "candidate":
            self.campaign_cmd("profile", "--bundle", bundle, "--round", 1,
                              "--diagnostic-run", self.root / "baseline/diagnostic", fail=True)
            self.assertEqual(self.campaign_cmd("status")["next_action"], "candidate_profile")
            # Simulate files left behind between atomic report publication and the SQLite commit.
            standalone = json.loads(self.call(ROOT / "scripts/step_analysis.py", "run", "--bundle", bundle,
                                    "--sources", sources, "--timeline-map", timeline, "--run", run))
            payload = (json.dumps(standalone, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()
            filename = hashlib.sha256(payload).hexdigest() + ".json"
            prior = self.campaign / "diagnosis" / filename
            prior.write_bytes(b"changed content")
            self.assertIn("evidence changed", self.campaign_cmd("profile", "--bundle", bundle, "--round", 1,
                          *args, fail=True))
            self.assertEqual(prior.read_bytes(), b"changed content")
            prior.write_bytes(payload)
            before = prior.stat()
        result = self.campaign_cmd("profile", "--bundle", bundle, "--round", 1, *args)
        if with_steps:
            receipt = result["active"]["profiles"][side]["step_analysis"]
            self.assertEqual(receipt["path"], str(self.campaign.resolve() / "diagnosis" / (receipt["sha256"] + ".json")))
            raw = Path(receipt["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt["sha256"])
            report = json.loads(raw)
            self.assertEqual(report["coverage"]["status"], "complete")
            self.assertEqual(report["metrics"]["N"], 1)
            self.assertEqual(report["timeline"]["coverage"]["attributed_spans"], 1)
            self.assertEqual(self.campaign_cmd("profile", "--bundle", bundle, "--round", 1, *args), result)
            if side == "candidate":
                self.assertEqual(prior.stat().st_ino, before.st_ino)
                self.assertEqual(prior.stat().st_mtime_ns, before.st_mtime_ns)
        return result

    def pairs(self, name):
        pairs = []
        for number in range(2):
            pair = {"scenario": "offline", **{side: str(self.root / side / (name + str(number)))
                                               for side in ("baseline", "candidate")}}
            pairs.append(pair)
        trial = write(self.root / (name + ".json"), {"schema_version": 1, "pairs": pairs})
        self.campaign_cmd("schedule", "--round", 1, "--evidence", trial)
        self.campaign_cmd("stamp", "--round", 1, "--metadata", self.root / "baseline/performance.json",
                          "--run-dir", self.root / "unscheduled", "--output", self.root / "wrong-slot.json", fail=True)
        for pair in pairs:
            for side in ("baseline", "candidate"):
                target = Path(pair[side])
                stamped = target.with_suffix(".metadata.json")
                self.campaign_cmd("stamp", "--round", 1, "--metadata", target.parent / "performance.json",
                                  "--output", stamped, "--run-dir", target)
                self.collect(side, target.name, metadata=stamped)
        return trial

    def health(self, side, filename):
        receipt = self.root / filename
        deployment = receipt.with_suffix(".deployment.json")
        probe = "import sys; from urllib.request import urlopen; print(urlopen(sys.argv[1]).read().decode())"
        self.campaign_cmd("observe", "--round", 1, "--output", deployment, "--", sys.executable,
                          "-c", probe, self.url.removesuffix("/v1") + "/fixture/deployment")
        self.campaign_cmd("health", "--side", side, "--deployment", deployment, "--round", 1, "--output", receipt)
        return ("--deployment", deployment, "--health", receipt)

    def test_independent_confirmation_adoption_and_memory(self):
        self.begin("--goal-scenario", "offline", "--goal-tps", 12)
        self.assertEqual(self.profile("baseline", with_steps=True)["next_action"], "plan")
        change = self.root / "change.txt"
        change.write_text("Set fixture delay to zero. Restore the original delay on failure.")
        self.campaign_cmd("plan", "--hypothesis", "sleep delays completion", "--prediction", "time per request drops",
                          "--round", 1, "--change", change, "--config", self.root / "candidate/config.json")
        result = self.profile("candidate", with_steps=True)
        self.assertEqual(result["next_action"], "schedule")
        comparison = result["active"]["step_comparison"]
        self.assertEqual(json.loads(Path(comparison["path"]).read_bytes())["rows"]["N"]["ratio"], 1)
        discovery = self.pairs("discovery")
        self.assertEqual(self.campaign_cmd("assess", "--evidence", discovery, "--round", 1)["next_action"],
                         "schedule_confirmation")
        self.campaign_cmd("schedule", "--evidence", discovery, "--round", 1, fail=True)
        self.assertEqual(self.campaign_cmd("status")["next_action"], "schedule_confirmation")
        premature = self.health("candidate", "early-health.json")
        self.campaign_cmd("close", "--decision", "adopt", "--observation", "gain", "--round", 1, *premature, fail=True)
        confirmation = self.pairs("confirmation")
        identities = []
        for side in ("baseline", "candidate"):
            run = self.root / side / "confirmation0"
            summary = json.loads(self.call(EVIDENCE, "describe-run", run))
            identities.append(write(self.root / (side + "-id.json"), summary["identity"]))
        checked = json.loads(self.call(EVIDENCE, "check", self.contract, confirmation, *identities))
        self.assertEqual(checked["recommendation"], "adopt")
        self.assertEqual(self.campaign_cmd("assess", "--evidence", confirmation, "--round", 1)["next_action"], "accept")
        self.campaign_cmd("close", "--decision", "adopt", "--observation", "gain", "--round", 1, *premature, fail=True)
        health = self.health("candidate", "candidate-health.json")
        receipt = self.root / "candidate-health.json"
        original = receipt.read_bytes()
        forged = json.loads(original)
        forged["response"]["id"] = "fixture-response-not-from-the-health-check"
        write(receipt, forged)
        self.campaign_cmd("close", "--decision", "adopt", "--observation", "gain", "--round", 1, *health, fail=True)
        receipt.write_bytes(original)
        field_export = self.root / "baseline/inventory.csv"
        original_export = field_export.read_bytes()
        field_export.write_bytes(original_export + b"\n")
        self.assertIn("evidence changed", self.campaign_cmd("close", "--decision", "adopt", "--observation", "gain",
                      "--round", 1, *health, fail=True))
        field_export.write_bytes(original_export)
        final = self.campaign_cmd("close", "--decision", "adopt", "--observation", "gain", "--round", 1, *health)
        self.assertEqual(final["stop_reason"], "goal_reached")
        exported = self.campaign_cmd("export", "--round", 1)
        self.assertEqual(exported["final_deployment"]["identity"], exported["candidate"])
        self.assertIn("reverted or abandoned", self.campaign_cmd("export", "--round", 1, "--recovery-only", fail=True))
        self.assertEqual(len(exported["evidence"]["assessments"]), 2)
        self.assertEqual(exported["evidence"]["step_comparison"], comparison)
        result = json.loads(self.call(EXPERIENCE, "add", "--db", self.db, "--campaign", self.campaign, "--round", 1))
        self.assertTrue(result["added_round"])
        found = json.loads(self.call(EXPERIENCE, "search", "--db", self.db, "--scope", "W5", "--decision", "adopt"))
        self.assertEqual(found[0]["round_record"], exported)
        for source in ("inventory.csv", "sources.json", "diagnostic/manifest.json", "clock.log", "timeline.json"):
            with self.subTest(changed_diagnostic_evidence=source):
                path = self.root / "candidate" / source
                contents = path.read_bytes()
                path.write_bytes(contents + b"\n")
                self.assertIn("evidence changed", self.campaign_cmd("export", "--round", 1, fail=True))
                path.write_bytes(contents)
        for ref in (comparison, exported["evidence"]["profiles"]["baseline"]["step_analysis"]):
            path = Path(ref["path"])
            contents = path.read_bytes()
            path.write_bytes(contents + b"\n")
            self.campaign_cmd("export", "--round", 1, fail=True)
            path.write_bytes(contents)
        trace = self.root / "candidate/bundle/raw/rank-0.json"
        trace.write_text("[]", encoding="utf-8")
        self.campaign_cmd("export", "--round", 1, fail=True)
        self.call(EXPERIENCE, "add", "--db", self.db, "--campaign", self.campaign, "--round", 1, fail=True)

    def test_stop_before_plan_requires_healthy_baseline_and_survives_restart(self):
        self.begin("--goal-scenario", "offline", "--goal-tps", 1)
        stopped = self.campaign_cmd("stop", "--reason", "user requested stop")
        self.assertEqual(stopped["next_action"], "close_at_baseline")
        self.campaign_cmd("export", "--round", 1, fail=True)
        self.campaign_cmd("begin", "--round", 2, "--scenario", "offline", "--profile-policy",
                          self.root / "policy.json", fail=True)
        receipt = self.health("baseline", "restored-health.json")
        self.campaign_cmd("close", "--round", 1, "--decision", "adopt", "--observation", "stop", *receipt, fail=True)
        result = self.campaign_cmd("close", "--round", 1, "--decision", "abandon", "--observation", "stop", *receipt)
        self.assertEqual(result["stop_reason"], "user requested stop")
        recovery = self.campaign_cmd("export", "--round", 1, "--recovery-only")
        self.assertEqual(recovery["decision"], "abandon")
        self.assertIsNone(recovery["candidate"])
        self.assertEqual(self.campaign_cmd("status")["next_action"], "stopped")
        self.call(EXPERIENCE, "add", "--db", self.db, "--campaign", self.campaign, "--round", 1)
        item = json.loads(self.call(EXPERIENCE, "search", "--db", self.db, "--decision", "abandon"))[0]["round_record"]
        self.assertIsNone(item["candidate"])
        self.assertEqual(item["final_deployment"]["identity"], item["baseline"])

    def test_goodput_primary_is_scored_during_initialization_and_both_trials(self):
        contract = json.loads(self.contract.read_bytes())
        contract["scenarios"]["offline"]["primary"]["metric"] = "goodput"
        contract["goodput_policy"] = {"scenarios": {"offline": {"max_ttft_s": 4, "max_e2e_s": 4}}}
        write(self.contract, contract)
        self.begin()
        self.assertEqual(self.profile("baseline")["next_action"], "plan")
        change = self.root / "change.txt"
        change.write_text("Remove fixture delay and keep its original value for rollback.", encoding="utf-8")
        self.campaign_cmd("plan", "--round", 1, "--hypothesis", "fixture delay limits goodput",
                          "--prediction", "eligible output arrives faster", "--change", change,
                          "--config", self.root / "candidate/config.json")
        self.assertEqual(self.profile("candidate")["next_action"], "schedule")
        for stage, action in (("discovery", "schedule_confirmation"), ("confirmation", "accept")):
            self.assertEqual(self.campaign_cmd("assess", "--evidence", self.pairs(stage), "--round", 1)["next_action"],
                             action)
        health = self.health("candidate", "goodput-health.json")
        self.campaign_cmd("close", "--decision", "adopt", "--observation", "goodput gain", "--round", 1, *health)
        exported = self.campaign_cmd("export", "--round", 1)
        self.assertEqual(exported["decision"], "adopt")
        assessments = exported["evidence"]["assessments"]
        self.assertEqual(len(assessments), 2)
        for result in assessments:
            report = result["comparison"]["scenarios"]["offline"]
            self.assertEqual((report["primary"]["metric"], report["primary"]["status"]), ("goodput", "passed"))
            self.assertEqual((report["guardrails"][0]["metric"], report["guardrails"][0]["status"]),
                             ("failure_rate", "passed"))
            self.assertEqual(set(result["goodput"]), set(result["run_ids"]))
            for run in result["comparison"]["validated_inputs"]:
                self.assertEqual(run["metrics"]["goodput"], run["metrics"]["output_token_throughput"])

    def test_frozen_trial_plan_checks_independent_confirmation_and_goodput_target(self):
        self.fixture_clock = True
        contract = json.loads(self.contract.read_bytes())
        scorer = self.root / "score.py"
        scorer.write_text('''import json, sys
items = json.load(sys.stdin)["items"]
scores = {r["request_id"]: int(r["status"] == "success" and r["output"]["content"] == r["reference"]) for r in items}
print(json.dumps({"schema_version": 1, "scores": scores}))
''')
        contract["quality_policy"]["scenarios"]["offline"] = {
            "method": "python_score_v1", "min_mean_score": 1, "timeout_s": 4,
            "reference": contract["quality_policy"]["scenarios"]["offline"]["reference"],
            "scorer": {"path": scorer.name, "sha256": hashlib.sha256(scorer.read_bytes()).hexdigest()}}
        contract["goodput_policy"] = {"scenarios": {"offline": {"max_ttft_s": 1, "max_e2e_s": 1, "min_request_score": 1}}}
        write(self.contract, contract)
        meter = self.root / "device-budget.sqlite"
        self.call(BUDGET, "init", "--db", meter, "--limit-die-seconds", 800)
        self.begin("--require-trial-plan", "--goal-scenario", "offline", "--goal-tps", 10,
                   "--goal-metric", "goodput", "--device-budget", meter)
        start = self.campaign_cmd("status")
        self.assertEqual(start["goal"]["metric"], "goodput")
        self.assertEqual(start["device_budget"]["remaining_die_seconds"], 800)
        second = "baseline-aa"
        self.deployment(second, 0.20)
        other_run = self.collect(second, "repeat-aa")
        same_deployment = self.collect("baseline", "same-aa")
        initial_run = self.root / "baseline/initial"
        bad_calibration = self.root / "invalid-calibration.json"
        self.assertIn("instance reused", self.call(PLAN, "calibrate", "--contract", self.contract, "--out",
                      bad_calibration, "--baseline-run", initial_run, "--baseline-run", same_deployment, fail=True))
        calibration = self.root / "calibration.json"
        aa = json.loads(self.call(PLAN, "calibrate", "--contract", self.contract, "--out", calibration,
                                  "--baseline-run", initial_run, "--baseline-run", other_run))
        self.assertGreaterEqual(aa["recommended_confirm_blocks"], 4)
        self.profile("baseline")
        change = self.root / "change.txt"
        change.write_text("Remove mock delay and restore 0.20 s on rollback.", encoding="utf-8")
        self.campaign_cmd("plan", "--round", 1, "--hypothesis", "fixture delay limits goodput",
                          "--prediction", "response arrival accelerates", "--change", change,
                          "--config", self.root / "candidate/config.json")
        self.profile("candidate")
        for stage, expected in (("screen", "schedule_confirmation"), ("confirm", "accept")):
            work = self.root / stage
            plan = json.loads(self.call(PLAN, "plan", "--out", work, "--contract", self.contract,
                                        "--stage", stage, "--calibration", calibration, "--seed", 41))
            pairs_file, plan_file = work / "pairs.json", work / "plan.json"
            absent = self.campaign_cmd("schedule", "--round", 1, "--evidence", pairs_file, fail=True)
            self.assertIn("frozen --plan", absent)
            self.campaign_cmd("schedule", "--round", 1, "--evidence", pairs_file, "--plan", plan_file)
            for index, slot in enumerate(plan["slots"]):
                is_baseline = slot["side"] == "baseline"
                deployment = f"{stage}-instance-{index:02d}"
                self.deployment(deployment, 0.20 if is_baseline else 0)
                metadata = self.root / deployment / "performance.json"
                stamped = metadata.with_suffix(".stamped.json")
                run = work / slot["path"]
                run.parent.mkdir(parents=True, exist_ok=True)
                self.campaign_cmd("stamp", "--round", 1, "--run-dir", run, "--metadata", metadata,
                                  "--output", stamped)
                self.collect(deployment, run, metadata=stamped)
            if stage == "screen":
                last_screen = work / plan["slots"][-1]["path"] / "manifest.json"
                screen_end = json.loads(last_screen.read_bytes())["client_run_order"]["end_ns"]
            else:
                # Each batch remains internally ordered and all instance IDs
                # remain fresh. Only the screen/confirmation boundary overlaps.
                target = work / plan["slots"][0]["path"] / "manifest.json"
                original = target.read_bytes()
                interrupted = json.loads(original)
                interrupted["client_run_order"]["start_ns"] = screen_end - 1
                write(target, interrupted)
                error = self.campaign_cmd("assess", "--round", 1, "--evidence", pairs_file, fail=True)
                self.assertIn("screen must finish", error)
                target.write_bytes(original)
            self.assertEqual(self.campaign_cmd("assess", "--round", 1, "--evidence", pairs_file)["next_action"], expected)
        self.server.deployment = json.loads((self.root / "candidate/observation.json").read_bytes())
        health = self.health("candidate", "qualified-output-health.json")
        state = self.campaign_cmd("close", "--decision", "adopt", "--observation", "goodput gain", "--round", 1, *health)
        self.assertEqual(state["stop_reason"], "goal_reached")
        exported = self.campaign_cmd("export", "--round", 1)
        assessments = exported["evidence"]["assessments"]
        execution = assessments[-1]["execution"]
        self.assertTrue(execution["independent_deployments"])
        self.assertEqual(execution["orders"]["offline"]["AB"], execution["orders"]["offline"]["BA"])

        # Relocate the complete evidence, then remove the original site path.
        from scripts import evidence_bundle, round_archive
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as dest, patch.object(evidence_bundle, "assessor_identity",
                                                                  return_value={"release_id": "fixture"}):
            bundle = Path(dest).resolve() / "sealed"
            export_file = write(self.root / "round-export.json", exported)
            result = round_archive.pack(export_file, [self.root], bundle)
            self.assertEqual(result["stage_recommendations"], ["adopt", "adopt"])
            self.assertEqual(result["confirmation_order"], "verified")
            self.assertEqual(result["device_release"], "unverified")
            hidden = self.root.with_name(self.root.name + "-hidden")
            self.root.rename(hidden)
            try:
                self.assertEqual(evidence_bundle.replay(bundle), result)
                exported["evidence"]["assessments"].pop()
                aliases = json.loads((bundle / "bundle.json").read_bytes())["aliases"]
                with self.assertRaisesRegex(ValueError, "adoption lacks required passing stages"):
                    round_archive.audit(exported, round_archive.ArchivedFiles(bundle, aliases))
            finally:
                hidden.rename(self.root)

    def test_goodput_goal_does_not_stop_at_raw_throughput_goal(self):
        contract = json.loads(self.contract.read_bytes())
        contract["goodput_policy"] = {"scenarios": {"offline": {"max_ttft_s": 0.05, "max_e2e_s": 0.05}}}
        write(self.contract, contract)
        runs = [self.collect("baseline", name) for name in ("goal-aa1", "goal-aa2")]
        args = ["--contract", self.contract, "--scope", "fixture SLO goal", "--max-rounds", 2,
                "--goal-tps", 1, "--goal-scenario", "offline"]
        for run in runs:
            args.extend(("--baseline-run", run))
        qualified = self.campaign_cmd("init", *args, "--goal-metric", "goodput")
        self.assertEqual(qualified["next_action"], "begin")
        raw = json.loads(self.call(CAMPAIGN, "init", "--campaign", self.root / "raw-goal", *args))
        self.assertEqual(raw["next_action"], "stopped")
        self.assertEqual(raw["stop_reason"], "initial_goal_reached")

    def test_remaining_device_budget_stops_fresh_work(self):
        from scripts.device_budget import Budget

        # Child CLI reads the same ledger that the trusted site adapter updates.
        meter = self.root / "budget.sqlite"
        budget = Budget.initialize(meter, 100)
        first = self.collect("baseline", "initial-budget")
        initialized = self.campaign_cmd(
            "init", "--baseline-run", first, "--contract", self.contract, "--scope", "fixture budget",
            "--max-rounds", 2, "--device-budget", meter, "--minimum-device-seconds", 20
        )
        self.assertEqual(initialized["next_action"], "begin")
        budget.claim("spent", "fixture", "boot", ["die0"], 10**9, 91)
        proof = write(self.root / "device-free.json", {"free": True, "host": "fixture", "boot_id": "boot",
                      "devices": ["die0"], "observed_mono_ns": 90 * 10**9})
        budget.release("spent", 91 * 10**9, proof, "collected")
        awaiting_stop = self.campaign_cmd("status")
        self.assertEqual(awaiting_stop["next_action"], "stop")
        self.assertEqual(awaiting_stop["device_budget"]["remaining_die_seconds"], 10)
        self.assertEqual(self.campaign_cmd("stop")["stop_reason"], "device_budget")

    def test_stop_after_diagnostic_source_loss_still_requires_healthy_baseline(self):
        self.begin()
        self.profile("baseline", with_steps=True)
        change = self.root / "change.txt"
        change.write_text("Remove fixture delay. Restore original delay on rollback.", encoding="utf-8")
        self.campaign_cmd("plan", "--round", 1, "--hypothesis", "delay limits throughput",
                          "--prediction", "completion time drops", "--change", change,
                          "--config", self.root / "candidate/config.json")
        self.profile("candidate", with_steps=True)
        missing = self.root / "baseline/inventory.csv"
        original = missing.read_bytes()
        missing.unlink()
        stopped = self.campaign_cmd("stop", "--reason", "diagnostic source lost")
        self.assertEqual(stopped["next_action"], "close_at_baseline")
        args = ["--round", 1, "--decision", "revert", "--observation", "restored fixture delay"]
        self.campaign_cmd("close", *args, *self.health("candidate", "still-candidate.json"), fail=True)
        self.server.deployment = json.loads((self.root / "baseline/observation.json").read_bytes())
        self.server.delay = self.server.deployment["effective_config"]["fixture_delay"]
        result = self.campaign_cmd("close", *args, *self.health("baseline", "back-at-baseline.json"))
        self.assertEqual(result["next_action"], "stopped")
        self.assertEqual(result["deployment_last_verified"]["identity"], result["baseline"])
        self.assertIn(missing.name, result["step_analysis_review_error"])
        self.assertEqual(self.campaign_cmd("history")[-1]["details"]["step_analysis_review_error"],
                         result["step_analysis_review_error"])
        self.assertIn(missing.name, self.campaign_cmd("export", "--round", 1, fail=True))
        record = self.campaign_cmd("export", "--round", 1, "--recovery-only")
        self.assertNotIn("evidence", record)
        self.assertEqual(record["stop_reason"], "diagnostic source lost")
        self.assertIn(missing.name, record["step_analysis_review_error"])
        from scripts import evidence_bundle, recovery_archive, round_archive
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as destination, patch.object(evidence_bundle, "assessor_identity",
                                                                      return_value={"release_id": "fixture"}):
            bundle = Path(destination).resolve() / "recovery"
            exported = write(self.root / "recovery.json", record)
            verified = recovery_archive.pack(exported, [self.root], bundle)
            self.assertEqual((verified["comparison"], verified["device_release"]), ("not_verified", "unverified"))
            self.assertIs(verified["baseline_restored"], True)
            self.assertNotIn("recommendation", verified)
            self.assertEqual(len(evidence_bundle.payload_files(bundle)), 3)
            with self.assertRaisesRegex(ValueError, "reverted or abandoned"):
                recovery_archive.audit(dict(record, decision="adopt"), round_archive.ArchivedFiles(bundle))
            with self.assertRaisesRegex(ValueError, "wrong final deployment"):
                recovery_archive.audit(dict(record, baseline=record["candidate"]), round_archive.ArchivedFiles(bundle))
            originals = Path(record["last_health"]["path"])
            raw_health = originals.read_bytes()
            originals.unlink()
            try:
                self.assertEqual(evidence_bundle.replay(bundle), verified)
                self.campaign_cmd("export", "--round", 1, "--recovery-only", fail=True)
                archived = round_archive.ArchivedFiles(bundle)(originals)
                archived.write_bytes(archived.read_bytes() + b"\n")
                with self.assertRaisesRegex(ValueError, "archive bytes changed"):
                    evidence_bundle.replay(bundle)
            finally:
                originals.write_bytes(raw_health)
        missing.write_bytes(original)
        exported = self.campaign_cmd("export", "--round", 1)
        self.assertEqual(exported["evidence"]["step_analysis_review_error"], result["step_analysis_review_error"])

    def test_repeated_baseline_at_goal_stops_without_starting_round(self):
        first = self.collect("baseline", "first-initial")
        second = self.collect("baseline", "second-initial")
        state = self.campaign_cmd("init", "--baseline-run", first, "--baseline-run", second, "--contract", self.contract,
                                  "--scope", "offline", "--max-rounds", 2, "--goal-scenario", "offline", "--goal-tps", 1)
        self.assertEqual(state["stop_reason"], "initial_goal_reached")
        self.assertFalse(state["active_round"])
        self.assertEqual(self.campaign_cmd("status")["next_action"], "stopped")


if __name__ == "__main__":
    unittest.main()

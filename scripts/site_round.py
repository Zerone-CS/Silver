#!/usr/bin/env python3
"""Drive one proposed parameter change through the existing round controller."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ascend_910c as host
from scripts import round_evidence, site_910c, trial_plan
from scripts.device_budget import Budget, InsufficientDeviceBudget
from diagnose.scripts.profile import profile_policy

CONTROL = ROOT / "scripts/campaign.py"


def call(campaign, command, *args, round_number=None):
    options = [] if round_number is None else ["--round", round_number]
    raw = site_910c.invoke(CONTROL, command, "--campaign", campaign, *options, *args)
    return json.loads(raw)


def retain(path, data):
    if path.exists():
        host.require(host.read(path) == data, "frozen inputs changed: " + str(path))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        host.write(path, data)


def profile_spec(site, path, run, prefix, requirements):
    spec = {
        "schema_version": 1, "format": "chrome-trace-v1",
        "tool": {"name": "torch_npu.profiler", "version": site.settings.packages["torch-npu"]},
        "diagnostic_run": str(run),
        "traces": {"0": f"{prefix}/ASCEND_PROFILER_OUTPUT/trace_view.json"},
        "rank_field": {"scope": "ascend-profiler-info", "path": "profiler_info_{rank}.json"},
        "window": {"clock": "single-rank-trace-us", "mode": "trace_extent", "min_duration_us": 1000},
        "requirements": requirements,
    }
    retain(path, spec)
    return spec


class Round:
    def __init__(self, args):
        self.a = args
        self.site = host.Site(args.root, host.SiteConfig.load(args.site), args.template)
        self.campaign = site_910c.site_path(self.site.root, args.campaign)
        self.campaign.mkdir(exist_ok=True)
        self.budget = Budget(args.budget)
        proposal = host.read(args.proposal)
        mandatory = {"schema_version", "scenario", "baseline", "candidate", "hypothesis", "prediction", "change"}
        host.require(set(proposal) in (mandatory, mandatory | {"profile_requirements"})
                     and proposal["schema_version"] == 1, "invalid parameter proposal")
        host.require(proposal["baseline"] != proposal["candidate"], "candidate must differ from baseline")
        self.proposal = proposal
        self.scenario = proposal["scenario"]
        self.change = (args.proposal.parent / proposal["change"]).resolve(strict=True)
        self.requirements = proposal.get("profile_requirements", [
            {"id": "runtime_kernels", "field": "name", "pattern": "(?i)(aten|acl|npu|hcom)", "min_count": 1}
        ])

    def setup(self):
        if not (self.campaign / "campaign.sqlite").exists():
            args = ["--contract", self.a.contract, "--scope", self.a.scope, "--max-rounds", self.a.max_rounds,
                    "--require-trial-plan", "--device-budget", self.a.budget,
                    "--minimum-device-seconds", self.a.max_seconds]
            if self.a.goal_tps is not None:
                args += ["--goal-metric", self.a.goal_metric, "--goal-tps", self.a.goal_tps,
                         "--goal-scenario", self.scenario]
            for path in self.a.baseline_run:
                args.extend(["--baseline-run", path])
            call(self.campaign, "init", *args)

    def control(self, name, *args):
        return call(self.campaign, name, *args, round_number=self.round_number)

    def request(self, action, side):
        return SimpleNamespace(action=action, config_key=self.proposal[side], scenario=self.scenario,
                               budget=self.a.budget, max_seconds=self.a.max_seconds,
                               cleanup_seconds=self.a.cleanup_seconds, reserve_die_seconds=self.a.max_seconds,
                               case=self.a.case, metric=self.a.metric, campaign=self.campaign,
                               round=self.round_number, profile_spec=None, run=None)

    def receipt(self, request):
        path = self.site.root / "deployments" / request.action / "action-receipt.json"
        if path.exists():
            receipt = host.read(path)
            lease = next((row for row in self.budget.audit()["leases"] if row["action_id"] == request.action), None)
            host.require(lease is not None and lease["outcome"] == "collected"
                         and all(receipt[key] == value for key, value in lease.items())
                         and receipt["scenario"] == request.scenario and receipt["configuration"] == request.config_key
                         and receipt["measurement"] is not None and receipt["measurement"]["run"] == str(request.run),
                         "receipt does not match this released lease, configuration and run")
        else:
            receipt = site_910c.measure(self.site, request)
        host.require(receipt["error"] is None and not receipt["action_overrun"],
                     "deployment failed or overran its reserved interval: " + str(path))
        return receipt["measurement"]

    def profile(self, side):
        request = self.request(self.label + "-profile-" + side, side)
        request.run = self.round_dir / "diagnostic" / side
        request.profile_spec = self.round_dir / f"profile-{side}.json"
        return request, profile_spec(self.site, request.profile_spec, request.run, side, self.requirements)

    def execute_plan(self, registered):
        plan = Path(registered["path"])
        host.require(host.sha(plan) == registered["sha256"], "registered plan changed")
        data, _ = trial_plan.load_plan(plan, contract_path=self.a.contract, pairs_path=plan.parent / "pairs.json")
        attempt = registered["sha256"][:10]
        for index, slot in enumerate(data["slots"], 1):
            request = self.request(f"{self.label}-{data['stage']}-{attempt}-{index:03d}", slot["side"])
            request.scenario = slot["scenario"]
            request.run = (plan.parent / slot["path"]).resolve()
            self.receipt(request)
        self.control("assess", "--evidence", plan.parent / "pairs.json")

    def close(self, action, reason=None):
        decision, side = {
            "accept": ("adopt", "candidate"),
            "restore": ("revert", "baseline"),
            "close_at_baseline": ("abandon", "baseline"),
        }[action]
        request = self.request(self.label + "-closure", side)
        request.reserve_die_seconds = 0

        def promotion(live):
            observation = live.directory / "round-observation.json"
            health = live.directory / "round-health.json"
            self.control("observe", "--output", observation, "--", sys.executable,
                         "-B", ROOT / "scripts/site_910c.py", "observe", "--root", self.site.root,
                         "--site", self.a.site, "--template", self.site.template, "--action", request.action)
            self.control("health", "--side", side, "--deployment", observation, "--output", health)
            self.control("close", "--decision", decision, "--deployment", observation, "--health", health,
                         "--observation", reason or f"{action} after formal comparison; device lease ends after health")
            return {"decision": decision, "config": str(live.config), "health": str(health)}

        receipt = site_910c.with_device(self.site, request, promotion, cleanup=True)
        host.require(not receipt["error"], "closing did not verify the final deployment: " + str(receipt["error"]))
        exported = self.control("export")
        retain(self.round_dir / "round-export.json", exported)
        if self.a.experience_db:
            site_910c.invoke(ROOT / "scripts/experience.py", "add", "--db", self.a.experience_db,
                             "--campaign", self.campaign, "--round", self.round_number)
        return {"round": self.round_number, "decision": decision, "export": str(self.round_dir / "round-export.json"),
                "budget": self.budget.status(), "deployment_release": receipt["proof_path"]}

    def drive(self):
        ledger = self.budget.audit()
        host.require(not ledger["pending"], "reconcile pending device action: " + ", ".join(ledger["pending"]))
        self.setup()
        state = call(self.campaign, "status")
        policy = state.get("device_budget")
        host.require(policy and Path(policy["path"]).resolve() == self.budget.path,
                     "site worker must use the campaign's registered device budget")
        if state["next_action"] in ("stop", "stopped"):
            if state["next_action"] == "stop":
                state = call(self.campaign, "stop")
            previous = state["round"] - 1
            if previous > 0:
                archived = self.campaign / f"round-{previous:03d}" / "round-export.json"
                retain(archived, call(self.campaign, "export", round_number=previous))
                state["export"] = str(archived)
            return state
        self.round_number = state["round"]
        self.label = f"round-{self.round_number:03d}"
        self.round_dir = self.campaign / self.label
        self.round_dir.mkdir(exist_ok=True)
        self.calibration = self.round_dir / "calibration.json"
        if not self.calibration.exists():
            retain(self.calibration, trial_plan.calibrate(self.a.contract, self.a.baseline_run))
        else:
            trial_plan.validate_calibration(self.a.contract, self.calibration)
        retain(self.round_dir / "proposal.frozen.json", {
            "proposal": self.proposal, "change_sha256": round_evidence.file_hash(self.change),
            "site_sha256": round_evidence.file_hash(self.a.site),
            "case_sha256": round_evidence.file_hash(self.a.case),
            "contract_sha256": round_evidence.file_hash(self.a.contract),
            "metric_sha256": round_evidence.file_hash(self.a.metric),
            "configuration_hashes": {side: round_evidence.file_hash(self.site.config(self.proposal[side]))
                                     for side in ("baseline", "candidate")},
        })
        expected = round_evidence.validate_run.canonical_hash(host.read(self.site.config(self.proposal["baseline"])))
        host.require(state["baseline"]["config_sha256"] == expected, "proposal refers to a different promoted baseline")
        host.require(host.read(self.calibration)["baseline_identity"] == state["baseline"],
                     "supply calibration runs from the current promoted baseline")
        while True:
            action = state["next_action"]
            if action == "begin":
                _, spec = self.profile("baseline")
                policy = self.round_dir / "profile-policy.json"
                retain(policy, profile_policy(spec))
                self.control("begin", "--scenario", self.scenario, "--profile-policy", policy)
            elif action in ("baseline_profile", "candidate_profile"):
                req, _ = self.profile(action.split("_")[0])
                measured = self.receipt(req)
                self.control("profile", "--bundle", measured["profile_bundle"])
            elif action == "plan":
                self.control("plan", "--hypothesis", self.proposal["hypothesis"],
                             "--prediction", self.proposal["prediction"], "--change", self.change,
                             "--config", self.site.config(self.proposal["candidate"]))
            elif action in ("schedule", "schedule_confirmation"):
                stage = "confirm" if action == "schedule_confirmation" else "screen"
                directory = self.round_dir / stage
                if not (directory / "plan.json").exists():
                    trial_plan.make_plan(self.a.contract, directory, stage, calibration=self.calibration)
                slots = host.read(directory / "plan.json")["slots"]
                ledger = self.budget.status()
                spent = [row["die_seconds"] for row in ledger["leases"] if row["outcome"] == "collected"]
                # Reserve enough for one action at its full cap, then the
                # largest observed successful lease per remaining slot.
                need = 2 * self.a.max_seconds + max(spent, default=self.a.max_seconds) * (len(slots) - 1)
                if ledger["remaining_die_seconds"] < need:
                    return self.close("close_at_baseline", f"budget insufficient for {stage} and closure ({need:.1f} die-s)")
                self.control("schedule", "--evidence", directory / "pairs.json", "--plan", directory / "plan.json")
            elif action in ("assess", "confirm"):
                try:
                    self.execute_plan(state["active"]["trial_plan"])
                except InsufficientDeviceBudget as error:
                    return self.close("close_at_baseline", str(error))
            elif action in ("accept", "restore", "close_at_baseline"):
                return self.close(action, state.get("stop_reason"))
            else:
                raise ValueError("unexpected controller action " + action)
            state = call(self.campaign, "status")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("root", "site", "case", "metric", "contract", "campaign", "budget", "proposal"):
        parser.add_argument("--" + option, type=lambda path: Path(path).resolve(), required=True)
    parser.add_argument("--baseline-run", type=lambda path: Path(path).resolve(), action="append", required=True)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--experience-db", type=Path)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--goal-metric", default="goodput", choices=("goodput", "output_token_throughput"))
    parser.add_argument("--goal-tps", type=float)
    parser.add_argument("--max-rounds", type=int, default=1)
    parser.add_argument("--max-seconds", type=float, default=210)
    parser.add_argument("--cleanup-seconds", type=float, default=40)
    args = parser.parse_args()
    try:
        report = Round(args).drive()
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

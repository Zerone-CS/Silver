#!/usr/bin/env python3
"""Calibrate A/A variation and verify the execution of a frozen A/B plan."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluate.scripts import compare_trials, validate_run
from scripts import round_evidence

SIDES = ("baseline", "candidate")
RAW_THROUGHPUT = "output_token_throughput"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_bytes())


def write(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reference(path, base):
    path = Path(path).resolve(strict=True)
    return {"path": os.path.relpath(path, base), "sha256": sha(path)}


def resolve_reference(ref, base):
    path = (base / ref["path"]).resolve(strict=True)
    require(sha(path) == ref["sha256"], "sealed trial input changed: " + str(path))
    return path


def metric_rules(spec):
    rules = [spec[key] for key in ("primary", "goodput_primary") if spec.get(key) is not None]
    return {rule["metric"]: rule["min_relative_improvement"] for rule in rules} or {RAW_THROUGHPUT: 0.10}


def clock_window(manifest):
    supplied = manifest.get("client_run_order")
    if supplied is not None:
        compare_trials.obj(supplied, "client_run_order")
        require(supplied["clock"] == "host_monotonic_ns", "unexpected cross-run clock")
        clock = "host_monotonic_ns"
        host = compare_trials.text(supplied["hostname"], "client_run_order.hostname")
        boot = compare_trials.text(supplied["boot_id"], "client_run_order.boot_id")
        start, end = supplied["start_ns"], supplied["end_ns"]
    else:
        platform = manifest["client_runtime"]["platform"]
        require(platform.startswith("Linux"), "planned trials need same-host Linux performance clocks "
                "or explicit host-monotonic window evidence")
        clock, host, boot = "linux_client_perf_counter", None, None
        start, end = (round(manifest[field] * 1e9) for field in ("start_ts", "end_ts"))
    require(type(start) is int and type(end) is int and 0 <= start < end,
            "invalid cross-run measurement interval")
    return {"clock": clock, "hostname": host, "boot_id": boot, "start_ns": start, "end_ns": end}


def require_common_clock(rows):
    clocks = {(row["order_window"]["clock"], row["order_window"]["hostname"],
               row["order_window"]["boot_id"]) for row in rows}
    require(len(clocks) == 1, "runs mix client clock domains")


def calibrate(contract_path, baseline_run_paths):
    """Use one benchmark run per independent A/A deployment and scenario."""
    contract_path = Path(contract_path).resolve(strict=True)
    contract = read(contract_path)
    baseline_run_paths = sorted(Path(path).resolve(strict=True) for path in baseline_run_paths)
    require(bool(baseline_run_paths), "A/A baseline runs required")
    round_evidence.validate_policy(contract_path)
    quality_rules = round_evidence.quality_rules(contract_path, contract, {})
    goodput_rules = round_evidence.goodput_rules(contract, quality_rules)
    rows = {scenario: [] for scenario in contract["scenarios"]}
    cases, sources = {}, {}
    seen_ids = set()
    baseline = None
    for path in baseline_run_paths:
        checked = validate_run.inspect_run(path)
        require(checked["valid"], "invalid A/A run " + str(path))
        manifest = checked["manifest"]
        scenario, run_id = manifest["scenario"], manifest["run_id"]
        require(scenario in rows and run_id not in seen_ids, "unexpected A/A scenario or reused run ID")
        seen_ids.add(run_id)
        require(manifest["run_purpose"] == "performance" and manifest["profiler_enabled"] is False,
                "A/A calibration requires unprofiled performance runs")
        identity = round_evidence.identity_of(manifest)
        require(baseline is None or baseline == identity, "A/A mixes baseline identities")
        baseline = identity
        case = round_evidence.case_of(manifest)
        require(cases.setdefault(scenario, case) == case, f"A/A case drift in {scenario}")
        deployment = round_evidence.verify_deployment(path, manifest, identity, sources)
        verdict = round_evidence.score_run(path, checked, quality_rules[scenario], sources)
        require(verdict["passed"], "A/A quality failed for " + str(path))
        values = checked["metrics"]
        require(values["n_success"] > 0, "A/A calibration requires successful requests")
        for guard in contract["scenarios"][scenario]["guardrails"]:
            if guard["metric"] == compare_trials.GOODPUT_METRIC:
                continue
            observed = values[guard["metric"]]
            require(observed is not None
                    and guard.get("min_value", -float("inf")) <= observed <= guard.get("max_value", float("inf")),
                    "A/A violates absolute guard " + guard["metric"])
        if scenario in goodput_rules:
            values[compare_trials.GOODPUT_METRIC] = round_evidence.measure_goodput(
                path, checked, goodput_rules[scenario], verdict)["output_tokens_per_s"]
            for guard in contract["scenarios"][scenario]["guardrails"]:
                if guard["metric"] == compare_trials.GOODPUT_METRIC:
                    observed = values[guard["metric"]]
                    require(guard.get("min_value", -float("inf")) <= observed <= guard.get("max_value", float("inf")),
                            "A/A violates an absolute goodput guard")
        rows[scenario].append({"path": str(path), "run_id": run_id,
                               "instance_id": deployment["instance_id"], "start_ts": manifest["start_ts"],
                               "end_ts": manifest["end_ts"], "source_hashes": checked["source_hashes"],
                               "order_window": clock_window(manifest),
                               "metrics": {metric: values[metric] for metric in metric_rules(contract["scenarios"][scenario])}})
    characterization = {}
    tiers = []
    for scenario, samples in rows.items():
        require(len(samples) >= 2, "A/A needs two independent deployments in " + scenario)
        require(len({r["instance_id"] for r in samples}) == len(samples),
                "A/A deployment instance reused in " + scenario)
        require_common_clock(samples)
        ordered = sorted((sample["order_window"] for sample in samples), key=lambda window: window["start_ns"])
        require(all(l["end_ns"] <= r["start_ns"] for l, r in zip(ordered, ordered[1:])),
                "A/A runs overlap on the measurement clock in " + scenario)
        measures = {}
        for metric, economic_effect in metric_rules(contract["scenarios"][scenario]).items():
            values = [sample["metrics"][metric] for sample in samples]
            center = statistics.median(values)
            require(center > 0, f"A/A median is nonpositive for relative metric {metric} in {scenario}")
            spread = (max(values) - min(values)) / center
            relevance = max(economic_effect, 0.01)
            tiers.append(int(spread > relevance) + int(spread > 2 * relevance))
            measures[metric] = {"median": center, "relative_range": spread,
                                "min_effect_for_planning": relevance, "samples": values}
        characterization[scenario] = measures
    floor = max(4, contract["min_pairs"])
    floor += floor % 2
    return {"schema_version": 1, "contract_sha256": sha(contract_path), "baseline_identity": baseline,
            "case_identity": cases, "runs": rows, "sources": sources, "variability": characterization,
            "recommended_confirm_blocks": floor + 2 * max(tiers),
            "planning_rule": "At least four balanced blocks, plus two per observed noise tier. "
                             "Tiers compare A/A relative range with one and two times the effect target. "
                             "This heuristic is not a power calculation."}


def validate_calibration(contract_path, calibration_path):
    report = read(calibration_path)
    paths = [sample["path"] for rows in report["runs"].values() for sample in rows]
    fresh = calibrate(contract_path, paths)
    # The first Linux reports predate the explicit order-window summary.
    # Recompute and return it even when the input preserved the older shape.
    for name, samples in report["runs"].items():
        for sample, rebuilt in zip(samples, fresh["runs"].get(name, [])):
            sample.setdefault("order_window", rebuilt["order_window"])
    require(fresh == report, "A/A calibration differs from its originals")
    return fresh


def layout(contract, stage, blocks, seed):
    rng = random.Random(seed)
    slots, pairs = [], []
    for index, scenario in enumerate(sorted(contract["scenarios"]), 1):
        orders = ["AB"] * ((blocks + 1) // 2) + ["BA"] * (blocks // 2)
        rng.shuffle(orders)
        for block, order in enumerate(orders, 1):
            paths = {side: f"runs/scenario-{index:02d}/block-{block:03d}/{side}" for side in SIDES}
            pairs.append({"scenario": scenario, **paths})
            sides = SIDES if order == "AB" else tuple(reversed(SIDES))
            slots.extend({"scenario": scenario, "block": block, "side": side,
                          "path": paths[side], "order": order} for side in sides)
    return slots, {"schema_version": 1, "pairs": pairs}


def check_blocks(contract, stage, count, calibration):
    require(stage in ("screen", "confirm"), "unknown trial stage")
    if stage == "confirm":
        require(calibration is not None, "independent confirmation needs frozen A/A calibration")
    minimum = (calibration["recommended_confirm_blocks"] if stage == "confirm"
               else max(2, contract["min_pairs"]))
    blocks = minimum if count is None else count
    require(type(blocks) is int and minimum <= blocks <= 10000, f"{stage} needs at least {minimum} blocks")
    require(stage != "confirm" or blocks % 2 == 0, "confirmation needs even AB/BA block counts")
    return blocks


def make_plan(contract_path, directory, stage, *, seed=None, blocks=None, calibration=None,
              future_confirm_blocks=None):
    contract_path = Path(contract_path).resolve(strict=True)
    round_evidence.validate_policy(contract_path)
    contract = read(contract_path)
    calibration = Path(calibration).resolve(strict=True) if calibration is not None else None
    aa = validate_calibration(contract_path, calibration) if calibration is not None else None
    blocks = check_blocks(contract, stage, blocks, aa)
    require(future_confirm_blocks is None or (stage == "screen" and aa is not None),
            "a future confirmation block count belongs in an A/A-calibrated screen plan")
    confirmed_count = (check_blocks(contract, "confirm", future_confirm_blocks, aa)
                       if stage == "screen" and aa is not None else None)
    seed = contract["seed"] if seed is None else seed
    require(type(seed) is int, "trial seed must be an integer")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    pairs_file, plan_file = directory / "pairs.json", directory / "plan.json"
    require(not pairs_file.exists() and not pairs_file.is_symlink()
            and not plan_file.exists() and not plan_file.is_symlink(), "trial output paths already exist")
    slots, pairs = layout(contract, stage, blocks, seed)
    compare_trials.validate_trial(pairs, contract)
    write(pairs_file, pairs)
    plan = {"schema_version": 1, "stage": stage, "seed": seed, "blocks_per_scenario": blocks,
            "contract": reference(contract_path, directory), "pairs": reference(pairs_file, directory),
            "calibration": reference(calibration, directory) if calibration is not None else None,
            "future_confirm_blocks": confirmed_count, "slots": slots}
    write(plan_file, plan)
    return plan


def load_plan(plan_path, *, contract_path=None, pairs_path=None, stage=None):
    plan_path = Path(plan_path).resolve(strict=True)
    plan = read(plan_path)
    compare_trials.version(plan, "trial plan")
    directory = plan_path.parent
    actual_contract = resolve_reference(plan["contract"], directory)
    actual_pairs = resolve_reference(plan["pairs"], directory)
    require(contract_path is None or actual_contract == Path(contract_path).resolve(strict=True),
            "trial plan is for another comparison contract")
    require(pairs_path is None or actual_pairs == Path(pairs_path).resolve(strict=True),
            "trial plan is for another pair list")
    require(stage is None or plan["stage"] == stage, "trial plan is for another stage")
    require(actual_pairs.parent == directory, "planned pair list must be next to the plan")
    contract = read(actual_contract)
    aa_source = resolve_reference(plan["calibration"], directory) if plan["calibration"] else None
    aa = validate_calibration(actual_contract, aa_source) if aa_source is not None else None
    blocks = check_blocks(contract, plan["stage"], plan["blocks_per_scenario"], aa)
    future = plan.get("future_confirm_blocks")
    require(future is None or (plan["stage"] == "screen" and aa is not None),
            "a future confirmation count requires an A/A-calibrated screen plan")
    if plan["stage"] == "screen" and aa is not None:
        plan["future_confirm_blocks"] = check_blocks(contract, "confirm", future, aa)
    require(type(plan["seed"]) is int, "trial seed must be an integer")
    slots, pairs = layout(contract, plan["stage"], blocks, plan["seed"])
    require(slots == plan["slots"] and pairs == read(actual_pairs), "slot order or pair list differs from the seed")
    compare_trials.validate_trial(pairs, contract)
    return plan, aa


def verify_execution(plan_path, assessment):
    plan_path = Path(plan_path).resolve(strict=True)
    plan, aa = load_plan(plan_path)
    pairing_hash = assessment["fingerprints"]["evidence_sha256"]
    require(pairing_hash == plan["pairs"]["sha256"]
            and assessment["fingerprints"]["contract_sha256"] == plan["contract"]["sha256"],
            "assessment provenance differs from plan")
    bases = [run for run in assessment["runs"] if run["side"] == "baseline"]
    if aa is not None:
        require(assessment["case_identity"] == aa["case_identity"]
                and all(run["identity"] == aa["baseline_identity"] for run in bases),
                "assessment differs from the A/A calibrated baseline")
    inputs = {row["manifest"]["run_id"]: row["manifest"]
              for row in assessment["comparison"]["validated_inputs"]}
    rows = {str(Path(row["path"]).resolve(strict=True)): row for row in assessment["runs"]}
    checks = []
    for slot in plan["slots"]:
        path = str((plan_path.parent / slot["path"]).resolve(strict=True))
        require(path in rows, "planned slot absent in assessment: " + path)
        run = rows.pop(path)
        require((run["scenario"], run["side"]) == (slot["scenario"], slot["side"]),
                "planned scenario or side differs from assessment")
        manifest = inputs[run["run_id"]]
        source_manifest = Path(path) / "manifest.json"
        require(sha(source_manifest) == run["source_hashes"]["manifest.json"],
                "benchmark manifest changed for " + path)
        original = read(source_manifest)
        require(all(original[k] == manifest[k] for k in ("run_id", "start_ts", "end_ts")),
                "assessment clock or run ID differs from original benchmark")
        observed = round_evidence.verify_deployment(Path(path), original, run["identity"], {})
        require(observed["instance_id"] == run["instance_id"],
                "assessment deployment ID differs from original deployment")
        checks.append({**slot, "run_id": run["run_id"], "instance_id": run["instance_id"],
                       "start_ts": manifest["start_ts"], "end_ts": manifest["end_ts"],
                       "order_window": clock_window(original)})
    require(not rows, "unplanned benchmark runs in assessment")
    require_common_clock(checks)
    require(all(l["order_window"]["end_ns"] <= r["order_window"]["start_ns"]
                for l, r in zip(checks, checks[1:])),
            "measurement runs overlapped or did not follow the frozen order")
    calibration_runs = [] if aa is None else [row for group in aa["runs"].values() for row in group]
    prior_instances = {row["instance_id"] for row in calibration_runs}
    if calibration_runs:
        require_common_clock([*calibration_runs, *checks])
        require(max(row["order_window"]["end_ns"] for row in calibration_runs) <= checks[0]["order_window"]["start_ns"],
                "A/A must finish before the first planned measurement")
    if plan["stage"] == "confirm":
        instances = [slot["instance_id"] for slot in checks]
        require(len(set(instances)) == len(instances) and not set(instances) & prior_instances,
                "confirmation reuses a deployment instance")
    orders = {name: dict(Counter(s["order"] for s in checks if s["scenario"] == name and s["side"] == "baseline"))
              for name in sorted({s["scenario"] for s in checks})}
    return {"schema_version": 1, "plan_sha256": sha(plan_path), "stage": plan["stage"],
            "calibration_sha256": plan["calibration"]["sha256"] if aa else None,
            "future_confirm_blocks": plan.get("future_confirm_blocks"),
            "orders": orders, "slots": checks,
            "independent_deployments": len({slot["instance_id"] for slot in checks}) == len(checks)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    commands = {name: subparsers.add_parser(name) for name in ("calibrate", "plan", "verify")}
    for name in ("calibrate", "plan"):
        cmd = commands[name]
        cmd.add_argument("--contract", required=True, type=Path)
        cmd.add_argument("--out", required=True, type=Path)
    commands["calibrate"].add_argument("--baseline-run", action="append", required=True, type=Path)
    cmd = commands["plan"]
    cmd.add_argument("--stage", required=True, choices=("screen", "confirm"))
    cmd.add_argument("--calibration", type=Path)
    cmd.add_argument("--blocks", type=int)
    cmd.add_argument("--future-confirm-blocks", type=int)
    cmd.add_argument("--seed", type=int)
    cmd = commands["verify"]
    cmd.add_argument("--plan", required=True, type=Path)
    cmd.add_argument("--assessment", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "calibrate":
            result = calibrate(args.contract, args.baseline_run)
            write(args.out, result)
        elif args.command == "plan":
            result = make_plan(args.contract, args.out, args.stage,
                               seed=args.seed, blocks=args.blocks, calibration=args.calibration,
                               future_confirm_blocks=args.future_confirm_blocks)
        else:
            result = verify_execution(args.plan, read(args.assessment))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

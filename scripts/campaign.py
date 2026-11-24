#!/usr/bin/env python3
"""Run one evidence-checked transition of a persistent throughput campaign."""
import argparse
import csv
import hashlib
import http.client
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluate.scripts import benchmark, compare_trials, validate_run
from evaluate.scripts.prepare_metadata import load_object
from scripts import device_budget, retry_evidence, trial_plan

COMMAND_ERRORS = (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, csv.Error,
                  sqlite3.Error, subprocess.SubprocessError, http.client.HTTPException)
RAW_THROUGHPUT = "output_token_throughput"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def fingerprint(path):
    path = Path(path).resolve(strict=True)
    require(path.is_file(), "expected a file: " + str(path))
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "sha256": digest.hexdigest()}


def verify_files(refs):
    for ref in refs:
        require(fingerprint(ref["path"]) == ref, "evidence changed: " + ref["path"])


def diagnosis_options(a):
    return {name: str(path.resolve(strict=True)) for name in ("diagnostic_run", "step_sources", "timeline_map")
            if (path := getattr(a, name)) is not None}


def publish_diagnosis(campaign, report):
    from scripts.step_analysis import emit
    contents = encode(report) + "\n"
    digest = hashlib.sha256(contents.encode("utf-8")).hexdigest()
    directory = campaign.resolve() / "diagnosis"
    directory.mkdir(exist_ok=True)
    output = directory / (digest + ".json")
    ref = {"path": str(output), "sha256": digest}
    try:
        emit(contents, output)
    except FileExistsError:
        verify_files([ref])
    return ref


def read_step_analysis(ref):
    report, actual = load_object(ref["path"])
    require(actual == ref, "step analysis report changed: " + ref["path"])
    refs = report["sources"] + report["analysis_inputs"] + report["benchmark_sources"]
    if report["timeline"]:
        refs.extend(report["timeline"]["sources"])
    # Large traces may occur in more than one reference list.
    seen = set()
    for ref in refs:
        key = ref["path"], ref["sha256"]
        if key not in seen:
            verify_files([ref])
            seen.add(key)
    return report


def review_step_analyses(evidence):
    for recorded in evidence["profiles"].values():
        if "step_analysis" in recorded:
            read_step_analysis(recorded["step_analysis"])
    if "step_comparison" in evidence:
        verify_files([evidence["step_comparison"]])


def identity(manifest):
    return {key: manifest.get(key) for key in ("config_sha256", "implementation_sha256")}


def timed_out(state):
    return state["deadline"] is not None and time.time() >= state["deadline"]


def device_status(state):
    if "device_budget" not in state:
        return None
    policy = state["device_budget"]
    budget = device_budget.Budget(policy["path"]).status()
    budget.pop("leases")
    return {**policy, **budget}


def stop_reason(state, budget):
    if state["stop_requested"]:
        return state["stop_requested"]
    if timed_out(state):
        return "deadline"
    if budget and not budget["pending"] and (
            budget["remaining_die_seconds"] == 0
            or budget["remaining_die_seconds"] < budget["minimum_next_die_seconds"]):
        return "device_budget"
    return None


def view(state, row):
    phase = row["phase"] if row else "begin"
    action = phase
    budget = device_status(state)
    reason = state["stopped"] or stop_reason(state, budget)
    if state["stopped"]:
        action = "stopped"
    elif reason:
        action = "close_at_baseline" if row else "stop"
    active = None
    if row:
        active = {k: row.get(k) for k in ("scenario", "baseline", "candidate", "hypothesis", "prediction", "change", "schedule")}
        active["profiles"] = row["evidence"]["profiles"]
        if "step_comparison" in row["evidence"]:
            active["step_comparison"] = row["evidence"]["step_comparison"]
        if row.get("trial_plan"):
            active["trial_plan"] = row["trial_plan"]
        active["assessment_outcomes"] = [r["recommendation"] for r in row["evidence"]["assessments"]]
    result = {"campaign_id": state["campaign_id"], "phase": phase, "active": active, "next_action": action,
              "round": row["round"] if row else state["count"] + 1,
              "max_rounds": state["max_rounds"], "deadline": state["deadline"],
              "baseline": state["baseline"], "stop_reason": reason,
              "deployment_last_verified": state["deployed"], "active_round": bool(row)}
    if budget is not None:
        result["device_budget"] = budget
    if "require_site_release" in state:
        result["require_site_release"] = state["require_site_release"]
    if "site_release" in state:
        result["site_release"] = state["site_release"]
        if state["site_release"]["status"] == "pending":
            result.update(next_action="attach_release", phase="awaiting_site_release",
                          round=state["site_release"]["round"])
    result["max_retries"] = state.get("max_retries", 1)
    if state.get("goal_tps") is not None:
        result["goal"] = {"scenario": state["goal_scenario"], "metric": state.get("goal_metric", RAW_THROUGHPUT),
                          "target": state["goal_tps"]}
    return result


def event(db, n, action, details):
    db.execute("INSERT INTO events(at,round,action,data) VALUES(?,?,?,?)",
               (time.time(), n, action, encode(details)))


def initial_goal(runs, contract, scenario, target, metric=RAW_THROUGHPUT, distinct_deployments=False):
    if target is None:
        return False
    for name, spec in contract["scenarios"].items():
        examples = [r for r in runs if r["scenario"] == name]
        if distinct_deployments and len({r["instance_id"] for r in examples}) < max(2, contract["min_pairs"]):
            return False
        metrics = [r["metrics"] for r in examples]
        if len(metrics) < contract["min_pairs"]:
            return False
        for sample in metrics:
            if name == scenario and sample[metric] < target:
                return False
            for primary in (spec.get("primary"), spec.get("goodput_primary")):
                if primary and "min_value" in primary and sample[primary["metric"]] < primary["min_value"]:
                    return False
            for guard in spec["guardrails"]:
                value = sample.get(guard["metric"])
                if value is None or value < guard.get("min_value", -math.inf) or value > guard.get("max_value", math.inf):
                    return False
    return True


def init(db, a):
    from scripts.round_evidence import goodput_metrics, goodput_rules, measure_goodput, quality_rules, score_run, validate_policy, verify_deployment
    require(a.max_rounds > 0 and (not a.single or a.max_rounds == 1),
            "max-rounds must be positive; --single requires exactly one round")
    require(a.max_retries >= 0, "max-retries cannot be negative")
    require(not a.require_site_release or a.device_budget is not None,
            "--require-site-release needs a registered --device-budget")
    if a.hours is not None:
        require(math.isfinite(a.hours) and 0 < a.hours < 1e6, "invalid hour budget")
    require((a.goal_tps is None) == (a.goal_scenario is None),
            "--goal-tps and --goal-scenario must be supplied together")
    require(a.goal_tps is not None or a.goal_metric is None, "--goal-metric requires an explicit target")
    goal_metric = a.goal_metric or RAW_THROUGHPUT
    require(not (a.single and a.require_trial_plan), "planned trials require a separate confirmation stage")
    contract, contract_ref = load_object(a.contract)
    contract_sources = validate_policy(a.contract)
    rules = quality_rules(a.contract.resolve(), contract, {})
    goodput_by_scenario = goodput_rules(contract, rules)
    cases, runs, endpoints = {}, [], {}
    baseline = None
    model_snapshots = set()
    for path in a.baseline_run:
        path = path.resolve(strict=True)
        checked = validate_run.inspect_run(path)
        m = checked["manifest"]
        require(checked["valid"] and m.get("run_purpose") == "performance"
                and m.get("profiler_enabled") is False and m.get("effective_config_verified") is True,
                "initial run must be valid, unprofiled and config verified")
        observed = identity(m)
        require(baseline is None or baseline == observed, "initial runs mix deployments")
        external = {}
        deploy = verify_deployment(path, m, observed, external)
        model_snapshots.add(deploy.get("model_snapshot_sha256"))
        name = m["scenario"]
        require(name in rules, "baseline scenario is absent from contract")
        spec = contract["scenarios"][name]
        stream_facts = {}
        quality = score_run(path, checked, rules[name], external, stream_facts=stream_facts)
        require(quality["passed"] and checked["metrics"]["n_success"] > 0, "initial baseline failed quality")
        if name in goodput_by_scenario:
            goodput = measure_goodput(path, checked, goodput_by_scenario[name], quality, stream_facts=stream_facts)
            checked["metrics"].update(goodput_metrics(goodput))
        metrics = [r["metric"] for r in spec["guardrails"]]
        if spec["primary"] is not None:
            metrics.append(spec["primary"]["metric"])
        if "goodput_primary" in spec:
            metrics.append(spec["goodput_primary"]["metric"])
        if a.goal_tps is not None and name == a.goal_scenario:
            metrics.append(goal_metric)
        require(all(checked["metrics"].get(key) is not None for key in metrics), "metric absent from baseline: " + name)
        baseline = observed
        case = {key: m[key] for key in (*compare_trials.IDENTITIES, "measurement_mode")}
        endpoint = {"base_url": m["base_url"], "model": m["client_measurement_policy"]["model"]}
        require(cases.setdefault(name, case) == case and endpoints.setdefault(name, endpoint) == endpoint,
                "case or endpoint drift between initial runs")
        runs.append({"run_id": m["run_id"], "requests_hash": checked["source_hashes"]["requests.jsonl"],
                     "instance_id": deploy["instance_id"], "scenario": name, "quality": quality,
                     "metrics": checked["metrics"], "sources": [dict(path=k, sha256=v) for k,v in external.items()]
                     + [dict(path=str((path / k).resolve()), sha256=v) for k,v in checked["source_hashes"].items()]})
    require(set(cases) == set(contract["scenarios"]), "initial runs must cover all contract scenarios")
    require(len({encode(v) for v in endpoints.values()}) == 1, "campaign needs one serving endpoint/model")
    if a.goal_tps is not None:
        require(math.isfinite(a.goal_tps) and a.goal_tps > 0 and a.goal_scenario in cases,
                "invalid throughput goal/scenario")
        require(goal_metric != compare_trials.GOODPUT_METRIC or a.goal_scenario in goodput_by_scenario,
                "a goodput goal requires request-level SLOs in the goal scenario")
    require(a.scope.strip(), "empty scope")
    state = {"schema_version": 1, "campaign_id": str(uuid.uuid4()), "scope": a.scope,
             "contract": contract_ref, "contract_sources": contract_sources, "baseline": baseline, "cases": cases,
             "endpoint": next(iter(endpoints.values())), "initial_runs": runs,
             "count": 0, "max_retries": a.max_retries, "max_rounds": a.max_rounds, "single": a.single,
             "goal_tps": a.goal_tps, "goal_scenario": a.goal_scenario, "goal_metric": goal_metric,
             "require_trial_plan": a.require_trial_plan, "require_confirmation_clock": a.require_trial_plan,
             "require_site_release": a.require_site_release,
             "deadline": time.time() + a.hours * 3600 if a.hours is not None else None,
             "deployed": None, "stop_requested": None, "stopped": None}
    require(len(model_snapshots) == 1, "initial deployments disagree on model snapshot")
    state["model_snapshot_sha256"] = model_snapshots.pop()
    state["deployed"] = {"identity": baseline, "instance_id": deploy["instance_id"],
                         "source": "last supplied calibration run", "run_id": runs[-1]["run_id"]}
    if a.device_budget is not None:
        budget = device_budget.Budget(a.device_budget)
        meter = budget.audit()
        require(not meter["pending"], "reconcile pending device claim before initializing a campaign")
        device_budget.number(a.minimum_device_seconds, "minimum die-seconds", allow_zero=True)
        require(a.minimum_device_seconds <= meter["limit_die_seconds"], "minimum lease exceeds device budget")
        state["device_budget"] = {"path": str(budget.path), "minimum_next_die_seconds": a.minimum_device_seconds}
    else:
        require(not a.minimum_device_seconds, "minimum device expenditure requires a device budget")
    if initial_goal(runs, contract, a.goal_scenario, a.goal_tps, goal_metric, a.require_trial_plan):
        state["stopped"] = "initial_goal_reached"
    db.execute("CREATE TABLE reserved_paths (path TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE used_traces (sha256 TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE campaign (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL)")
    db.execute("CREATE TABLE rounds (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
    db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, at REAL, round INTEGER, action TEXT, data TEXT)")
    db.execute("CREATE TABLE used_runs (run_id TEXT PRIMARY KEY, requests_hash TEXT UNIQUE NOT NULL)")
    for run in runs:
        db.execute("INSERT INTO used_runs VALUES(?,?)", (run["run_id"], run["requests_hash"]))
    db.execute("INSERT INTO campaign VALUES(1,?)", (encode(state),))
    event(db, 0, "init", {"contract": contract_ref, "cases": cases, "require_site_release": a.require_site_release})
    return view(state, None)


def begin(state, a):
    require(a.round == state["count"] + 1 <= state["max_rounds"], "unexpected round or exhausted budget")
    require(a.scenario in state["cases"], "unknown diagnostic scenario")
    policy, ref = load_object(a.profile_policy)
    require(policy.get("requirements") and policy.get("ranks"), "supply output of profile.py policy")
    state["count"] = a.round
    return {"schema_version": 1, "campaign_id": state["campaign_id"], "scope": state["scope"],
            "round": a.round, "scenario": a.scenario, "baseline": state["baseline"], "candidate": None,
            "acceptance_protocol": {"contract": state["contract"], "cases": state["cases"],
                                    "endpoint": state["endpoint"], "single": state["single"],
                                    "require_retry_snapshot": True,
                                    "model_snapshot_sha256": state.get("model_snapshot_sha256"),
                                    "require_trial_plan": state.get("require_trial_plan", False),
                                    "require_site_release": state.get("require_site_release", False),
                                    "require_confirmation_clock": state.get("require_confirmation_clock", False)},
            "phase": "baseline_profile", "profile_policy": policy, "hypothesis": "",
            "evidence": {"evidence_files": [state["contract"], ref] + state["contract_sources"], "profiles": {}, "assessments": []}}


def profile(db, state, row, a):
    from scripts.round_evidence import verify_deployment
    from diagnose.scripts.profile import verify_bundle
    side = {"baseline_profile": "baseline", "candidate_profile": "candidate"}.get(row["phase"])
    require(side is not None, "a profile is not due at this phase")
    report = verify_bundle(a.bundle)
    require(report["valid"], "invalid profile: " + str(report["errors"]))
    require(report["run_binding_status"] in ("trace_field_verified", "fresh_command_outputs"),
            "profile import requires a shared trace field, or capture fresh outputs with a command")
    capture, _ = load_object(a.bundle / "manifest.json")
    for name, digest in capture["files"].items():
        if name.startswith("raw/rank-"):
            db.execute("INSERT INTO used_traces VALUES(?)", (digest,))
    require(identity(report) == row[side] and report["scenario"] == row["scenario"],
            "profile deployment or scenario differs from the active round")
    for key, val in state["cases"][row["scenario"]].items():
        if key != "measurement_mode":
            require(report.get(key) == val, "profile case mismatch: " + key)
    require(report["profile_policy"] == row["profile_policy"], "profile coverage policy changed")
    original = a.bundle / "diagnostic-run"
    manifest, _ = load_object(original / "manifest.json")
    metadata, _ = load_object(original / "metadata.json")
    require(metadata.get("campaign_binding") == row["challenge"], "diagnostic run belongs to another round or phase")
    require(manifest["base_url"] == state["endpoint"]["base_url"], "diagnostic endpoint changed")
    deployment_files = {}
    verify_deployment(original, manifest, row[side], deployment_files)
    row["evidence"]["evidence_files"].extend(dict(path=k, sha256=v) for k,v in deployment_files.items())
    db.execute("INSERT INTO used_runs VALUES(?,?)",
               (report["run_id"], report["diagnostic_source_hashes"]["requests.jsonl"]))
    profiles = row["evidence"]["profiles"]
    require(not any(p["run_id"] == report["run_id"] for p in profiles.values()), "duplicate profile run")
    profiles[side] = {"path": str(a.bundle.resolve(strict=True)), "run_id": report["run_id"],
                      "binding": row["challenge"],
                      "manifest_sha256": report["manifest_sha256"], "spec_sha256": report["spec_sha256"]}
    options = diagnosis_options(a)
    if options:
        from scripts import step_analysis
        analysis = step_analysis.analyze(a.diagnostic_run, sources=a.step_sources, bundle=a.bundle,
                                         timeline_map=a.timeline_map, verified_bundle=report)
        profiles[side]["step_analysis"] = publish_diagnosis(a.campaign, analysis)
        profiles[side]["step_analysis_options"] = options
        if side == "candidate" and "step_analysis" in profiles["baseline"]:
            baseline = read_step_analysis(profiles["baseline"]["step_analysis"])
            comparison = step_analysis.compare(baseline, analysis)
            row["evidence"]["step_comparison"] = publish_diagnosis(a.campaign, comparison)
    row["phase"] = "plan" if side == "baseline" else "schedule"


def plan(row, a):
    require(row["phase"] == "plan", "baseline profile must be verified before planning")
    require(a.hypothesis.strip() and a.prediction.strip(), "empty hypothesis or prediction")
    cfg, cfg_ref = load_object(a.config)
    impl = None
    change = fingerprint(a.change)
    refs = [cfg_ref, change]
    require(Path(a.change).stat().st_size, "empty change or recovery instructions")
    if a.implementation:
        obj, ref = load_object(a.implementation)
        impl = validate_run.canonical_hash(obj)
        refs.append(ref)
    candidate = {"config_sha256": validate_run.canonical_hash(cfg), "implementation_sha256": impl}
    require(candidate != row["baseline"], "candidate must differ from baseline")
    if impl != row["baseline"]["implementation_sha256"]:
        require(impl and row["baseline"]["implementation_sha256"]
                and candidate["config_sha256"] == row["baseline"]["config_sha256"]
                and impl != row["baseline"]["implementation_sha256"],
                "implementation comparison needs two distinct implementations and a fixed config")
    if a.git_repo:
        repo = a.git_repo.resolve(strict=True)
        cmd = ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options"]
        rev = a.git_rev or "HEAD"
        oid = subprocess.check_output(cmd + [rev + "^{commit}"], text=True).strip()
        tree = subprocess.check_output(cmd + [oid + "^{tree}"], text=True).strip()
        row["git"] = {"repository": str(repo), "commit": oid, "tree": tree}
    else:
        require(a.git_rev is None, "--git-rev requires --git-repo")
    row.update(candidate=candidate, hypothesis=a.hypothesis, prediction=a.prediction,
               change=change, phase="candidate_profile")
    row["evidence"]["evidence_files"].extend(refs)


def schedule(db, state, row, a):
    require(row["phase"] in ("schedule", "schedule_confirmation"), "no registration due at this phase")
    verify_files([state["contract"]])
    contract, _ = load_object(state["contract"]["path"])
    trial, ref = load_object(a.evidence)
    compare_trials.validate_trial(trial, contract)
    plans = getattr(a, "plan", None)
    require(plans is not None or not state.get("require_trial_plan"), "supply the frozen --plan")
    row.pop("trial_plan", None)
    if plans is not None:
        stage = "confirm" if row["phase"] == "schedule_confirmation" else "screen"
        document, calibration = trial_plan.load_plan(plans, contract_path=state["contract"]["path"],
                                                     pairs_path=a.evidence, stage=stage)
        require(calibration is not None or not state.get("require_trial_plan"), "freeze A/A before screening")
        if calibration is not None:
            require(calibration["case_identity"] == state["cases"]
                    and calibration["baseline_identity"] == row["baseline"], "A/A differs from campaign case or baseline")
        if stage == "confirm":
            discovery = row["evidence"]["assessments"][0].get("execution")
            if discovery and discovery["calibration_sha256"]:
                require(document["calibration"]["sha256"] == discovery["calibration_sha256"],
                        "confirmation must use the A/A frozen before screening")
                require(document["blocks_per_scenario"] == discovery["future_confirm_blocks"],
                        "confirmation must match the repetition count frozen before screening")
        row["trial_plan"] = fingerprint(plans)
    root = a.evidence.resolve().parent
    paths = []
    for pair in trial["pairs"]:
        for side in ("baseline", "candidate"):
            path = root / pair[side]
            require(not path.exists() and not path.is_symlink(), "scheduled run must use a fresh path: " + str(path))
            paths.append(str(path.resolve()))
    require(len(paths) == len(set(paths)), "duplicate run directories in schedule")
    for name in contract["scenarios"]:
        require(sum(p["scenario"] == name for p in trial["pairs"]) >= contract["min_pairs"],
                "schedule lacks required pairs for " + name)
    db.executemany("INSERT INTO reserved_paths VALUES(?)", [(path,) for path in paths])
    row["schedule"] = ref
    row["scheduled_slots"] = paths
    row["phase"] = "assess" if row["phase"] == "schedule" else "confirm"


def stamp(state, row, a):
    require(not state["stop_requested"] and not timed_out(state), "campaign stopping")
    m, _ = load_object(a.metadata)
    phase = row["phase"]
    diag_side = {"baseline_profile": "baseline", "candidate_profile": "candidate"}.get(phase)
    require(m.get("scenario") in state["cases"], "unknown metadata scenario")
    if diag_side:
        require(m.get("run_purpose") == "diagnostic" and m.get("profiler_enabled") is True
                and identity(m) == row[diag_side] and m.get("scenario") == row["scenario"],
                "wrong diagnostic metadata for this phase")
    else:
        require(phase in ("assess", "confirm") and a.run_dir is not None, "specify a registered --run-dir")
        require(m.get("run_purpose") == "performance" and m.get("profiler_enabled") is False,
                "expected unprofiled performance metadata")
        trial, ref = load_object(row["schedule"]["path"])
        require(ref == row["schedule"], "registered schedule changed")
        run = a.run_dir.resolve()
        require(not run.exists(), "formal run path already exists")
        parent = Path(ref["path"]).parent
        require(any((parent / pair[side]).resolve() == run and identity(m) == row[side]
                    and m["scenario"] == pair["scenario"] for pair in trial["pairs"] for side in ("baseline","candidate")),
                "run not registered for this scenario/side")
        m["campaign_run_dir"] = str(run)
    if diag_side:
        m["profile_capture_id"] = row["challenge"]
    m["campaign_binding"] = row["challenge"]
    metadata = m
    with a.output.open("x", encoding="utf-8") as handle:
        handle.write(encode(metadata) + "\n")
    return fingerprint(a.output)


def assess(db, state, row, a):
    from scripts.round_evidence import assess as evaluate
    phase = row["phase"]
    require(phase in ("assess", "confirm"), "no new assessment due")
    require(fingerprint(state["contract"]["path"]) == state["contract"], "comparison contract changed")
    require(fingerprint(a.evidence) == row["schedule"], "must use the original registered pairs")
    trial, _ = load_object(a.evidence)
    for pair in trial["pairs"]:
        for side in ("baseline", "candidate"):
            run = (a.evidence.resolve().parent / pair[side]).resolve()
            metadata, _ = load_object(run / "metadata.json")
            manifest, _ = load_object(run / "manifest.json")
            recorded = Path(manifest["client_arguments"]["run_dir"])
            require(metadata.get("campaign_run_dir") == str(run) and recorded.is_absolute() and recorded.resolve() == run, "run was collected for another output slot")
            require(metadata.get("campaign_binding") == row["challenge"], "run not collected for the active trial")
    result = evaluate(Path(state["contract"]["path"]), a.evidence, row["baseline"], row["candidate"])
    for run in result["comparison"]["validated_inputs"]:
        require(run["manifest"]["base_url"] == state["endpoint"]["base_url"], "formal endpoint changed")
    require(result["case_identity"] == state["cases"], "case drift versus campaign baseline")
    require(len(result["run_ids"]) == len(result["requests_hashes"]), "incomplete run identities")
    for run_id, req_hash in zip(result["run_ids"], result["requests_hashes"]):
        db.execute("INSERT INTO used_runs VALUES(?,?)", (run_id, req_hash))
    evidence = row["evidence"]
    evidence["evidence_files"].extend(result.pop("evidence_files"))
    if row.get("trial_plan"):
        plan = row["trial_plan"]
        verify_files([plan])
        result["execution"] = trial_plan.verify_execution(plan["path"], result)
        result["execution"]["plan"] = plan
        if phase == "confirm":
            instances = {r["instance_id"] for prior in evidence["assessments"] for r in prior["runs"]}
            require(not instances.intersection(slot["instance_id"] for slot in result["execution"]["slots"]),
                    "confirmation reuses a discovery deployment instance")
        evidence["evidence_files"].append(plan)
    if phase == "confirm":
        result["confirmation_order"] = trial_plan.confirmation_order(
            evidence["assessments"][0].get("execution"), result.get("execution"),
            required=row.get("acceptance_protocol", {}).get("require_confirmation_clock", False))
    result["registration"] = {"stage": phase, "pairs": row["schedule"], "binding": row["challenge"]}
    evidence["assessments"].append(result)
    if result["recommendation"] != "adopt":
        row["phase"] = "restore"
    elif phase == "assess" and not state["single"]:
        row["phase"] = "schedule_confirmation"
    else:
        row["phase"] = "accept"


def retry(db, state, row, a):
    require(row["phase"] in ("assess", "confirm"), "only an active formal batch can be replaced")
    attempts = row["evidence"].setdefault("invalid_attempts", [])
    require(len(attempts) < state["max_retries"], "round retry budget exhausted")
    require(a.reason.strip(), "record the reason for retry")
    registered = row["schedule"]
    trial, ref = load_object(registered["path"])
    require(ref == registered, "restore original schedule before retry")
    db.execute("SAVEPOINT retry_check")
    try:
        assess(db, state, json.loads(encode(row)), argparse.Namespace(evidence=Path(registered["path"])))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.IntegrityError) as exc:
        failure, error_type = str(exc), type(exc).__name__
    else:
        raise ValueError("registered trial is valid; record its measured outcome with assess")
    finally:
        db.execute("ROLLBACK TO retry_check")
        db.execute("RELEASE retry_check")
    old_slots = map(Path, retry_evidence.slots(registered, trial))
    slot_paths = [str(path.parent.resolve() / path.name) for path in old_slots]
    require(row.get("scheduled_slots", slot_paths) == slot_paths,
            "retired slot no longer resolves under its registered parent")
    original_paths = set(slot_paths)
    require(all(db.execute("SELECT 1 FROM reserved_paths WHERE path=?", (path,)).fetchone()
                for path in original_paths), "retired slot differs from its registered path")
    new_trial, _ = load_object(a.evidence)
    compare_trials.validate_trial(new_trial, load_object(state["contract"]["path"])[0])
    new_paths = {str((a.evidence.resolve().parent / pair[side]).resolve()) for pair in new_trial["pairs"]
                 for side in ("baseline", "candidate")}
    require(original_paths.isdisjoint(new_paths), "retry needs entirely new directories")
    attempt = {"schedule": registered, "paths": sorted(original_paths), "reason": a.reason, "error": failure,
               "error_type": error_type, "stage": row["phase"], "binding": row["challenge"]}
    if row.get("trial_plan"):
        attempt["plan"] = row["trial_plan"]
    row["phase"] = "schedule_confirmation" if row["phase"] == "confirm" else "schedule"
    schedule(db, state, row, a)
    attempt["snapshot"] = retry_evidence.capture(a.campaign, attempt, trial)
    attempts.append(attempt)
    row["evidence"]["evidence_files"].extend([registered, attempt["snapshot"]])


def review_evidence(row):
    from diagnose.scripts.profile import verify_bundle
    verify_files(row["evidence"]["evidence_files"])
    review_step_analyses(row["evidence"])
    retry_evidence.review(row)
    for recorded in row["evidence"]["profiles"].values():
        checked = verify_bundle(Path(recorded["path"]))
        require(checked["valid"] and checked["manifest_sha256"] == recorded["manifest_sha256"]
                and checked["spec_sha256"] == recorded["spec_sha256"], "profile changed since validation")
    for assessment in row["evidence"]["assessments"]:
        execution = assessment.get("execution")
        if execution:
            trial_plan.load_plan(execution["plan"]["path"])


def check_closure_model(row, observation):
    expected = row.get("acceptance_protocol", {}).get("model_snapshot_sha256")
    if expected is not None:
        require(observation.get("model_snapshot_sha256") == expected, "closure model differs from the measured model")


def close_round(state, row, a):
    from scripts.round_evidence import check_observation
    require(a.observation.strip(), "record an observation/reason for this decision")
    action = getattr(a, "site_action", None)
    required = state.get("require_site_release", False)
    require(row.get("acceptance_protocol", {}).get("require_site_release", False) is required,
            "round release policy differs from campaign initialization")
    require(not required or action, "site release is required; close with the active cleanup --site-action")
    reason = stop_reason(state, device_status(state))
    if a.decision == "adopt":
        require(row["phase"] == "accept", "adoption requires complete successful assessment")
        require(not reason, "stop requested; restore baseline")
        review_evidence(row)
    else:
        try:
            review_step_analyses(row["evidence"])
        except (OSError, ValueError) as exc:
            # Keep the evidence failure auditable while allowing verified baseline recovery.
            row["evidence"]["step_analysis_review_error"] = str(exc)
    if a.decision == "revert":
        require(row["candidate"] is not None, "no planned candidate to revert")
    chosen = row["candidate"] if a.decision == "adopt" else row["baseline"]
    obs = check_observation(a.deployment, chosen, **state["endpoint"])
    check_closure_model(row, obs)
    receipt, health_ref = load_object(a.health)
    require(fingerprint(a.deployment) == row.get("last_probe"), "deployment observation was superseded")
    require(health_ref == row.get("last_health"), "health receipt was not issued for the current transition")
    verify_health(receipt, state, row, a.deployment, obs)
    if action is not None:
        require(state.get("device_budget"), "site closure needs the registered device budget")
        ledger = device_budget.Budget(state["device_budget"]["path"]).audit()
        require(ledger["pending"] == [action], "site closure must name the active cleanup lease")
        lease = next(item for item in ledger["leases"] if item["action_id"] == action)
        directory = a.deployment.resolve(strict=True).parent
        require(lease["cleanup"] and directory.name == action and directory.parent.name == "deployments"
                and a.health.resolve(strict=True).parent == directory, "closure lease and observation directory differ")
        container, container_ref = load_object(directory / "container.json")
        require(container["label"] == action and obs["instance_id"].split(":", 1)[0] == container["id"],
                "closure observation is for another container")
        release = {"status": "pending", "round": row["round"], "action_id": action, "container": container_ref,
                   "claim": {key: lease[key] for key in ("host", "boot_id", "devices", "start_mono_ns")}}
        row["site_release"] = state["site_release"] = release
        row["evidence"]["evidence_files"].append(container_ref)
    now = time.time()
    row["closure_binding"] = {"challenge": row["challenge"], "challenge_time": row["challenge_time"]}
    row.update(decision=a.decision, observation=a.observation, phase="closed", closed_at=now,
               final_deployment={"identity": chosen, "instance_id": obs["instance_id"],
                                 "observation": fingerprint(a.deployment), "verified_at": now})
    row["evidence"]["evidence_files"].extend([fingerprint(a.deployment), health_ref])
    state.update(deployed=row["final_deployment"], baseline=chosen)
    if reason:
        row["stop_reason"] = reason
        state["stopped"] = reason
    elif a.decision == "adopt" and state["goal_tps"] is not None:
        comparison = row["evidence"]["assessments"][-1]["comparison"]
        ids = {p["candidate_run_id"] for p in comparison["pairs"] if p["scenario"] == state["goal_scenario"]}
        metric = state.get("goal_metric", RAW_THROUGHPUT)
        rates = [r["metrics"][metric] for r in comparison["validated_inputs"]
                 if r["manifest"]["run_id"] in ids]
        if rates and all(rate is not None and rate >= state["goal_tps"] for rate in rates):
            state["stopped"] = "goal_reached"
    if not state["stopped"] and state["count"] == state["max_rounds"]:
        state["stopped"] = "round_budget"


def verify_health(receipt, state, row, deployment, obs, *, original_ref=None, latest=None):
    probe = obs.get("round_probe", {})
    release_policy = row.get("acceptance_protocol", {}).get("require_site_release", False)
    require(type(release_policy) is bool and probe.get("require_site_release", False) is release_policy,
            "closure observation differs from the frozen release policy")
    require(probe.get("challenge") == row["challenge"] and probe.get("campaign_id") == state["campaign_id"]
            and probe.get("round") == row["round"], "refresh deployment observation for this phase")
    require(receipt.get("campaign_id") == state["campaign_id"] and receipt.get("round") == row["round"]
            and receipt.get("challenge") == row["challenge"]
            and receipt.get("deployment") == (original_ref or fingerprint(deployment)), "health receipt belongs to another deployment or round")
    require(receipt.get("instance_id") == obs["instance_id"] and receipt.get("base_url") == obs["base_url"],
            "health receipt endpoint or instance mismatch")
    reply = receipt.get("response")
    require(receipt.get("request", {}).get("messages") == [{"role": "user", "content": "Reply OK. Request " + row["challenge"]}],
            "health request lacks current round challenge")
    require(receipt.get("http_status") == 200 and isinstance(reply, dict)
            and reply.get("model") == obs["model"] and reply.get("id")
            and isinstance(reply.get("choices"), list) and reply["choices"]
            and isinstance(reply["choices"][0], dict)
            and reply["choices"][0].get("finish_reason") in ("stop", "length"),
            "health check did not yield a completed chat response")
    message = reply["choices"][0].get("message")
    require(isinstance(message, dict) and message.get("role") == "assistant"
            and any(isinstance(message.get(k), str) and message[k].strip()
                    for k in ("content","reasoning_content")), "health reply lacks assistant output")
    require(row["challenge_time"] <= receipt.get("at", 0) <= (time.time() + 1 if latest is None else latest),
            "health receipt predates the latest transition")


def observe(state, row, a):
    argv = a.program[1:] if a.program[:1] == ["--"] else a.program
    require(argv, "supply a live deployment observation command after --")
    require(not a.output.exists(), "observation output already exists")
    done = subprocess.run(argv, capture_output=True, timeout=60, check=True)
    obj = json.loads(done.stdout, object_pairs_hook=validate_run.strict_object)
    require(isinstance(obj, dict) and "effective_config" in obj, "observer must emit a deployment object")
    obj["round_probe"] = {"campaign_id": state["campaign_id"], "round": row["round"],
                         "challenge": row["challenge"], "at": time.time(), "argv": argv}
    if state.get("require_site_release"):
        obj["round_probe"]["require_site_release"] = True
    with a.output.open("x", encoding="utf-8") as handle:
        handle.write(encode(obj) + "\n")
    return fingerprint(a.output)


def health(state, row, a):
    from scripts.round_evidence import check_observation
    chosen = row["candidate"] if a.side == "candidate" else row["baseline"]
    require(chosen is not None, "no candidate planned")
    obs = check_observation(a.deployment, chosen, **state["endpoint"])
    require(fingerprint(a.deployment) == row.get("last_probe"), "use the observation issued in this phase")
    endpoint = benchmark.parse_endpoint(obs["base_url"])
    payload = {"model": obs["model"], "max_tokens": 8, "stream": False,
               "messages": [{"role": "user", "content": "Reply OK. Request " + row["challenge"]}]}
    headers = {"Content-Type": "application/json"}
    if os.environ.get(a.api_key_env):
        headers["Authorization"] = "Bearer " + os.environ[a.api_key_env]
    client = http.client.HTTPSConnection if endpoint["scheme"] == "https" else http.client.HTTPConnection
    with closing(client(endpoint["host"], endpoint["port"], timeout=30)) as conn:
        conn.request("POST", endpoint["path"], body=encode(payload).encode(), headers=headers)
        response = conn.getresponse()
        code = response.status
        require(code == 200, "health request returned HTTP " + str(code))
        reply = json.load(response)
    receipt = {"schema_version": 1, "campaign_id": state["campaign_id"], "round": row["round"],
               "challenge": row["challenge"], "at": time.time(), "deployment": fingerprint(a.deployment),
               "base_url": obs["base_url"], "instance_id": obs["instance_id"], "http_status": code,
               "request": payload, "response": reply}
    verify_health(receipt, state, row, a.deployment, obs)
    with a.output.open("x", encoding="utf-8") as handle:
        handle.write(encode(receipt) + "\n")
    return fingerprint(a.output)


def get_round(db, number):
    result = db.execute("SELECT data FROM rounds WHERE id=?", (number,)).fetchone()
    return json.loads(result[0]) if result else None


def verify_closure_release(recorded, lease, proof, ref, directory):
    claim = recorded["claim"]
    require(lease["action_id"] == recorded["action_id"] == directory.name
            and set(claim) == {"host", "boot_id", "devices", "start_mono_ns"}
            and all(lease[k] == v for k, v in claim.items())
            and lease["cleanup"] is True and lease["end_mono_ns"] is not None
            and lease["proof_path"] == ref["path"] and lease["proof_sha256"] == ref["sha256"]
            and Path(ref["path"]).parent == directory and proof.get("action_id") == recorded["action_id"]
            and proof.get("free") is True and proof.get("site_evidence", {}).get("processes_on_card") == {}
            and all(proof[k] == lease[k] for k in ("host", "boot_id", "devices"))
            and lease["start_mono_ns"] < proof["observed_mono_ns"] <= lease["end_mono_ns"],
            "release evidence does not match this closure")


def attach_release(db, state, number):
    row = get_round(db, number)
    require(row and row["phase"] == "closed" and row.get("site_release"), "no site closure to reconcile")
    recorded = row["site_release"]
    ledger = device_budget.Budget(state["device_budget"]["path"]).audit()
    action = recorded["action_id"]
    lease = next((item for item in ledger["leases"] if item["action_id"] == action), None)
    require(lease and lease["cleanup"] and lease["end_mono_ns"] is not None
            and all(lease[k] == v for k, v in recorded["claim"].items()), "closure lease is pending or changed")
    proof, ref = load_object(lease["proof_path"])
    directory = Path(row["final_deployment"]["observation"]["path"]).parent
    verify_closure_release(recorded, lease, proof, ref, directory)
    release = {**recorded, "status": "released", "lease": lease, "proof": ref}
    if recorded["status"] == "released":
        require(recorded == release, "recorded release changed")
        return release
    require(state.get("site_release") == recorded, "latest site closure differs")
    row["site_release"] = state["site_release"] = release
    row["evidence"]["evidence_files"].append(ref)
    db.execute("UPDATE rounds SET data=? WHERE id=?", (encode(row), number))
    db.execute("UPDATE campaign SET state=? WHERE id=1", (encode(state),))
    event(db, number, "attach-release", {"action_id": action, "proof": ref})
    return release


def transition(db, a):
    if a.command == "init":
        return init(db, a)
    state = json.loads(db.execute("SELECT state FROM campaign WHERE id=1").fetchone()[0])
    row = get_round(db, state["count"])
    if row and row["phase"] == "closed":
        row = None
    if a.command == "status":
        return view(state, row)
    if a.command == "history":
        return [dict(id=i, at=t, round=n, action=k, details=json.loads(v)) for i,t,n,k,v in
                db.execute("SELECT id,at,round,action,data FROM events ORDER BY id")]
    if a.command == "export":
        result = get_round(db, a.round)
        require(result and result["phase"] == "closed", "round has not closed")
        if state.get("require_site_release"):
            from scripts import round_archive
            require(result["acceptance_protocol"].get("require_site_release") is True,
                    "export lacks the campaign's frozen release policy")
            round_archive.release_status(result, Path)
        if a.recovery_only:
            from scripts import recovery_archive
            return recovery_archive.export(result)
        review_evidence(result)
        return result
    if a.command == "attach-release":
        return attach_release(db, state, a.round)
    require(state.get("site_release", {}).get("status") != "pending", "attach the previous site release before continuing")
    require(not state["stopped"], "campaign already stopped: " + str(state["stopped"]))
    if a.command == "stop":
        reason = stop_reason(state, device_status(state)) or a.reason
        require(reason and reason.strip(), "supply the user's stop reason")
        if row and not state["stop_requested"]:
            row.update(challenge=uuid.uuid4().hex, challenge_time=time.time())
            row.pop("last_probe", None)
            row.pop("last_health", None)
            db.execute("UPDATE rounds SET data=? WHERE id=?", (encode(row), row["round"]))
        state["stop_requested"] = reason
        if not row:
            state["stopped"] = reason
    else:
        if a.command not in ("health", "observe", "close"):
            require(not stop_reason(state, device_status(state)), "budget or user stop; recover and close")
        if a.command == "begin":
            require(not row, "an active round exists")
            row = begin(state, a)
        else:
            require(row and row["round"] == a.round, "command must name the active round")
            if a.command in ("health", "observe"):
                if a.command == "observe":
                    ref = observe(state, row, a)
                    row["last_probe"] = ref
                    row.pop("last_health", None)
                else:
                    ref = health(state, row, a)
                    row["last_health"] = ref
                db.execute("UPDATE rounds SET data=? WHERE id=?", (encode(row), row["round"]))
                event(db, row["round"], a.command, ref)
                return ref
            if a.command == "stamp":
                return stamp(state, row, a)
            if a.command == "profile":
                # A lost CLI response can be retried without rotating the phase challenge.
                previous_side = {"plan": "baseline", "schedule": "candidate"}.get(row["phase"])
                previous = row["evidence"]["profiles"].get(previous_side)
                if previous and "step_analysis" in previous:
                    options = diagnosis_options(a)
                    require(str(a.bundle.resolve(strict=True)) == previous["path"]
                            and options == previous["step_analysis_options"],
                            "profile already recorded with different diagnostic inputs")
                    review_evidence(row)
                    return view(state, row)
                profile(db, state, row, a)
            elif a.command == "plan":
                plan(row, a)
            elif a.command == "schedule":
                schedule(db, state, row, a)
            elif a.command == "retry":
                retry(db, state, row, a)
            elif a.command == "assess":
                assess(db, state, row, a)
            elif a.command == "close":
                close_round(state, row, a)
        row.update(challenge=uuid.uuid4().hex, challenge_time=time.time())
        db.execute("INSERT OR REPLACE INTO rounds VALUES(?,?)", (row["round"], encode(row)))
    db.execute("UPDATE campaign SET state=? WHERE id=1", (encode(state),))
    result = view(state, row if row and row["phase"] != "closed" else None)
    details = {"next_action": result["next_action"],
               "inputs": {k: str(v) for k,v in vars(a).items() if k not in ("campaign", "command")}}
    if a.command == "close" and "step_analysis_review_error" in row["evidence"]:
        error = row["evidence"]["step_analysis_review_error"]
        result["step_analysis_review_error"] = error
        details["step_analysis_review_error"] = error
    event(db, state["count"], a.command, details)
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    commands = {}
    for name in ("init","status","begin","profile","plan","schedule","stamp","retry","assess","observe","health","close","attach-release","stop","history","export"):
        cmd = commands[name] = sub.add_parser(name)
        cmd.add_argument("--campaign", type=Path, required=True, help="campaign directory (local SQLite)")
        if name in ("begin","profile","plan","schedule","stamp","retry","assess","observe","health","close","attach-release","export"):
            cmd.add_argument("--round", type=int, required=True)
    commands["export"].add_argument("--recovery-only", action="store_true",
                                    help="verify and export baseline restoration after revert or abandon")
    cmd = commands["init"]
    cmd.add_argument("--contract", type=Path, required=True)
    cmd.add_argument("--scope", required=True, help="model, hardware and workload applicability")
    cmd.add_argument("--baseline-run", type=Path, action="append", required=True, help="at least one run directory per case")
    cmd.add_argument("--max-rounds", type=int, required=True)
    cmd.add_argument("--max-retries", type=int, default=1, help="additional invalid formal batches per round")
    cmd.add_argument("--hours", type=float)
    cmd.add_argument("--single", action="store_true", help="one preselected candidate, no search")
    cmd.add_argument("--goal-scenario")
    cmd.add_argument("--goal-tps", type=float)
    cmd.add_argument("--goal-metric", choices=(RAW_THROUGHPUT, compare_trials.GOODPUT_METRIC))
    cmd.add_argument("--require-trial-plan", action="store_true")
    cmd.add_argument("--require-site-release", action="store_true",
                     help="require a bound cleanup lease and verified proof before completing site delivery")
    cmd.add_argument("--device-budget", type=Path, help="initialized site device-budget SQLite ledger")
    cmd.add_argument("--minimum-device-seconds", type=float, default=0,
                     help="minimum remaining die-seconds for new work")
    commands["begin"].add_argument("--profile-policy", type=Path, required=True)
    commands["begin"].add_argument("--scenario", required=True)
    commands["profile"].add_argument("--bundle", type=Path, required=True)
    commands["profile"].add_argument("--step-sources", type=Path, help="frozen run-bound step source spec")
    commands["profile"].add_argument("--timeline-map", type=Path, help="versioned trace event mapping")
    commands["profile"].add_argument("--diagnostic-run", type=Path, help="original diagnostic directory with retained SSE")
    for name in ("hypothesis", "prediction"):
        commands["plan"].add_argument("--" + name, required=True)
    commands["plan"].add_argument("--config", type=Path, required=True, help="candidate configuration JSON")
    commands["plan"].add_argument("--implementation", type=Path)
    commands["plan"].add_argument("--git-repo", type=Path, help="optional source repository for this candidate")
    commands["plan"].add_argument("--git-rev", help="resolve a candidate revision to an immutable commit and tree")
    commands["plan"].add_argument("--change", type=Path, required=True, help="patch or change and recovery record")
    commands["stamp"].add_argument("--run-dir", type=Path)
    commands["stamp"].add_argument("--metadata", type=Path, required=True)
    commands["stamp"].add_argument("--output", type=Path, required=True)
    commands["schedule"].add_argument("--evidence", type=Path, required=True)
    commands["schedule"].add_argument("--plan", type=Path, help="seeded execution plan accompanying the pair list")
    commands["retry"].add_argument("--evidence", type=Path, required=True)
    commands["retry"].add_argument("--plan", type=Path, help="replacement seeded plan with fresh slots")
    commands["retry"].add_argument("--reason", required=True)
    commands["assess"].add_argument("--evidence", type=Path, required=True)
    commands["observe"].add_argument("--output", type=Path, required=True)
    commands["observe"].add_argument("program", nargs=argparse.REMAINDER)
    for name in ("health","close"):
        commands[name].add_argument("--deployment", type=Path, required=True, help="observed effective deployment JSON")
    commands["health"].add_argument("--side", choices=("baseline","candidate"), required=True)
    commands["health"].add_argument("--api-key-env", default="OPENAI_API_KEY")
    commands["health"].add_argument("--output", type=Path, required=True)
    commands["close"].add_argument("--decision", choices=("adopt","revert","abandon"), required=True)
    commands["close"].add_argument("--observation", required=True)
    commands["close"].add_argument("--health", type=Path, required=True)
    commands["close"].add_argument("--site-action", help="910C cleanup lease to attest after the final observation")
    commands["stop"].add_argument("--reason", help="user stop or documented exhaustion of directions")
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    database = a.campaign.resolve() / "campaign.sqlite"
    reading = a.command in ("status","history","export","stamp")
    mode = "rwc" if a.command == "init" else ("ro" if reading else "rw")
    try:
        if a.command == "init":
            a.campaign.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database.as_uri() + "?mode=" + mode, uri=True,
                                     isolation_level=None, timeout=10)) as db:
            db.execute("BEGIN" if reading else "BEGIN IMMEDIATE")
            try:
                result = transition(db, a)
            except COMMAND_ERRORS as exc:
                db.rollback()
                if not reading and a.command != "init":
                    event(db, getattr(a, "round", 0), "rejected:" + a.command, {"error": str(exc)})
                raise
            db.commit()
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except COMMAND_ERRORS as exc:
        print("campaign: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

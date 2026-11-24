#!/usr/bin/env python3
"""Run and reconcile budgeted vLLM deployments on one Ascend 910C die."""

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ascend_910c as host
from scripts import round_evidence
from scripts.device_budget import Budget

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
PERF_TOOL = ROOT / "evaluate/scripts/benchmark.py"
STANDALONE_PROFILE = ROOT / "diagnose/scripts/vllm_profile.py"
PROFILE = ROOT / "diagnose/scripts/profile.py"


@contextmanager
def lock_card(settings):
    with ExitStack() as stack:
        for path in settings.lock_paths:
            stream = stack.enter_context(path.open("a"))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("device is reserved by another experiment: " + str(path)) from None
        yield


def machine(site):
    return socket.gethostname(), BOOT_ID.read_text().strip(), [f"davinci{site.settings.die}"]


def site_path(root, path):
    root = root.resolve(strict=True)
    path = Path(path).resolve()
    path.relative_to(root)
    return path


def parse_case(spec, scenario):
    obj = host.read(spec)
    host.require(set(obj) == {"schema_version", "scenarios"} and obj["schema_version"] == 1,
                 "invalid site case settings")
    row = obj["scenarios"][scenario]
    required = {"dataset", "concurrency"}
    allowed = required | {"warmup_rounds", "warmup_dataset", "mode", "workers", "seed", "temperature", "top_p", "timeout"}
    host.require(required <= row.keys() <= allowed, "invalid options for site case " + scenario)
    dataset = (spec.parent / row["dataset"]).resolve(strict=True)
    parallelism = row["concurrency"]
    workers = row.get("workers", parallelism)
    warmups = row.get("warmup_rounds", 1)
    host.require(all(type(v) is int and v >= 1 for v in (parallelism, workers)) and workers >= parallelism
                 and type(warmups) is int and warmups >= 0, "invalid worker count or warmup")
    source = row.get("warmup_dataset")
    if "warmup_dataset" in row:
        host.require(warmups > 0 and isinstance(source, dict) and set(source) in ({"path", "sha256"}, {"path", "sha256", "shared_prefix"})
                     and isinstance(source["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                     and isinstance(source["path"], str) and source["path"], "invalid warmup dataset reference")
        if "shared_prefix" in source:
            rule = source["shared_prefix"]
            host.require(isinstance(rule, dict) and set(rule) == {"characters", "matching_requests"}
                         and type(rule["characters"]) is int and rule["characters"] > 0
                         and type(rule["matching_requests"]) is int and rule["matching_requests"] >= 0,
                         "invalid character-prefix overlap rule")
        source = {**source, "path": (spec.parent / source["path"]).resolve(strict=True)}
    options = {"model": None, "dataset": dataset, "concurrency": parallelism, "workers": workers,
               "mode": row.get("mode", "closed_loop"), "seed": row.get("seed", 42),
               "temperature": row.get("temperature", 0), "top-p": row.get("top_p", 1),
               "timeout": row.get("timeout", 60)}
    return options, warmups, source


def prepare_warmup(live, options, rounds, source):
    """Snapshot inputs before sending any requests. Return portable policy and local evidence."""
    concurrency = options["concurrency"]
    policy = {"rounds": rounds, "concurrency": concurrency, "mode": "no_workload_warmup"}
    artifacts = []
    results = []
    if rounds:
        entire = source is not None
        dataset = source["path"] if entire else options["dataset"]
        payload = dataset.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        host.require(not entire or digest == source["sha256"], "warmup dataset differs from its frozen digest")
        local = live.directory / "warmup-input.jsonl"
        with local.open("xb") as stream:
            stream.write(payload)
        policy.update(mode="explicit_dataset" if entire else "measured_first_concurrency", dataset_sha256=digest)
        if entire and "shared_prefix" in source:
            warm = [round_evidence.benchmark.strict_json(line) for line in payload.splitlines() if line.strip()]
            measured = [round_evidence.benchmark.strict_json(line) for line in options["dataset"].read_bytes().splitlines()
                        if line.strip()]
            policy["shared_prefix"] = round_evidence.warmup_overlap(warm, measured, source["shared_prefix"])
        results = live.warmup(local, concurrency, rounds, entire_dataset=entire)
        artifacts.append(local.name)
    receipt = live.directory / "warmup-receipt.json"
    host.write(receipt, {"policy": policy, "round_results": results})
    return policy, artifacts + [receipt.name]


def invoke(script, *args, log=None, codes=(0,), timeout=None):
    command = [sys.executable, "-B", str(script), *map(str, args)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=5)
            except ProcessLookupError:
                process.wait()
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise
    if log is not None:
        with log.open("x", encoding="utf-8") as file:
            file.write(stdout)
            file.write(stderr)
    if process.returncode not in codes:
        raise RuntimeError(f"{script.name} returned {process.returncode}: {stderr[-1800:]}")
    return stdout


def attest_free(site, action_id, budget, directory, outcome):
    raw = host.card_snapshot(site.settings, set(), False)
    observed = time.monotonic_ns()
    hostname, boot_id, devices = machine(site)
    proof = directory / f"device-release-{observed}.json"
    host.write(proof, {"action_id": action_id, "host": hostname, "boot_id": boot_id, "devices": devices, "free": True,
                       "observed_mono_ns": observed, "site_evidence": raw})
    return budget.release(action_id, time.monotonic_ns(), proof, outcome)


def work_timeout(_signal, _frame):
    raise TimeoutError("device work deadline interrupted the test")


@contextmanager
def work_deadline(seconds):
    old_alarm = signal.signal(signal.SIGALRM, work_timeout)
    old_term = signal.signal(signal.SIGTERM, work_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_alarm)
        signal.signal(signal.SIGTERM, old_term)


def collect(site, live, request):
    run = site_path(site.root, request.run)
    run.parent.mkdir(parents=True, exist_ok=True)
    options, warmups, warmup_source = parse_case(request.case, request.scenario)
    options["model"] = site.model
    warmup_policy, artifacts = prepare_warmup(live, options, warmups, warmup_source)
    metric = live.directory / "site-metric.json"
    metric_input = host.read(request.metric)
    host.require(isinstance(metric_input, dict) and "site_warmup_policy" not in metric_input,
                 "site_warmup_policy is generated by the site worker")
    host.write(metric, {**metric_input, "site_warmup_policy": warmup_policy})
    env = host.read(site.root / "environment.json")
    snapshot = host.read(live.directory / "model-snapshot.json")
    environment = live.directory / "site-environment.json"
    host.write(environment, {**env, "model_hashes": snapshot["files_sha256"],
                             "model_snapshot_sha256": snapshot["content_sha256"]})
    proof = host.read(live.evidence)
    evidence = live.directory / "measurement-evidence.json"
    host.write(evidence, {**proof, "evidence": proof["evidence"] + artifacts})
    live.assert_isolated("benchmark-before")
    meta = live.directory / "metadata.unbound.json"
    diagnostic = request.profile_spec is not None
    invoke(ROOT / "evaluate/scripts/prepare_metadata.py", "--scenario", request.scenario,
           "--config", live.config, "--environment", environment,
           "--metric-contract", metric, "--config-evidence", evidence, "--output", meta,
           "--profiler-enabled", "on" if diagnostic else "off",
           "--run-purpose", "diagnostic" if diagnostic else "performance", timeout=10)
    if request.campaign:
        bound = live.directory / "metadata.bound.json"
        campaign_args = ["stamp", "--campaign", request.campaign, "--round", request.round,
                         "--metadata", meta, "--output", bound]
        if not diagnostic:
            campaign_args.extend(["--run-dir", run])
        invoke(ROOT / "scripts/campaign.py", *campaign_args, timeout=20)
        meta = bound
    options["metadata"] = meta
    arguments = [arg for key, value in options.items() for arg in ("--" + key, value)]
    if diagnostic:
        spec = host.read(request.profile_spec)
        host.require((request.profile_spec.parent / spec["diagnostic_run"]).resolve() == run,
                     "profile spec must refer to the registered diagnostic run slot")
        bundle = live.directory / "profile-bundle"
        invoke(PROFILE, "capture", "--spec", request.profile_spec, "--out", bundle,
               "--timeout", request.max_seconds, "--",
               sys.executable, "-B", STANDALONE_PROFILE, "--spec", request.profile_spec,
               "--base-url", site.base_url, "--export-root", live.export_root, "--",
               *arguments, log=live.directory / "profile.log")
    else:
        invoke(PERF_TOOL, "--base-url", site.base_url, "--run-dir", run, *arguments,
               log=live.directory / "benchmark.log", codes=(0, 1))
    live.assert_isolated("benchmark-after")
    result = round_evidence.validate_run.inspect_run(run)
    host.require(result["valid"], "benchmark failed evidence validation: " + str(result["errors"]))
    tail_path = live.directory / "stream-tail.json"
    raw_tail = invoke(ROOT / "diagnose/scripts/stream_tail.py", "--run", run, timeout=30)
    host.write(tail_path, json.loads(raw_tail))
    return {"run": str(run), "run_id": result["manifest"]["run_id"], "metrics": result["metrics"],
            "observation": str(live.directory / "observation.json"),
            "stream_tail": {"path": str(tail_path), "sha256": host.sha(tail_path)},
            "profile_bundle": str(bundle) if diagnostic else None}


def with_device(site, request, function, *, cleanup=False):
    action = request.action
    host.require(re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", action), "invalid action ID")
    host.require(math.isfinite(request.max_seconds) and request.max_seconds > request.cleanup_seconds > 0,
                 "the maximum lease must leave an interval for cleanup")
    directory = site.root / "deployments" / action
    budget = Budget(request.budget)
    with lock_card(site.settings):
        host.require(not directory.exists(), "action must have a fresh destination")
        host.require(host.optional_container(site.name) is None, "an owned service needs reconciliation")
        host.verify_container_reservation(site.settings)
        host.card_snapshot(site.settings, set(), False)
        budget.claim(action, *machine(site), time.monotonic_ns(), request.max_seconds,
                     reserve_seconds=request.reserve_die_seconds, cleanup=cleanup)
        result = None
        failure = None
        try:
            with work_deadline(request.max_seconds - request.cleanup_seconds):
                with site.deployment(request.config_key, action) as live:
                    try:
                        result = function(live)
                    finally:
                        signal.setitimer(signal.ITIMER_REAL, 0)
        except (Exception, KeyboardInterrupt) as error:
            failure = str(error)
        # Leave a pending lease if we cannot prove release. A new action will
        # fail admission until an operator invokes reconcile.
        directory.mkdir(exist_ok=True)
        host.require(host.optional_container(site.name) is None, "deployment still exists; reconcile the lease")
        charge = attest_free(site, action, budget, directory, "collected" if failure is None else "failed")
        receipt = {**charge, "error": failure, "measurement": result,
                   "scenario": request.scenario, "configuration": request.config_key,
                   "seconds": charge["die_seconds"], "released_mono_ns": charge["end_mono_ns"],
                   "real_card_cleanup": True}
        host.write(directory / "action-receipt.json", receipt)
        return receipt


def measure(site, request):
    host.require(not site_path(site.root, request.run).exists(), "run must have a fresh destination")
    parse_case(request.case, request.scenario)
    return with_device(site, request, lambda live: collect(site, live, request))


def reconcile(site, budget, action):
    host.require(re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", action), "invalid action ID")
    directory = site.root / "deployments" / action
    with lock_card(site.settings):
        state = budget.status()
        charge = next((row for row in state["leases"] if row["action_id"] == action), None)
        host.require(charge is not None, "action does not match a registered lease")
        if charge["end_mono_ns"] is None:
            host.require(state["pending"] == [action], "action does not match the pending lease")
            host.require((charge["host"], charge["boot_id"], charge["devices"]) == machine(site),
                         "pending lease belongs to another host, boot, or device")
            directory.mkdir(exist_ok=True)
            site.stop(action, directory)
            charge = attest_free(site, action, budget, directory, "reconciled_interruption")
        host.require(Path(charge["proof_path"]).parent == directory, "release proof belongs to another site")
        host.require(host.sha(Path(charge["proof_path"])) == charge["proof_sha256"], "release proof changed")
        if charge["outcome"] == "reconciled_interruption":
            path = directory / "reconciled-action.json"
            if path.exists():
                host.require(host.read(path) == charge, "reconciled charge changed")
            else:
                host.write(path, charge)
    context = directory / "closure-context.json"
    if context.exists():
        target = host.read(context)
        campaign = site_path(site.root, target["campaign"])
        control = ROOT / "scripts/campaign.py"
        state = json.loads(invoke(control, "status", "--campaign", campaign))
        release = state.get("site_release", {})
        if release.get("round") == target["round"] and release.get("action_id") == action:
            invoke(control, "attach-release", "--campaign", campaign, "--round", target["round"])
    return charge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("init", "measure", "reconcile", "preflight", "observe"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--site", type=Path, required=True)
        command.add_argument("--template", type=Path)
    for name in ("measure", "reconcile", "observe"):
        sub.choices[name].add_argument("--action", required=True)
    for name in ("measure", "reconcile"):
        sub.choices[name].add_argument("--budget", type=Path, required=True)
    command = sub.choices["measure"]
    command.add_argument("--config-key", required=True)
    command.add_argument("--scenario", required=True)
    for name in ("case", "run", "metric"):
        command.add_argument("--" + name, type=Path, required=True)
    for name in ("campaign", "profile-spec"):
        command.add_argument("--" + name, type=Path)
    command.add_argument("--round", type=int)
    command.add_argument("--max-seconds", type=float, default=210)
    command.add_argument("--cleanup-seconds", type=float, default=40)
    command.add_argument("--reserve-die-seconds", type=float, default=0)
    args = parser.parse_args()
    try:
        site = host.Site(args.root, host.SiteConfig.load(args.site), args.template)
        if args.operation == "init":
            with lock_card(site.settings):
                site.initialize()
            result = host.read(site.root / "preflight.json")
        elif args.operation == "preflight":
            with lock_card(site.settings):
                host.verify_container_reservation(site.settings)
                result = host.card_snapshot(site.settings, set(), False)
        elif args.operation == "measure":
            host.require((args.campaign is None) == (args.round is None), "supply --campaign and --round together")
            result = measure(site, args)
        elif args.operation == "observe":
            result = host.Live(site, site.root / "deployments" / args.action).observe()
        else:
            result = reconcile(site, Budget(args.budget), args.action)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("error") is not None else 0


if __name__ == "__main__":
    sys.exit(main())

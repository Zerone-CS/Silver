"""Replay the qualification and cleanup of a closed round from portable evidence."""

import json
import os
import shutil
import sys
from pathlib import Path

from diagnose.scripts.profile import verify_bundle
from scripts import campaign, device_budget, retry_evidence, round_evidence as ev, trial_plan

require = ev.require


def read_export(path):
    # An aggregate of many assessments can exceed the per-contract JSON limit.
    with Path(path).open(encoding="utf-8") as stream:
        row = json.load(stream, parse_constant=ev.compare_trials.reject_constant,
                        object_pairs_hook=ev.compare_trials.unique_keys)
    ev.compare_trials.check_finite_tree(row, str(path))
    return ev.compare_trials.obj(row, str(path))


def absolute(path):
    path = Path(path)
    require(path.is_absolute(), "expected absolute evidence path: " + str(path))
    return Path(os.path.normpath(path))


class ArchivedFiles:
    """Keep original path relationships without falling back to live host paths."""

    def __init__(self, directory, aliases=None):
        self.aliases = aliases or {}
        self.tree = directory / "payload" / "fs"

    def destination(self, original):
        path = absolute(original)
        path = absolute(self.aliases.get(str(path), path))
        return self.tree / path.relative_to(path.anchor)

    def __call__(self, name):
        path = absolute(name)
        if not path.is_relative_to(self.tree):
            path = self.destination(path)
        require(path.resolve(strict=True) == path, "archive path traverses a symlink")
        return path


def reference_paths(row, archived):
    """Only include the registered artifacts, the profile bundle, and A/A inputs."""
    evidence = row["evidence"]
    yield from evidence["evidence_files"]
    for attempt in evidence.get("invalid_attempts", []):
        if attempt.get("snapshot"):
            yield attempt["snapshot"]
            yield from retry_evidence.files(attempt["snapshot"], archived)
    for profile in evidence["profiles"].values():
        root = Path(profile["path"])
        yield {"path": root / "manifest.json", "sha256": profile["manifest_sha256"]}
        yield {"path": root / "spec.json", "sha256": profile["spec_sha256"]}
        for name, digest in ev.read_object(archived(root / "manifest.json"))["files"].items():
            require(not Path(name).is_absolute() and ".." not in Path(name).parts, "invalid profile filename")
            yield {"path": root / name, "sha256": digest}
        if profile.get("step_analysis"):
            ref = profile["step_analysis"]
            yield ref
            analysis = ev.read_object(archived(ref["path"]))
            for key in ("sources", "analysis_inputs", "benchmark_sources"):
                yield from analysis[key]
            if analysis.get("timeline"):
                yield from analysis["timeline"]["sources"]
    if evidence.get("step_comparison"):
        yield evidence["step_comparison"]
    for result in evidence["assessments"]:
        yield result["registration"]["pairs"]
        if not result.get("execution"):
            continue
        plan_ref = result["execution"]["plan"]
        yield plan_ref
        plan = ev.read_object(archived(plan_ref["path"]))
        ref = plan.get("calibration")
        if ref:
            path = absolute(Path(plan_ref["path"]).parent / ref["path"])
            yield {"path": path, "sha256": ref["sha256"]}
            calibration = ev.read_object(archived(path))
            for file, digest in calibration["sources"].items():
                yield {"path": file, "sha256": digest}
            for samples in calibration["runs"].values():
                for sample in samples:
                    for name, digest in sample["source_hashes"].items():
                        yield {"path": Path(sample["path"]) / name, "sha256": digest}
    release = row.get("site_release", {})
    if release.get("status") == "released":
        yield release["proof"]


def pack(export, roots, output):
    from scripts import evidence_bundle as bundles

    export = export.resolve(strict=True)
    row = read_export(export)
    require(row.get("acceptance_protocol") and row.get("closure_binding"),
            "full-round verification needs the acceptance bindings in a new round export")
    roots = [path.resolve(strict=True) for path in roots]
    require(roots and all(root.is_dir() and root != Path("/") for root in roots), "invalid source roots")
    output = output.resolve()
    require(not output.exists(), "bundle destination already exists")
    require(all(not output.is_relative_to(root) for root in roots), "put the bundle outside the input roots")
    aliases = {}
    for assessment in row["evidence"]["assessments"]:
        for run in assessment["runs"]:
            root = absolute(run["path"])
            require(any(root.is_relative_to(base) for base in roots) and root.resolve(strict=True) == root,
                    "run outside source roots or noncanonical run path")
            source = root / "manifest.json"
            require(source.resolve(strict=True) == source and ev.file_hash(source) == run["source_hashes"][source.name],
                    "registered run manifest changed")
            recorded = absolute(ev.read_object(source)["client_arguments"]["run_dir"])
            require(recorded.resolve(strict=True) == root, "run directory does not resolve to its registered slot")
            if recorded != root:
                aliases[str(recorded)] = str(root)
    files = ArchivedFiles(output, aliases)
    output.mkdir(parents=True)
    seen = {}
    # Copy once per source and verify the copied bytes, including on the pack host.
    for ref in reference_paths(row, files):
        source = absolute(ref["path"])
        require(source == Path(sys.executable).resolve() or any(source.is_relative_to(root) for root in roots),
                "evidence outside supplied roots: " + str(source))
        require(source.resolve(strict=True) == source and source.is_file(), "expected regular evidence: " + str(source))
        require(seen.setdefault(source, ref["sha256"]) == ref["sha256"], "contradictory source hashes")
        dest = files.destination(source)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            require(ev.file_hash(dest) == ref["sha256"], "registered evidence changed: " + str(source))
    exported = output / "payload" / "round-export.json"
    shutil.copyfile(export, exported)
    require(read_export(exported) == row, "round export changed during packing")
    manifest = {"schema_version": 2, "kind": "closed_round", "verifier": bundles.assessor_identity(),
                "aliases": aliases, "files_sha256": bundles.payload_files(output), "result": audit(row, files)}
    (output / "bundle.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                                         encoding="utf-8")
    return manifest["result"]


def checked(ref, locate):
    path = locate(ref["path"])
    require(ev.file_hash(path) == ref["sha256"], "changed round evidence: " + str(ref["path"]))
    return path


def release_status(row, locate):
    release = row.get("site_release", {})
    require(isinstance(release, dict), "malformed site release")
    status = release.get("status", "unverified")
    if status == "unverified":
        require("site_release" not in row, "malformed site release")
        require(not row.get("acceptance_protocol", {}).get("require_site_release"),
                "required site release record is missing")
        return status
    require(status in ("pending", "released"), "unknown site release status")
    action = release["action_id"]
    directory = Path(row["final_deployment"]["observation"]["path"]).parent
    container = ev.read_object(checked(release["container"], locate))
    require(directory.name == action and directory.parent.name == "deployments"
            and Path(release["container"]["path"]) == directory / "container.json"
            and Path(row["last_health"]["path"]).parent == directory
            and container["label"] == action and row["final_deployment"]["instance_id"].startswith(container["id"] + ":")
            and release["round"] == row["round"], "closure action and container differ")
    if status == "pending":
        claim = release.get("claim")
        require(isinstance(claim, dict) and set(claim) == {"host", "boot_id", "devices", "start_mono_ns"},
                "missing or malformed pending cleanup claim")
        device_budget.label(claim["host"], "host")
        device_budget.label(claim["boot_id"], "boot ID")
        device_budget.devices_of(claim["devices"])
        device_budget.tick(claim["start_mono_ns"])
        return status
    ref = release["proof"]
    proof = ev.read_object(checked(ref, locate))
    campaign.verify_closure_release(release, release["lease"], proof, ref, directory)
    return "released"


def verify_profiles(row, locate, register):
    evidence = row["evidence"]
    protocol = row["acceptance_protocol"]
    seen_traces = set()
    for side, saved in evidence["profiles"].items():
        require(side in ("baseline", "candidate"), "unexpected profile side")
        path = locate(saved["path"])
        report = verify_bundle(path)
        require(report["valid"] and report["profile_policy"] == row["profile_policy"]
                and report["run_binding_status"] in ("trace_field_verified", "fresh_command_outputs")
                and report["run_id"] == saved["run_id"] and report["scenario"] == row["scenario"]
                and all(saved[k] == report[k] for k in ("manifest_sha256", "spec_sha256")),
                "diagnostic profile does not match round")
        diag = path / "diagnostic-run"
        manifest = ev.read_object(diag / "manifest.json")
        binding = saved["binding"]
        require(binding
                and ev.read_object(diag / "metadata.json")["campaign_binding"] == binding,
                "diagnostic phase binding mismatch")
        require(ev.identity_of(manifest) == row[side] and ev.case_of(manifest) == protocol["cases"][row["scenario"]]
                and manifest["base_url"] == protocol["endpoint"]["base_url"], "diagnostic case mismatch")
        ev.verify_deployment(diag, manifest, row[side], {}, locate)
        register(saved["run_id"], report["diagnostic_source_hashes"]["requests.jsonl"], binding)
        traces = {h for name, h in ev.read_object(path / "manifest.json")["files"].items() if name.startswith("raw/rank-")}
        require(not traces.intersection(seen_traces), "reused trace in two diagnostic phases")
        seen_traces.update(traces)
        if saved.get("step_analysis"):
            analysis = ev.read_object(checked(saved["step_analysis"], locate))
            for ref in analysis["sources"] + analysis["analysis_inputs"] + analysis["benchmark_sources"]:
                checked(ref, locate)
            if analysis.get("timeline"):
                for ref in analysis["timeline"]["sources"]:
                    checked(ref, locate)
    if evidence.get("step_comparison"):
        checked(evidence["step_comparison"], locate)


def verify_stages(row, locate, register, seen_bindings):
    from scripts.evidence_bundle import stable_result

    protocol = row["acceptance_protocol"]
    evidence = row["evidence"]
    contract = checked(protocol["contract"], locate)
    stages = []
    discoveries = set()
    screen_plan = None
    confirmation_order = "not_applicable"
    for saved in evidence["assessments"]:
        stage = "confirm" if stages else "assess"
        reg = saved["registration"]
        require(reg["stage"] == stage and len(stages) < 2 and reg["binding"] and reg["binding"] not in seen_bindings
                and (not stages or stages == ["adopt"] and not protocol["single"]), "invalid formal stage sequence")
        seen_bindings.add(reg["binding"])
        require(set(evidence["profiles"]) == {"baseline", "candidate"}, "missing diagnostic phase")
        pairs = checked(reg["pairs"], locate)
        actual = ev.assess(contract, pairs, row["baseline"], row["candidate"], locate=locate)
        require(actual["case_identity"] == protocol["cases"] and stable_result(actual) == stable_result(saved),
                "formal stage replay differs from the registered assessment")
        for run in actual["runs"]:
            path = Path(run["path"])
            manifest = ev.read_object(path / "manifest.json")
            meta = ev.read_object(path / "metadata.json")
            require(meta["campaign_binding"] == reg["binding"]
                    and path == locate(meta["campaign_run_dir"]) == locate(manifest["client_arguments"]["run_dir"])
                    and manifest["base_url"] == protocol["endpoint"]["base_url"], "formal run binding mismatch")
            register(run["run_id"], run["source_hashes"]["requests.jsonl"])
        recorded = saved.get("execution")
        execution = None
        require(recorded or not protocol["require_trial_plan"], "missing registered trial plan")
        if recorded:
            plan_path = checked(recorded["plan"], locate)
            execution = trial_plan.verify_execution(plan_path, actual, locate=locate)
            require(execution == {k: v for k, v in recorded.items() if k != "plan"}, "trial execution mismatch")
            require(execution["stage"] == ("confirm" if stages else "screen")
                    and (execution["calibration_sha256"] or not protocol["require_trial_plan"]),
                    "incorrect trial plan stage or missing calibration")
            if stages:
                require(not discoveries.intersection(slot["instance_id"] for slot in execution["slots"]),
                        "reused discovery deployment in confirmation")
                if screen_plan and screen_plan["calibration_sha256"]:
                    require(screen_plan["calibration_sha256"] == execution["calibration_sha256"]
                            and screen_plan["future_confirm_blocks"] == len(execution["slots"]) // (2 * len(protocol["cases"])),
                            "confirmation differs from pre-registered plan")
            else:
                screen_plan = execution
        if stages:
            confirmation_order = trial_plan.confirmation_order(
                screen_plan, execution, required=protocol.get("require_confirmation_clock", False))
            require(saved.get("confirmation_order", confirmation_order) == confirmation_order,
                    "confirmation clock verification differs from its registered result")
        else:
            discoveries = {r["instance_id"] for r in actual["runs"]}
        stages.append(actual["recommendation"])

    return stages, confirmation_order


def audit(row, locate):
    require(row.get("phase") == "closed", "round is not closed")
    protocol = row["acceptance_protocol"]
    for ref in row["evidence"]["evidence_files"]:
        checked(ref, locate)
    seen_runs, seen_requests, seen_bindings = set(), set(), set()

    def register(run_id, digest, binding=None):
        require(run_id not in seen_runs and digest not in seen_requests, "reused run evidence")
        seen_runs.add(run_id)
        seen_requests.add(digest)
        if binding is not None:
            require(binding not in seen_bindings, "reused diagnostic phase binding")
            seen_bindings.add(binding)

    verify_profiles(row, locate, register)
    stages, confirmation_order = verify_stages(row, locate, register, seen_bindings)
    decision = row["decision"]
    require(decision in ("adopt", "abandon", "revert"), "unknown closure decision")
    if decision == "adopt":
        require(stages == ["adopt"] * (1 if protocol["single"] else 2), "adoption lacks required passing stages")
    released = verify_closure(row, locate)
    closure = "awaiting_site_release" if protocol.get("require_site_release") and released == "pending" else "verified"
    return {"campaign_id": row["campaign_id"], "round": row["round"], "decision": decision,
            "comparison": "recomputed" if stages else "not_measured", "stage_recommendations": stages,
            "confirmation_order": confirmation_order, "invalid_attempts": retry_evidence.review(row, locate),
            "round_closure": closure, "final_health": "verified", "device_release": released}


def verify_closure(row, locate):
    require(row.get("phase") == "closed", "round is not closed")
    decision = row["decision"]
    require(decision in ("adopt", "abandon", "revert"), "unknown closure decision")
    if decision == "revert":
        require(row["candidate"] is not None, "no candidate to revert")
    selected = row["candidate"] if decision == "adopt" else row["baseline"]
    final = row["final_deployment"]
    require(final["identity"] == selected and final["observation"] == row["last_probe"], "wrong final deployment")
    deploy_path = checked(final["observation"], locate)
    obs = ev.check_observation(deploy_path, selected, **row["acceptance_protocol"]["endpoint"])
    require(obs["instance_id"] == final["instance_id"], "wrong final instance")
    campaign.check_closure_model(row, obs)
    receipt = ev.read_object(checked(row["last_health"], locate))
    campaign.verify_health(receipt, row, {**row, **row["closure_binding"]},
                           deploy_path, obs, original_ref=final["observation"], latest=row["closed_at"])
    return release_status(row, locate)

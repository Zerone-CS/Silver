"""Freeze a retired formal batch and review exclusion evidence after relocation."""

import json
import shutil
import stat
from pathlib import Path
from tempfile import TemporaryDirectory

from evaluate.scripts import validate_run
from scripts import round_evidence as ev

CORE = ("manifest.json", "metadata.json", "requests.jsonl", "workload.json")
require = ev.require


def slots(schedule, pairs):
    parent = Path(schedule["path"]).parent
    return [str(parent / pair[side]) for pair in pairs["pairs"] for side in ("baseline", "candidate")]


def inventory(root):
    """Never follow links in a partial run. A missing slot is a valid observation."""
    regular, unavailable = {}, {}

    def scan(path, name):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                scan(child, (name + "/" if name else "") + child.name)
        elif stat.S_ISREG(info.st_mode) and name:
            regular[name] = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        else:
            unavailable[name or "."] = "symlink" if stat.S_ISLNK(info.st_mode) else "not_a_regular_file"

    if root.exists() or root.is_symlink():
        scan(root, "")
    return regular, unavailable


def expected_files(root, present):
    expected = set(CORE)
    if "workload.json" in present:
        try:
            jobs, _ = ev.compare_trials.read_json(root / "workload.json")
        except (ValueError, UnicodeError):
            return expected
        if isinstance(jobs, list):
            for index in range(len(jobs)):
                expected.update("raw/%08d.%s" % (index, suffix) for suffix in ("request.json", "sse", "output.json"))
    return expected


def capture(campaign, attempt, pairs):
    store = Path(campaign).resolve() / "retry-evidence"
    store.mkdir(exist_ok=True)
    require(not store.is_symlink(), "retry evidence store cannot be a link")
    info = {"schema_version": 1, "schedule": attempt["schedule"], "binding": attempt["binding"],
            "stage": attempt["stage"], "error_type": attempt["error_type"], "slots": []}
    declared = slots(attempt["schedule"], pairs)
    with TemporaryDirectory(prefix=".capturing-", dir=store) as scratch:
        staged = Path(scratch) / "batch"
        staged.mkdir()
        for i, original in enumerate(declared):
            # Paths reserved by schedule() were canonicalized before collection.
            # Resolve the parent only, so a substituted slot symlink is observed.
            original_path = Path(original)
            path = original_path.parent.resolve() / original_path.name
            require(str(path) in attempt["paths"] and not store.is_relative_to(path),
                    "retired slot differs from its reservation")
            before, unavailable = inventory(path)
            dest = staged / str(i)
            hashes = {}
            for name in sorted(before):
                target = dest / name
                target.parent.mkdir(parents=True, exist_ok=True)
                require((path / name).resolve(strict=True) == path / name, "retired artifact now traverses a link")
                shutil.copyfile(path / name, target)
                hashes[name] = ev.file_hash(target)
            require(inventory(path) == (before, unavailable), "retired slot changed during capture: " + str(path))
            info["slots"].append({"declared": original, "path": str(path), "files": hashes,
                                  "missing": sorted(expected_files(dest, hashes) - hashes.keys()),
                                  "unavailable": unavailable})
        plan = attempt.get("plan")
        if plan is not None:
            info["registered_plan"] = plan
            src = Path(plan["path"])
            if src.is_file() and not src.is_symlink():
                shutil.copyfile(src, staged / "registered-plan.json")
                info["observed_plan_sha256"] = ev.file_hash(staged / "registered-plan.json")
        info["finding"] = classify(staged, info)
        receipt = staged / "inventory.json"
        receipt.write_text(json.dumps(info, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        digest = ev.file_hash(receipt)
        final = store / digest
        if not final.exists():
            staged.rename(final)
        # The DB points to a fully published snapshot. An interrupted transition can reuse it.
        ref = {"path": str(final / receipt.name), "sha256": digest}
        verify(attempt, ref, ev.local_path, pairs)
        return ref


def classify(root, info):
    """Record a sufficient local defect separately from the original CLI exception."""
    incomplete = None
    for i, slot in enumerate(info["slots"]):
        path = root / str(i)
        try:
            if "metadata.json" in slot["files"]:
                meta = ev.read_object(path / "metadata.json")
                if meta.get("campaign_run_dir") != slot["path"] or meta.get("campaign_binding") != info["binding"]:
                    return {"verification": "reproduced", "criterion": "registration_mismatch", "slot": i}
            if slot["missing"]:
                if incomplete is None:
                    incomplete = {"verification": "recorded_missing", "criterion": "missing_run_evidence", "slot": i}
                continue
            checked = validate_run.inspect_run(path)
            if not checked["valid"]:
                detail = sorted(msg.replace(str(path), "<slot>") for msg in checked["errors"])[0]
                return {"verification": "reproduced", "criterion": "invalid_run", "slot": i, "detail": detail}
            # Reuse the formal ledger and raw stream checks on the frozen bytes.
            ev.diagnostic_stream_facts(path, checked, {})
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            return {"verification": "reproduced", "criterion": "invalid_run_evidence", "slot": i,
                    "detail": str(exc).replace(str(path), "<slot>")}
    if info.get("registered_plan"):
        observed = info.get("observed_plan_sha256")
        if observed is None:
            return {"verification": "recorded_missing", "criterion": "missing_registered_plan"}
        if observed != info["registered_plan"]["sha256"]:
            return {"verification": "reproduced", "criterion": "registered_plan_changed"}
    return incomplete or {"verification": "unverified", "criterion": "no_independent_run_defect"}


def files(ref, read):
    """Enumerate the registered snapshot copies, never external paths found in them."""
    info = ev.read_object(read(ref["path"]))
    root = Path(ref["path"]).parent
    for index, slot in enumerate(info["slots"]):
        for name, digest in slot["files"].items():
            relative = Path(name)
            require(name == relative.as_posix() and name != "." and not relative.is_absolute()
                    and ".." not in relative.parts, "invalid retired run artifact name")
            yield {"path": str(root / str(index) / relative), "sha256": digest}
    if info.get("observed_plan_sha256") is not None:
        yield {"path": str(root / "registered-plan.json"), "sha256": info["observed_plan_sha256"]}


def verify(attempt, ref, locate, pairs):
    path = locate(ref["path"])
    require(ev.file_hash(path) == ref["sha256"], "retry evidence inventory changed")
    info = ev.read_object(path)
    require(info["schema_version"] == 1 and info["schedule"] == attempt["schedule"]
            and info["stage"] == attempt["stage"] and info["binding"] == attempt["binding"]
            and info["error_type"] == attempt["error_type"] and info.get("registered_plan") == attempt.get("plan")
            and [s["declared"] for s in info["slots"]] == slots(attempt["schedule"], pairs)
            and sorted(s["path"] for s in info["slots"]) == attempt["paths"],
            "retry inventory differs from the retired registration")
    for source in files(ref, locate):
        require(ev.file_hash(locate(source["path"])) == source["sha256"], "retired run artifact changed")
    missing = {}
    for i, slot in enumerate(info["slots"]):
        folder = path.parent / str(i)
        present, unsupported = inventory(folder)
        require(set(present) == set(slot["files"]) and not unsupported
                and slot["missing"] == sorted(expected_files(folder, present) - present.keys()),
                "retry slot inventory or missing evidence differs")
        if slot["missing"]:
            missing[slot["path"]] = slot["missing"]
    actual = classify(path.parent, info)
    require(actual == info["finding"], "replayed exclusion differs from frozen retry finding")
    return {"stage": attempt["stage"], "reason": attempt["reason"], "error": attempt["error"],
            "error_type": attempt["error_type"], **actual, "missing": missing}


def review(row, locate=ev.local_path):
    attempts = row["evidence"].get("invalid_attempts", [])
    requires_snapshot = row.get("acceptance_protocol", {}).get("require_retry_snapshot", False)
    if requires_snapshot:
        recorded = []
        for ref in row["evidence"].get("evidence_files", []):
            path = Path(ref["path"])
            if (path.name == "inventory.json" and path.parent.name == ref["sha256"]
                    and path.parent.parent.name == "retry-evidence"):
                recorded.append(ref)
        require(recorded == [attempt.get("snapshot") for attempt in attempts],
                "retired batches differ from the required evidence snapshot list")
    if not attempts:
        return []
    reports = []
    stages = row["evidence"].get("assessments", [])
    used_paths = {r["path"] for stage in stages for r in stage["runs"]}
    bindings = {stage["registration"]["binding"] for stage in stages if stage.get("registration")}
    for attempt in attempts:
        retired = set(attempt["paths"])
        require(not retired.intersection(used_paths), "retired slots reused in a different batch")
        used_paths.update(retired)
        ref = attempt.get("snapshot")
        if not ref:
            require(not requires_snapshot, "retired batch lacks its required evidence snapshot")
            reports.append({"verification": "unverified", "criterion": "legacy_record_only",
                            "reason": attempt["reason"], "error": attempt["error"]})
            continue
        require(attempt["stage"] in ("assess", "confirm") and bool(attempt["binding"])
                and attempt["binding"] not in bindings, "invalid or reused retry phase registration")
        bindings.add(attempt["binding"])
        schedule = attempt["schedule"]
        pairs = locate(schedule["path"])
        require(ev.file_hash(pairs) == schedule["sha256"], "retired batch registration changed")
        reports.append(verify(attempt, ref, locate, ev.read_object(pairs)))
    return reports

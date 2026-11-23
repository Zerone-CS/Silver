#!/usr/bin/env python3
"""Capture or import Chrome trace exports and independently replay their coverage checks."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import zlib
from decimal import Decimal, InvalidOperation

if __name__ == "__main__":
    sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evaluate.scripts.validate_run import inspect_run, strict_object


RUN_FILES = ("manifest.json", "metadata.json", "workload.json", "requests.jsonl")
IDENTITY = ("run_id", "scenario", "environment_sha256", "config_sha256", "workload_sha256",
            "metric_contract_sha256", "benchmark_sha256", "client_measurement_policy_sha256",
            "implementation_sha256")
READ_ERRORS = (OSError, ValueError, TypeError, EOFError, zlib.error, ArithmeticError)
CAPTURE_ERRORS = READ_ERRORS + (subprocess.TimeoutExpired,)


def check(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=strict_object)


def write_json(path, data):
    tmp = path.with_suffix(".partial")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def retain_file(source, target):
    with source.open("rb") as src:
        before = os.fstat(src.fileno())
        check(stat.S_ISREG(before.st_mode), "expected a regular export: " + str(source))
        with target.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        after = os.fstat(src.fileno())
        check((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
              "export changed while copying: " + str(source))


def number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid timestamp or duration: " + repr(value)) from None
    check(result.is_finite(), "timestamp or duration must be finite")
    return result


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def keys(value, expected, context):
    check(isinstance(value, dict) and value.keys() == set(expected),
          context + " requires fields: " + ", ".join(expected))


def profile_policy(spec):
    """Return the predeclared rules shared across comparable baseline/candidate runs."""
    required = ("schema_version", "format", "tool", "diagnostic_run", "traces", "rank_field",
                "window", "requirements")
    if isinstance(spec, dict) and "run_binding" in spec:
        required += ("run_binding",)
    keys(spec, required, "spec")
    check(type(spec["schema_version"]) is int and spec["schema_version"] == 1 and
          spec["format"] == "chrome-trace-v1", "unsupported profile spec or format")
    tool = spec["tool"]
    tool_fields = ("name", "version")
    if isinstance(tool, dict) and "version_evidence" in tool:
        tool_fields += ("version_evidence",)
    keys(tool, tool_fields, "tool")
    check(all(nonempty(v) for v in tool.values()), "tool name, version and optional evidence path must be nonempty")
    tool_policy = {field: tool[field] for field in ("name", "version")}
    if "version_evidence" in tool:
        tool_policy["version_evidence"] = "archived_text"
    check(nonempty(spec["diagnostic_run"]), "diagnostic_run must be a path")
    traces = spec["traces"]
    check(isinstance(traces, dict) and bool(traces) and all(
        re.fullmatch(r"0|[1-9][0-9]*", r) and nonempty(p) for r, p in traces.items()),
        "traces must map numeric rank IDs to export paths")
    check(len(set(traces.values())) == len(traces), "duplicate source paths across ranks")
    rank = spec["rank_field"]
    keys(rank, ("scope", "path"), "rank_field")
    check(rank["scope"] in ("event", "document", "ascend-profiler-info") and nonempty(rank["path"]),
          "rank_field requires an event or document field path")
    if rank["scope"] == "ascend-profiler-info":
        check(rank["path"] == "profiler_info_{rank}.json", "use the native profiler_info_{rank}.json name")
    window = spec["window"]
    check(isinstance(window, dict) and nonempty(window.get("clock")), "window.clock is required")
    policy_window = {"clock": window["clock"]}
    if window.get("mode") == "trace_extent":
        keys(window, ("clock", "mode", "min_duration_us"), "trace extent")
        minimum = number(window["min_duration_us"])
        check(minimum > 0, "min_duration_us must be positive")
        policy_window.update(mode="trace_extent", min_duration_us=str(minimum.normalize()))
    elif "start_us" in window:
        keys(window, ("clock", "start_us", "end_us"), "numeric window (microseconds)")
        check(number(window["end_us"]) > number(window["start_us"]), "window must increase")
        policy_window["mode"] = "timestamps"
        policy_window["duration_us"] = str((number(window["end_us"]) - number(window["start_us"])).normalize())
    else:
        keys(window, ("clock", "start_event", "end_event"), "marker window")
        check(all(nonempty(window[k]) for k in ("start_event", "end_event")) and
              window["start_event"] != window["end_event"], "use two distinct marker names")
        policy_window.update(mode="markers", start_event=window["start_event"], end_event=window["end_event"])
    requirements = spec["requirements"]
    check(isinstance(requirements, list) and requirements, "requirements must be a nonempty array")
    labels = set()
    for rule in requirements:
        keys(rule, ("id", "field", "pattern", "min_count"), "requirement")
        check(all(nonempty(rule[k]) for k in ("id", "field", "pattern")) and
              type(rule["min_count"]) is int and rule["min_count"] > 0, "invalid requirement")
        check(rule["id"] not in labels, "duplicate requirement id")
        labels.add(rule["id"])
        try:
            re.compile(rule["pattern"])
        except re.error as exc:
            raise ValueError("invalid requirement pattern: " + str(exc)) from exc
    policy = {"schema_version": 1, "format": spec["format"], "tool": tool_policy,
              "ranks": sorted(traces, key=int), "rank_field": rank, "window": policy_window,
              "requirements": sorted(requirements, key=lambda item: item["id"])}
    if "run_binding" in spec:
        binding = spec["run_binding"]
        keys(binding, ("scope", "path", "source"), "run_binding")
        check(binding["scope"] in ("document", "event") and nonempty(binding["path"]) and
              binding["source"] in ("run_id", "profile_capture_id"), "invalid run_binding fields")
        policy["run_binding"] = binding
    return policy


def field(obj, path):
    for key in path.split("."):
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj


def inspect_trace(path, spec, rank, shared_window=None, binding_value=None):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        doc = json.load(handle, parse_float=Decimal)
    events = doc.get("traceEvents") if isinstance(doc, dict) else doc
    check(isinstance(events, list) and events, "missing traceEvents array or empty export")
    check(all(isinstance(e, dict) and nonempty(e.get("ph")) for e in events), "malformed trace event")
    rank_field = spec["rank_field"]
    if rank_field["scope"] == "document":
        observed = {str(field(doc, rank_field["path"]))}
    elif rank_field["scope"] == "event":
        observed = {str(v) for e in events if (v := field(e, rank_field["path"])) is not None}
    else:
        info = read_json(path.parent / ("profiler_info_" + rank + ".json"))
        check(isinstance(info, dict) and bool(info), "empty native profiler metadata")
        observed = {str(info.get("rank_id", rank))}
    check(observed == {rank}, "expected rank %s; observed %s" % (rank, sorted(observed)))
    if "run_binding" in spec:
        binding = spec["run_binding"]
        values = [field(doc, binding["path"])] if binding["scope"] == "document" else (
            field(event, binding["path"]) for event in events)
        check({str(v) for v in values if v is not None} == {binding_value},
              "trace association does not match the diagnostic benchmark")
    timed = []
    for event in events:
        if event["ph"] == "M":
            continue
        start = number(event.get("ts"))
        duration = number(event.get("dur")) if event["ph"] == "X" else Decimal(0)
        check(duration >= 0, "negative event duration")
        timed.append((event, start, start + duration))
    check(timed, "no timestamped non-metadata events")
    window = spec["window"]
    if window.get("mode") == "trace_extent":
        left = min(start for _, start, _ in timed)
        right = max(end for _, _, end in timed)
    elif "start_us" in window:
        left, right = number(window["start_us"]), number(window["end_us"])
    else:
        def marker(key):
            stamps = [t for e, t, _ in timed if e.get("name") == window[key]]
            check(len(stamps) == 1, "expected one " + window[key] + " window marker")
            return stamps[0]
        left, right = marker("start_event"), marker("end_event")
    check(right > left, "window markers out of order")
    check(min(start for _, start, _ in timed) <= left and max(end for _, _, end in timed) >= right,
          "trace does not bracket the requested window")
    if shared_window is not None:
        left, right = shared_window
    counts = {}
    for rule in spec["requirements"]:
        matches = re.compile(rule["pattern"])
        count = 0
        for event, start, end in timed:
            if event["ph"] not in ("X", "i", "I", "C") or not (left <= start < right and end <= right):
                continue
            value = field(event, rule["field"])
            if value is not None and not isinstance(value, (dict, list)) and matches.search(str(value)):
                count += 1
        counts[rule["id"]] = count
    return {"rank": rank, "window_start_us": str(left), "window_end_us": str(right),
            "event_counts": counts, "total_events": len(events)}


def trace_path(spec, rank):
    return "raw/rank-" + rank + (".json.gz" if spec["traces"][rank].endswith(".gz") else ".json")


def expected_files(spec, mode):
    names = [trace_path(spec, rank) for rank in spec["traces"]]
    if spec["rank_field"]["scope"] == "ascend-profiler-info":
        names.extend("raw/profiler_info_" + rank + ".json" for rank in spec["traces"])
    names.extend("diagnostic-run/" + name for name in RUN_FILES)
    if "version_evidence" in spec["tool"]:
        names.append("raw/tool-version.txt")
    if mode == "command":
        names.extend(("stdout.log", "stderr.log"))
    return names


def verify_bundle(bundle):
    """Recompute a bundle from archived inputs. Never trust a stored profile-report.json."""
    bundle = Path(bundle)
    errors = []
    report = {"schema_version": 1, "valid": False, "errors": errors, "coverage": {},
              **dict.fromkeys(IDENTITY)}
    try:
        spec_path, manifest_path = bundle / "spec.json", bundle / "manifest.json"
        report["spec_sha256"] = file_hash(spec_path)
        report["manifest_sha256"] = file_hash(manifest_path)
        spec, manifest = read_json(spec_path), read_json(manifest_path)
        policy = profile_policy(spec)
        report["profile_policy"] = policy
        policy_bytes = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        report["profile_policy_sha256"] = hashlib.sha256(policy_bytes).hexdigest()
        check(isinstance(manifest, dict) and manifest.get("schema_version") == 1, "invalid capture manifest")
        check(manifest.get("spec_sha256") == report["spec_sha256"], "spec hash mismatch")
        check(manifest.get("status") == "complete", "capture incomplete or failed: " + str(manifest.get("errors")))
        mode = manifest.get("mode")
        check(mode in ("import", "command"), "unsupported capture mode")
        if mode == "command":
            check(type(manifest.get("exit_code")) is int and manifest["exit_code"] == 0 and
                  isinstance(manifest.get("argv"), list) and manifest["argv"], "capture command failed")
        artifacts = manifest.get("files")
        keys(artifacts, expected_files(spec, mode), "manifest.files")
        for filename, digest in artifacts.items():
            check(file_hash(bundle / filename) == digest, "artifact hash mismatch: " + filename)
        if "version_evidence" in spec["tool"]:
            version = spec["tool"]["version"]
            with (bundle / "raw/tool-version.txt").open(encoding="utf-8") as handle:
                evidence = handle.read(64 * 1024 + 1)
            check(len(evidence) <= 64 * 1024, "tool version evidence exceeds 64 KiB")
            pattern = r"(?<![\w.])" + re.escape(version) + r"(?![\w.])"
            check(re.search(pattern, evidence) is not None, "tool version evidence disagrees with the declared version")
            report["tool_version_evidence_sha256"] = artifacts["raw/tool-version.txt"]
        rank_hashes = {artifacts[trace_path(spec, rank)] for rank in policy["ranks"]}
        check(len(rank_hashes) == len(policy["ranks"]), "identical raw export reused for multiple ranks")
        diagnostic = inspect_run(bundle / "diagnostic-run")
        check(diagnostic["valid"], "invalid diagnostic run: " + str(diagnostic["errors"]))
        identity = diagnostic["manifest"]
        check(identity["run_purpose"] == "diagnostic" and identity["profiler_enabled"],
              "benchmark manifest must declare diagnostic run with profiler enabled")
        report.update({key: identity.get(key) for key in IDENTITY})
        report["base_url"] = identity.get("base_url")
        report["model"] = identity["client_measurement_policy"].get("model")
        report["diagnostic_source_hashes"] = diagnostic["source_hashes"]
        binding_value = None
        report["run_binding_status"] = "operator_paired" if mode == "import" else "fresh_command_outputs"
        if "run_binding" in spec:
            source = spec["run_binding"]["source"]
            binding_value = (identity["run_id"] if source == "run_id" else
                             read_json(bundle / "diagnostic-run" / "metadata.json").get(source))
            check(nonempty(binding_value), "diagnostic run lacks " + source)
            report["run_binding_status"] = "pending"
        for rank in policy["ranks"]:
            try:
                coverage = inspect_trace(bundle / trace_path(spec, rank), spec, rank, binding_value=binding_value)
                report["coverage"][rank] = coverage
            except READ_ERRORS as exc:
                errors.append("rank " + rank + ": " + str(exc))
        if not errors:
            intervals = {rank: (number(item["window_start_us"]), number(item["window_end_us"]))
                         for rank, item in report["coverage"].items()}
            shared = max(v[0] for v in intervals.values()), min(v[1] for v in intervals.values())
            check(shared[0] < shared[1], "no common rank window in the declared clock")
            if policy["window"]["mode"] == "trace_extent":
                check(shared[1] - shared[0] >= number(policy["window"]["min_duration_us"]),
                      "common trace extent shorter than min_duration_us")
            for rank, interval in intervals.items():
                if interval != shared:
                    report["coverage"][rank] = inspect_trace(bundle / trace_path(spec, rank), spec, rank,
                                                             shared, binding_value)
                for rule in spec["requirements"]:
                    count = report["coverage"][rank]["event_counts"][rule["id"]]
                    if count < rule["min_count"]:
                        errors.append("rank %s: %s has %d events; expected >= %d" %
                                      (rank, rule["id"], count, rule["min_count"]))
            if binding_value is not None:
                report["run_binding_status"] = "trace_field_verified"
    except READ_ERRORS as exc:
        errors.append(str(exc))
    report["valid"] = not errors
    return report


def run_command(argv, cwd, timeout, bundle):
    with (bundle / "stdout.log").open("xb") as out, (bundle / "stderr.log").open("xb") as err:
        with subprocess.Popen(argv, cwd=cwd, stdout=out, stderr=err, start_new_session=True) as proc:
            try:
                return proc.wait(timeout=timeout)
            except BaseException:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                raise


def capture(spec_path, bundle, argv=(), cwd=None, timeout=None):
    spec_path, bundle = Path(spec_path).resolve(), Path(bundle).resolve()
    spec_bytes = spec_path.read_bytes()
    spec = json.loads(spec_bytes, object_pairs_hook=strict_object)
    profile_policy(spec)
    check(timeout is None or (0 < timeout < float("inf")), "timeout must be positive and finite")
    bundle.mkdir(parents=True, exist_ok=False)
    (bundle / "spec.json").write_bytes(spec_bytes)
    (bundle / "raw").mkdir()
    (bundle / "diagnostic-run").mkdir()
    mode = "command" if argv else "import"
    manifest = {"schema_version": 1, "status": "started", "mode": mode, "argv": list(argv),
                "cwd": str(Path(cwd or os.getcwd()).resolve()), "started_ns": time.time_ns(),
                "spec_sha256": file_hash(bundle / "spec.json"), "files": {}, "errors": [], "exit_code": None}
    write_json(bundle / "manifest.json", manifest)
    diag = (spec_path.parent / spec["diagnostic_run"]).resolve()
    sources = {trace_path(spec, r): spec_path.parent / p for r, p in spec["traces"].items()}
    collect_sources = False
    try:
        check(len({p.resolve() for p in sources.values()}) == len(sources), "trace source paths alias each other")
        if spec["rank_field"]["scope"] == "ascend-profiler-info":
            for rank in spec["traces"]:
                export = sources[trace_path(spec, rank)]
                check(export.parent.name == "ASCEND_PROFILER_OUTPUT" and
                      export.name in ("trace_view.json", "trace_view.json.gz"),
                      "native rank check requires worker/ASCEND_PROFILER_OUTPUT/trace_view.json[.gz]")
                info = "profiler_info_" + rank + ".json"
                sources["raw/" + info] = export.parent.parent / info
        if "version_evidence" in spec["tool"]:
            sources["raw/tool-version.txt"] = spec_path.parent / spec["tool"]["version_evidence"]
        if argv:
            check(all(not os.path.lexists(p) for p in [diag, *sources.values()]),
                  "command capture requires fresh paths: old diagnostic run or trace already exists")
        collect_sources = True
        if argv:
            manifest["exit_code"] = run_command(argv, manifest["cwd"], timeout, bundle)
            check(manifest["exit_code"] == 0, "capture process exit code " + str(manifest["exit_code"]))
    except CAPTURE_ERRORS as exc:
        manifest["errors"].append(str(exc))
    sources.update({"diagnostic-run/" + name: diag / name for name in RUN_FILES})
    if collect_sources:
        for name, path in sources.items():
            try:
                check(path.is_file(), "missing export: " + str(path))
                retain_file(path, bundle / name)
            except READ_ERRORS as exc:
                manifest["errors"].append(str(exc))
    for name in expected_files(spec, mode):
        path = bundle / name
        if path.is_file():
            manifest["files"][name] = file_hash(path)
    manifest["status"] = "failed" if manifest["errors"] else "complete"
    manifest["finished_ns"] = time.time_ns()
    write_json(bundle / "manifest.json", manifest)
    report = verify_bundle(bundle)
    write_json(bundle / "profile-report.json", report)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("verify").add_argument("--bundle", required=True, type=Path)
    sub.add_parser("policy").add_argument("--spec", required=True, type=Path)
    cap = sub.add_parser("capture")
    cap.add_argument("--spec", required=True, type=Path)
    cap.add_argument("--out", required=True, type=Path)
    cap.add_argument("--cwd", type=Path)
    cap.add_argument("--timeout", type=float)
    cap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    try:
        if args.action == "policy":
            output = profile_policy(read_json(args.spec))
        elif args.action == "verify":
            output = verify_bundle(args.bundle)
        else:
            argv = args.command[1:] if args.command[:1] == ["--"] else args.command
            check(not args.command or argv, "missing command after --")
            output = capture(args.spec, args.out, argv, args.cwd, args.timeout)
    except READ_ERRORS as exc:
        output = {"valid": False, "errors": [str(exc)]}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output.get("valid", True) else 1


def interrupted(_signal, _frame):
    raise TimeoutError("profile capture was cancelled")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())

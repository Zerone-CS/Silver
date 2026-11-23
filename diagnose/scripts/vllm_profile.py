#!/usr/bin/env python3
"""Bracket a diagnostic benchmark with a live vLLM profiler and stage fresh Ascend exports."""
import argparse
import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

if __name__ == "__main__":
    sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from diagnose.scripts.profile import check, profile_policy, read_json, retain_file
from evaluate.scripts import benchmark


def control(endpoint, action, key, timeout):
    connection_type = http.client.HTTPSConnection if endpoint["scheme"] == "https" else http.client.HTTPConnection
    conn = connection_type(endpoint["host"], endpoint["port"], timeout=timeout)
    try:
        headers = {"Authorization": "Bearer " + key} if key else {}
        conn.request("POST", "/" + action, headers=headers)
        response = conn.getresponse()
        check(200 <= response.status < 300, "%s returned HTTP %d" % (action, response.status))
    finally:
        conn.close()


def worker_dirs(root):
    workers = set()
    def fail_scan(error):
        raise error
    for parent, directories, _ in os.walk(root, onerror=fail_scan):
        found = [d for d in directories if d.endswith("_ascend_pt")]
        workers.update(Path(parent) / d for d in found if not (Path(parent) / d).is_symlink())
        directories[:] = [d for d in directories if d not in found]
    return workers


def fresh_exports(root, old, expected):
    found = {}
    for worker in sorted(worker_dirs(root) - old):
        infos = list(worker.glob("profiler_info_*.json"))
        ranks = [(m.group(1), info) for info in infos
                 if (m := re.fullmatch(r"profiler_info_(0|[1-9][0-9]*)\.json", info.name))]
        check(len(ranks) <= 1, "multiple rank metadata files in one worker: " + str(worker))
        if not ranks or ranks[0][0] not in expected:
            continue
        rank, info = ranks[0]
        check(rank not in found, "ambiguous fresh worker directories for rank " + rank)
        folder = worker / "ASCEND_PROFILER_OUTPUT"
        traces = [folder / name for name in ("trace_view.json", "trace_view.json.gz")
                  if (folder / name).is_file()]
        check(len(traces) <= 1, "multiple trace exports for rank " + rank)
        found[rank] = None
        if traces:
            check(not any(p.is_symlink() for p in (info, folder, traces[0])), "symlink in worker export")
            found[rank] = (traces[0], info)
    return found


def wait_exports(root, old, expected, timeout, settle):
    deadline = time.monotonic() + timeout
    previous, stable_since = None, None
    while True:
        found = fresh_exports(root, old, expected)
        now = time.monotonic()
        if set(found) == expected and all(found.values()):
            signature = []
            for pair in found.values():
                for path in pair:
                    stat = path.stat()
                    signature.append((path, stat.st_size, stat.st_mtime_ns))
            if all(size > 0 for _, size, _ in signature):
                if signature != previous:
                    previous, stable_since = signature, now
                if now - stable_since >= settle:
                    return found
        else:
            previous = None
        if now >= deadline:
            missing = sorted(r for r in expected if not found.get(r))
            raise ValueError("fresh completed exports did not settle in time; missing ranks: " + str(missing))
        time.sleep(min(0.2, deadline - now))


def collect(args):
    spec_path = args.spec.resolve(strict=True)
    spec = read_json(spec_path)
    policy = profile_policy(spec)
    check(policy["rank_field"]["scope"] == "ascend-profiler-info", "use the native ascend-profiler-info rank policy")
    endpoint = benchmark.parse_endpoint(args.base_url)
    check(endpoint["path"] == "/v1/chat/completions", "supply the direct instance URL or its /v1 root")
    bench_dir = (spec_path.parent / spec["diagnostic_run"]).resolve()
    traces = {r: (spec_path.parent / p).absolute() for r, p in spec["traces"].items()}
    for r, path in traces.items():
        check(path.name in ("trace_view.json", "trace_view.json.gz") and path.parent.name == "ASCEND_PROFILER_OUTPUT",
              "use <worker>/ASCEND_PROFILER_OUTPUT/trace_view.json[.gz] destinations")
        info = path.parent.parent / ("profiler_info_" + r + ".json")
        check(not os.path.lexists(path) and not os.path.lexists(info), "destination already exists: " + str(path))
    check(not os.path.lexists(bench_dir), "diagnostic run directory already exists")
    check(len({p.parent.parent.resolve() for p in traces.values()}) == len(traces), "use distinct destination workers")
    argv = args.benchmark_args[1:] if args.benchmark_args[:1] == ["--"] else args.benchmark_args
    bench_argv = [*argv, "--base-url", args.base_url, "--run-dir", str(bench_dir), "--api-key-env", args.api_key_env]
    parsed = benchmark.parser().parse_args(bench_argv)
    metadata, _, _ = benchmark.read_inputs(parsed)
    check(metadata["profiler_enabled"] and metadata["run_purpose"] == "diagnostic",
          "benchmark metadata must declare profiler_enabled=true and run_purpose=diagnostic")
    check(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env), "invalid API key environment name")
    key = os.environ.get(args.api_key_env)
    check(not key or all(32 <= ord(ch) < 127 for ch in key), "invalid API credential characters")
    root = args.export_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    old = worker_dirs(root)
    try:
        control(endpoint, "start_profile", key, args.http_timeout)
    except (OSError, http.client.HTTPException):
        # The remote instance may have started profiling before its reply was lost.
        control(endpoint, "stop_profile", key, args.http_timeout)
        raise
    try:
        subprocess.run([sys.executable, "-B", str(ROOT / "evaluate/scripts/benchmark.py"), *bench_argv],
                       check=True, timeout=args.benchmark_timeout)
    finally:
        control(endpoint, "stop_profile", key, args.http_timeout)
    exports = wait_exports(root, old, set(traces), args.export_timeout, args.settle_seconds)
    sources = {}
    for rank, (source, info) in exports.items():
        dest = traces[rank]
        check(source.suffix == dest.suffix, "spec and source compression differ for rank " + rank)
        doc = read_json(info)
        check(isinstance(doc, dict) and doc and str(doc.get("rank_id", rank)) == rank,
              "empty or mismatched native rank metadata: " + str(info))
        dest.parent.mkdir(parents=True, exist_ok=True)
        retain_file(source, dest)
        retain_file(info, dest.parent.parent / info.name)
        sources[rank] = {"trace": str(source), "rank_info": str(info), "staged": str(dest)}
    return {"diagnostic_run": str(bench_dir), "sources": sources}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--export-root", required=True, type=Path, help="server's locally visible profiler output root")
    parser.add_argument("--base-url", required=True, help="direct vLLM instance URL, e.g. http://127.0.0.1:8000/v1")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--http-timeout", type=float, default=180)
    parser.add_argument("--benchmark-timeout", type=float, default=600)
    parser.add_argument("--export-timeout", type=float, default=180)
    parser.add_argument("--settle-seconds", type=float, default=1)
    parser.add_argument("benchmark_args", nargs=argparse.REMAINDER, help="-- --model ... --dataset ... --metadata ...")
    args = parser.parse_args()
    if any(not (math.isfinite(t) and t > 0) for t in (
            args.http_timeout, args.benchmark_timeout, args.export_timeout, args.settle_seconds)):
        parser.error("timeouts and settle-seconds must be positive finite seconds")
    if args.settle_seconds >= args.export_timeout:
        parser.error("settle-seconds must be less than export-timeout")
    try:
        print(json.dumps(collect(args), ensure_ascii=False, indent=2))
    except (ValueError, OSError, http.client.HTTPException, subprocess.SubprocessError) as exc:
        print("vllm_profile: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

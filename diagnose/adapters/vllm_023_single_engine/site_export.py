"""One isolated diagnostic workload per capture on a newly instrumented single-engine server."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ITERATION = re.compile(r"Iteration\((\d+)\):\s*\d+ context requests,\s*\d+ context tokens,\s*\d+ generation requests,\s*\d+ generation tokens, iteration elapsed time:")


def require(value, message):
    if not value:
        raise ValueError(message)


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_read(path):
    return json.loads(path.read_text())


def write(path, value):
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")


def check_id(value):
    require(re.fullmatch(r"[0-9A-Za-z_-]{1,120}", value), "invalid capture ID")
    return value


def new_csv(root, name, columns, rows):
    with (root / name).open("x", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(columns)
        writer.writerows(rows)


def snapshot(path, container):
    with path.open("xb") as out:
        subprocess.run(["docker", "logs", "--timestamps", container], stdout=out, stderr=subprocess.STDOUT, check=True)


def control(args):
    capture = check_id(args.capture_id)
    snap = args.root / capture
    gate = args.root / "active"
    container = subprocess.check_output(
        ["docker", "inspect", "--format", "{{.Id}}", args.container], text=True).strip()
    if args.action == "open":
        require(not gate.exists(), "another capture gate is open")
        snap.mkdir()
        snapshot(snap / "before.log", container)
        write(snap / "open.json", dict(capture_id=capture, container=container, utc=utc()))
        with gate.open("x") as handle:
            handle.write(capture + "\n")
    else:
        require(gate.read_text().strip() == capture, "attempt to close a different gate")
        require(json_read(snap / "open.json")["container"] == container, "container changed")
        gate.unlink()
        snapshot(snap / "after.log", container)
        write(snap / "close.json", dict(capture_id=capture, container=container, utc=utc()))


def native(path):
    result = []
    for line in path.read_text(errors="replace").splitlines():
        hit = ITERATION.search(line)
        if hit:
            require("(dummy)" not in line, "DP dummy iteration in this one-engine capture")
            result.append(int(hit.group(1)))
    require(result == list(range(len(result))), "missing/duplicated/truncated native iteration series: " + str(path))
    return result


EVIDENCE_FILES = ("engine.jsonl", "before.log", "after.log", "open.json", "close.json")


def prepare(args):
    run, root, out = args.run.resolve(strict=True), args.root.resolve(strict=True), args.output.resolve()
    suite = args.suite.resolve(strict=True)
    run_id = json_read(run / "manifest.json")["run_id"]
    capture = check_id(json_read(run / "metadata.json")["profile_capture_id"])
    require(not (root / "active").exists(), "capture gate still open")
    snap = root / capture
    logs = list(root.glob("engine-*.jsonl"))
    require(len(logs) == 1, "exactly one engine process required for this probe")
    out.mkdir()
    for name in EVIDENCE_FILES:
        source = logs[0] if name == "engine.jsonl" else snap / name
        (out / name).write_bytes(source.read_bytes())
    opened, closed = (json_read(out / n) for n in ("open.json", "close.json"))
    require(opened["capture_id"] == closed["capture_id"] == capture and opened["container"] == closed["container"],
            "invalid gate receipts")
    before, after = native(out / "before.log"), native(out / "after.log")
    require(after[:len(before)] == before and len(after) > len(before), "no independent iteration delta")
    counted = len(after) - len(before)
    schedules, completions, bridges, commits = {}, {}, {}, []
    finishes = []
    for n, line in enumerate((out / "engine.jsonl").read_bytes().splitlines(), 1):
        x = json.loads(line)
        if x["capture_id"] != capture:
            continue
        kind = x["kind"]
        if kind == "bridge":
            key = x["backend_id"]
            require(key and x["response_id"] and key not in bridges, "invalid or duplicate ID bridge")
            bridges[key] = (x["response_id"], n)
        elif kind == "schedule":
            step = x["step_id"]
            require(step not in schedules and sum(x["scheduled"].values()) > 0, "invalid scheduled step")
            schedules[step] = (x["scheduled"], n)
        elif kind == "complete":
            step = x["step_id"]
            require(step not in completions, "duplicate completed step")
            completions[step] = n
        elif kind == "commit":
            commits.append((x, n))
        elif kind == "finish":
            finishes.append((x, n))
        else:
            raise ValueError("unusable event at line %d: %r" % (n, x))
    require(set(schedules) == set(completions) and len(schedules) == counted,
            "completed logical inventory disagrees with independent native count (%d/%d/%d)"
            % (len(schedules), len(completions), counted))
    require(all(key in bridges for entries, _ in schedules.values() for key in entries),
            "schedule contains a request admitted outside this gate")
    for event, n in commits:
        step = event["step_id"]
        require(step in schedules and event["backend_id"] in schedules[step][0],
                "commit request was not scheduled in its step at engine line %d" % n)
        require(event["output_count"] > 0 and n > completions[step],
                "invalid output count or commit order at engine line %d" % n)
    finished_requests = set()
    for event, n in finishes:
        step, request = event["step_id"], event["backend_id"]
        require(step in completions and n > completions[step] and request in bridges and
                request not in finished_requests, "invalid finish at engine line %d" % n)
        finished_requests.add(request)
    for step, (scheduled, n) in schedules.items():
        require(completions[step] > n and all(bridges[key][1] < n for key in scheduled),
                "invalid admission/schedule/completion event order at engine line %d" % n)
    new_csv(out, "inventory.csv", ["capture", "step", "schedule_jsonl_line", "complete_jsonl_line"],
            [(capture, step, schedules[step][1], completions[step]) for step in schedules])
    new_csv(out, "bridges.csv", ["capture", "backend", "external", "jsonl_line"],
            [(capture, key, external, n) for key, (external, n) in bridges.items()])
    new_csv(out, "commits.csv", ["capture", "step", "backend", "start", "count", "jsonl_line"],
            [(capture, x["step_id"], x["backend_id"], x["output_start"], x["output_count"], n) for x,n in commits])
    new_csv(out, "seal.csv", ["capture", "expected_steps", "native_first", "native_last", "before_sha256", "after_sha256"],
            [(capture, counted, after[len(before)], after[-1], sha(out / "before.log"), sha(out / "after.log"))])
    subprocess.run([sys.executable, "-B", str(suite / "scripts/step_analysis.py"), "request-map", "--run", str(run),
                    "--output", str(out / "request-map.csv")], check=True)
    with (out / "request-map.csv").open() as f:
        exported = list(csv.DictReader(f))
    wanted = {row["response_id"] for row in exported}
    require(len(exported) == len(json_read(run / "workload.json")) == len(bridges)
            and wanted == {p[0] for p in bridges.values()},
            "isolated all-success test requires all and only measured SSE request IDs in the gate")
    owner = {"replica": "0", "owner_rank": "0"}
    # The engine sends committed output after update_from_output returns. The
    # last completed batch must contribute an output to establish that its
    # completion precedes the end of the measured client's wall-clock window.
    last_completion = max(completions, key=completions.get)
    last_in_window = any(event["step_id"] == last_completion for event, _ in commits)
    scope = "full_run" if last_in_window else "partial"
    mapping = {"scope": scope, "step_definition": "completed vLLM scheduler batch with total_num_scheduled_tokens > 0 per replica",
               "owner_ranks":{"0":"0"}, "tables": [
               {"kind":"inventory", "path":"inventory.csv", "constants":owner,
                "columns":{"capture_id":"capture", "step_id":"step"}},
               {"kind":"bridge", "path":"bridges.csv",
                "columns":{"capture_id":"capture","backend_id":"backend","response_id":"external"}},
               {"kind":"request_map","path":"request-map.csv",
                "columns":{k:k for k in ("run_id","manifest_sha256","request_id","response_id","sse_sha256")}},
               {"kind":"outputs","path":"commits.csv","constants":owner,
                "columns":{"capture_id":"capture","step_id":"step","backend_id":"backend",
                           "output_start":"start","output_count":"count"}},
               {"kind":"seal","path":"seal.csv","constants":{"replica":"0"},
                "columns":{"capture_id":"capture","expected_steps":"expected_steps"}}]}
    mapping["evidence_files"] = [{"path": name, "sha256": sha(out / name)} for name in EVIDENCE_FILES]
    write(out / "mapping.json", mapping)
    subprocess.run([sys.executable, "-B", str(suite / "diagnose/scripts/step_sources.py"), "prepare",
                    "--run-dir", str(run), "--mapping", str(out / "mapping.json"), "--output", str(out / "sources.json"),
                    "--tool-version", "vllm-0.23.0-observed-scheduler-v1"], check=True)
    report = out / "report.json"
    subprocess.run([sys.executable, "-B", str(suite / "scripts/step_analysis.py"), "run", "--run", str(run),
                    "--sources", str(out / "sources.json"), "--output", str(report)], check=True)
    ledger = json_read(report)
    require(ledger["coverage"]["unmapped_output_tokens"] == 0, "incomplete token accounting")
    expected_status, expected_k = ("complete", counted) if last_in_window else ("partial", None)
    require(ledger["coverage"]["status"] == expected_status and ledger["metrics"]["K"] == expected_k,
            "inconsistent logical step coverage")
    write(out / "provenance.json", {
        "capture_id":capture, "run_id":run_id, "container":opened["container"],
        "step_definition":mapping["step_definition"], "native_iteration_count":counted,
        "last_completed_step":last_completion, "final_completion_produced_client_output":last_in_window,
        "wall_window_binding":"The last observed completion delivered committed output" if last_in_window else
            "The final completed positive batch emitted no tokens; its position relative to client end is unknown",
        "sha256_by_file":{p.name:sha(p) for p in sorted(out.iterdir()) if p.is_file()},
        "native_count_method":"Distinct contiguous engine-native Iteration indices between gate log snapshots, excluding pre-gate iterations.",
        "timing":"Client T is the full benchmark wall. Engine monotonic stamps, when present, are local to the engine.",
        "scope":"One isolated all-success run; TP=DP=PP=1. No measured run shared with warm-up or other traffic."})
    print(json.dumps({k:ledger["metrics"][k] for k in ("N","T","K","R","Y_bar","output_token_throughput")}, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    cmd = p.add_subparsers(dest="action", required=True)
    for action in ("open","close"):
        one = cmd.add_parser(action)
        one.add_argument("--root", type=Path, required=True)
        one.add_argument("--container", required=True)
        one.add_argument("--capture-id", required=True)
    data = cmd.add_parser("prepare")
    for key in ("root", "output", "run", "suite"):
        data.add_argument("--" + key, type=Path, required=True)
    args = p.parse_args()
    try:
        (prepare if args.action == "prepare" else control)(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        p.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()

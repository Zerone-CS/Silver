#!/usr/bin/env python3
"""Recompute client stream phases and correlate optional server events by response ID."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from diagnose.scripts import profile, request_map
from evaluate.scripts import validate_run


TERMINAL_FIELDS = ("finish_reason_ts", "final_usage_ts", "stream_done_ts")


def duration(left, right):
    return right - left if left is not None and right is not None else None


def client_phases(request, limit):
    sent, first, last, end = (request[name] for name in
                              ("send_start_ts", "first_output_ts", "last_output_ts", "end_ts"))
    finish, usage, done = (request.get(name) for name in TERMINAL_FIELDS)
    stages = {
        "ttft": duration(sent, first),
        "first_to_last_output": duration(first, last),
        "last_output_to_finish": duration(last, finish),
        "finish_to_final_usage": duration(finish, usage),
        "final_usage_to_done": duration(usage, done),
        "done_to_request_end": duration(done, end),
        "last_output_to_request_end": duration(last, end),
        "e2e": duration(sent, end),
    }
    status = "legacy" if TERMINAL_FIELDS[0] not in request else (
        "complete" if None not in (finish, usage, done) else "partial")
    tail = stages["last_output_to_request_end"]
    return {"request_id": request["request_id"], "final_status": request["final_status"],
            "stream_timing": status, "client_seconds": stages,
            "long_tail": tail is not None and tail >= limit}


def engine_phases(directory, run, checked, response_to_request):
    receipt = profile.read_json(directory / "provenance.json")
    profile.check(receipt["run_id"] == checked["manifest"]["run_id"], "engine evidence run ID differs")
    capture = profile.read_json(run / "metadata.json").get("profile_capture_id")
    profile.check(profile.nonempty(capture) and receipt["capture_id"] == capture, "engine evidence capture ID differs")
    path = directory / "engine.jsonl"
    raw = path.read_bytes()
    raw_digest = hashlib.sha256(raw).hexdigest()
    profile.check(receipt["sha256_by_file"]["engine.jsonl"] == raw_digest, "engine evidence hash differs")
    clocks = {}
    response_ids = set()
    previous_stamp = None
    for n, line in enumerate(raw.splitlines(), 1):
        event = validate_run.parse_json(line.decode("utf-8"))
        if event["capture_id"] != capture:
            continue
        stamp = event.get("monotonic_ns")
        if stamp is not None:
            profile.check(type(stamp) is int and stamp >= 0 and (
                previous_stamp is None or stamp >= previous_stamp),
                "invalid engine timestamp at line " + str(n))
            previous_stamp = stamp
        kind = event["kind"]
        if kind == "bridge":
            backend_id, response_id = event["backend_id"], event["response_id"]
            profile.check(backend_id not in clocks and response_id not in response_ids and
                          response_id in response_to_request,
                          "engine bridge is mismatched or duplicated at line " + str(n))
            response_ids.add(response_id)
            clocks[backend_id] = (response_id, {"admitted": stamp})
        elif kind == "schedule":
            for backend_id in event["scheduled"]:
                clocks[backend_id][1].setdefault("first_scheduled", stamp)
        elif kind == "commit":
            _, phases = clocks[event["backend_id"]]
            phases.setdefault("first_output_committed", stamp)
            phases["last_output_committed"] = stamp
        elif kind == "finish":
            _, phases = clocks[event["backend_id"]]
            profile.check("finished" not in phases, "duplicate engine finish at line " + str(n))
            phases["finished"] = stamp
        elif kind != "complete":
            raise ValueError("unexpected engine event at line " + str(n))
    return {response_id: phases for response_id, phases in clocks.values()}, raw_digest


def api_phases(path, checked, response_to_request):
    raw = path.read_bytes()
    doc = validate_run.parse_json(raw.decode("utf-8"))
    expected = ("schema_version", "run_id", "manifest_sha256", "clock", "events")
    profile.keys(doc, expected, "API event evidence")
    profile.check(type(doc["schema_version"]) is int and doc["schema_version"] == 1 and
                  doc["run_id"] == checked["manifest"]["run_id"] and
                  doc["manifest_sha256"] == checked["source_hashes"]["manifest.json"],
                  "API event evidence does not match the benchmark")
    profile.check(profile.nonempty(doc["clock"]) and isinstance(doc["events"], list), "invalid API event evidence")
    frames = {}
    for event in doc["events"]:
        profile.keys(event, ("response_id", "last_output_written_ns", "done_written_ns"), "API event")
        response_id = event["response_id"]
        profile.check(isinstance(response_id, str) and response_id in response_to_request and response_id not in frames,
                      "API response ID is unknown or duplicated")
        last, done = (event[k] for k in ("last_output_written_ns", "done_written_ns"))
        profile.check(all(t is None or (type(t) is int and t >= 0) for t in (last, done)) and
                      (last is None or done is None or done >= last), "invalid API write timestamps")
        frames[response_id] = {"last_output_written": last, "done_written": done}
    return frames, doc["clock"], hashlib.sha256(raw).hexdigest()


def analyze(run, long_tail_s=1.0, engine_evidence=None, api_evidence=None):
    run = Path(run).resolve(strict=True)
    profile.check(math.isfinite(long_tail_s) and long_tail_s > 0, "long-tail threshold must be positive")
    checked = validate_run.inspect_run(run)
    profile.check(checked["valid"], "invalid benchmark run: " + str(checked["errors"]))
    requests = validate_run.read_verified_requests(run, checked)
    rows = [client_phases(row, long_tail_s) for row in requests]
    rows.sort(key=lambda row: (-row["client_seconds"]["e2e"], row["request_id"]))
    source_hashes = dict(checked["source_hashes"])
    server_clocks = {}
    if engine_evidence or api_evidence:
        mapping = list(csv.DictReader(request_map.request_map(run).splitlines()))
        profile.check(all(row["manifest_sha256"] == source_hashes["manifest.json"] for row in mapping),
                      "diagnostic manifest changed before request mapping")
        response_to_request = {row["response_id"]: row["request_id"] for row in mapping}
        request_to_response = {row["request_id"]: row["response_id"] for row in mapping}
        engine, api = {}, {}
        if engine_evidence:
            engine, source_hashes["engine.jsonl"] = engine_phases(
                Path(engine_evidence), run, checked, response_to_request)
            server_clocks["engine"] = "engine_local_monotonic_ns"
        if api_evidence:
            api, server_clocks["api"], source_hashes["api_events.json"] = api_phases(
                Path(api_evidence), checked, response_to_request)
        for row in rows:
            response = row["response_id"] = request_to_response.get(row["request_id"])
            row["engine_ns"] = engine.get(response)
            row["api_ns"] = api.get(response)
    return {
        "schema_version": 1, "valid": True,
        "run_id": checked["manifest"]["run_id"], "source_hashes": source_hashes,
        "measurement": {"wall_s": checked["metrics"]["wall_s"],
                        "e2e_uses": "send_start_ts to end_ts",
                        "client_clock": checked["manifest"]["clock"]},
        "server_clocks": server_clocks,
        "cross_clock_alignment": "identity_only",
        "long_tail_threshold_s": long_tail_s,
        "long_tail_requests": [row["request_id"] for row in rows if row["long_tail"]],
        "terminal_timestamp_coverage": {
            status: sum(row["stream_timing"] == status for row in rows)
            for status in ("complete", "partial", "legacy")
        },
        "requests": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--long-tail-s", type=float, default=1.0)
    parser.add_argument("--engine-evidence", type=Path, help="prepared single-engine evidence directory")
    parser.add_argument("--api-evidence", type=Path, help="optional JSON of server writes keyed by SSE response ID")
    args = parser.parse_args(argv)
    try:
        result = analyze(args.run, args.long_tail_s, args.engine_evidence, args.api_evidence)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

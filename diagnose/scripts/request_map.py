"""Recover client-request to API-response identities from retained benchmark streams."""
import csv
import io

from diagnose.scripts import profile, step_ledger
from evaluate.scripts import benchmark, compare_trials, validate_run
from evaluate.scripts.replay import verify_stream


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def read_diagnostic(directory):
    checked = validate_run.inspect_run(directory)
    require(checked["valid"], "invalid diagnostic run: " + str(checked["errors"]))
    m = checked["manifest"]
    require(m.get("profiler_enabled") is True and m.get("run_purpose") == "diagnostic",
            "use a diagnostic run collected with the profiler")
    return checked


def response_ids(path):
    ids = set()
    data = []
    with path.open("rb") as src:
        for line in src:
            stripped = line.rstrip(b"\r\n")
            if stripped == b"" and data:
                payload = b"\n".join(data)
                if payload.strip() == b"[DONE]":
                    break
                event = benchmark.strict_json(payload)
                require(isinstance(event, dict), "invalid SSE data event: " + str(path))
                if "id" in event:
                    value = event["id"]
                    require(isinstance(value, str) and value.strip() and "\x00" not in value,
                            "invalid response ID: " + str(path))
                    ids.add(value)
                data.clear()
            elif stripped.startswith(b"data:"):
                data.append(stripped[5:].lstrip(b" "))
            elif stripped == b"data":
                data.append(b"")
    require(len(ids) <= 1, "conflicting response IDs in SSE: " + str(path))
    return ids.pop() if ids else None


def request_map(run):
    run = run.resolve(strict=True)
    checked = read_diagnostic(run)
    manifest = checked["manifest"]
    work, digest = compare_trials.read_json(run / "workload.json")
    require(digest == checked["source_hashes"]["workload.json"], "workload changed during request mapping")
    positions = {r["request_id"]: i for i, r in enumerate(work)}
    out = io.StringIO(newline="")
    cols = ("run_id", "manifest_sha256", "request_id", "response_id", "sse_sha256")
    writer = csv.DictWriter(out, fieldnames=cols)
    writer.writeheader()
    seen = set()
    for req in step_ledger.checked_requests(run, checked).values():
        if req["final_status"] != "success":
            continue
        prefix = "raw/%08d" % positions[req["request_id"]]
        for field, ext in (("raw_response_path", ".sse"), ("output_path", ".output.json")):
            require(req.get(field) == prefix + ext, "raw request evidence path mismatch")
            (run / req[field]).resolve(strict=True).relative_to(run)
        sse = run / req["raw_response_path"]
        sha = profile.file_hash(sse)
        output, _ = compare_trials.read_json(run / req["output_path"])
        verify_stream(sse, req, output)
        key = response_ids(sse)
        require(profile.file_hash(sse) == sha, "SSE changed during replay")
        if key is not None:
            require(key not in seen, "response ID reused by multiple requests: " + key)
            seen.add(key)
            writer.writerow(dict(zip(cols, (manifest["run_id"], checked["source_hashes"]["manifest.json"],
                                             req["request_id"], key, sha))))
    return out.getvalue()

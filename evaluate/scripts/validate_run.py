#!/usr/bin/env python3
"""Validate schema-v1 finite-batch measurements; never infer missing token counts."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

if __package__:
    from .implementation_identity import implementation_fields
else:
    if __name__ == "__main__":
        sys.dont_write_bytecode = True  # Standalone tools, including --help, leave the suite read-only.
    from implementation_identity import implementation_fields


class InputError(ValueError):
    pass


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate JSON object key: %s" % key)
        result[key] = value
    return result


def parse_json(text):
    # Python normally accepts NaN/Infinity: keep them for an explicit semantic
    # validation failure, instead of letting non-finite numbers enter metrics.
    return json.loads(text, object_pairs_hook=strict_object)


def number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def integer(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def nonfinite_paths(value, path):
    if isinstance(value, float) and not math.isfinite(value):
        return [path + " contains a non-finite number"]
    if isinstance(value, dict):
        return [error for key, item in value.items()
                for error in nonfinite_paths(item, path + "." + str(key))]
    if isinstance(value, list):
        return [error for index, item in enumerate(value)
                for error in nonfinite_paths(item, path + "[%d]" % index)]
    return []


def quantile(values, probability):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    low = math.floor(index)
    high = math.ceil(index)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def canonical_hash(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def in_flight_integral(intervals, window_start, window_end):
    """Integrate count of [send_start,end) requests, not a mean over events."""
    events = {}
    for start, end in intervals:
        events[start] = events.get(start, 0) + 1
        events[end] = events.get(end, 0) - 1
    previous = window_start
    active = 0
    area = 0.0
    for timestamp in sorted(events):
        area += active * (timestamp - previous)
        active += events[timestamp]
        if active < 0:
            raise ValueError("negative in-flight count")
        previous = timestamp
    area += active * (window_end - previous)
    if active != 0:
        raise ValueError("in-flight count does not return to zero")
    return area


def validate(manifest, requests):
    errors, warnings = [], []

    def check(condition, message):
        if not condition:
            errors.append(message)

    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"], warnings, {}
    required = ("schema_version run_id scenario measurement_mode start_ts end_ts clock "
                "profiler_enabled workload_sha256 config_sha256 environment_sha256 "
                "metric_contract_sha256 benchmark_sha256 quality_status "
                "effective_config_verified client_measurement_policy "
                "client_measurement_policy_sha256 metadata_sha256 run_purpose").split()
    for key in required:
        check(key in manifest, "manifest missing field: " + key)
    check(type(manifest.get("schema_version")) is int and manifest.get("schema_version") == 1,
          "manifest.schema_version must be integer 1; legacy records are not accepted")
    for key in ("run_id", "scenario"):
        check(nonempty(manifest.get(key)), "manifest.%s must be a nonempty string" % key)
    check(manifest.get("measurement_mode") == "finite_batch",
          "manifest.measurement_mode must be finite_batch")
    check(manifest.get("run_purpose") in ("performance", "diagnostic"),
          "manifest.run_purpose must be performance/diagnostic")
    check(manifest.get("clock") == "client_perf_counter",
          "manifest.clock must be client_perf_counter")
    for key in ("profiler_enabled", "effective_config_verified"):
        check(type(manifest.get(key)) is bool, "manifest.%s must be a JSON boolean" % key)
    check(manifest.get("quality_status") in ("pass", "fail", "not_evaluated"),
          "manifest.quality_status must be pass/fail/not_evaluated")
    try:
        implementation_fields(manifest)
    except ValueError as exc:
        errors.append("manifest: " + str(exc))
    errors.extend(nonfinite_paths(manifest, "manifest"))
    for key in ("workload_sha256", "config_sha256", "environment_sha256",
                "metric_contract_sha256", "benchmark_sha256", "client_measurement_policy_sha256",
                "metadata_sha256"):
        value = manifest.get(key)
        check(isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None,
              "manifest.%s must contain 64 hexadecimal digits" % key)
    if "benchmark_hash_algorithm" in manifest or "benchmark_source_hashes" in manifest:
        check(manifest.get("benchmark_hash_algorithm") == "sha256-canonical-source-map-v1",
              "missing or unsupported benchmark_hash_algorithm")
        sources = manifest.get("benchmark_source_hashes")
        sources_ok = (isinstance(sources, dict) and set(sources) == {"benchmark.py", "implementation_identity.py"}
                      and all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in sources.values()))
        check(sources_ok, "benchmark_source_hashes must list both collector source files and their SHA-256 hashes")
        if sources_ok:
            check(canonical_hash(sources) == str(manifest.get("benchmark_sha256", "")).lower(),
                  "benchmark_sha256 does not match canonical benchmark_source_hashes")
    policy = manifest.get("client_measurement_policy")
    check(isinstance(policy, dict) and bool(policy),
          "manifest.client_measurement_policy must be a nonempty JSON object")
    if isinstance(policy, dict) and not nonfinite_paths(policy, "policy"):
        check(canonical_hash(policy) == str(manifest.get("client_measurement_policy_sha256", "")).lower(),
              "manifest.client_measurement_policy_sha256 does not match canonical policy JSON")
    start, end = manifest.get("start_ts"), manifest.get("end_ts")
    window_ok = number(start) and number(end) and 0 <= start < end
    check(window_ok, "manifest window must have finite 0 <= start_ts < end_ts")
    check(bool(requests), "requests.jsonl must contain at least one request")
    if "n_requests" in manifest:
        check(integer(manifest["n_requests"]) and manifest["n_requests"] == len(requests),
              "manifest.n_requests must equal the number of request records")
    identifiers = set()
    for index, request in enumerate(requests, 1):
        prefix = "request[%d]" % index
        if not isinstance(request, dict):
            errors.append(prefix + " must be a JSON object")
            continue
        errors.extend(nonfinite_paths(request, prefix))
        required_request = ("run_id request_id planned_send_ts send_start_ts response_open_ts "
                            "first_output_ts last_output_ts end_ts final_status finish_reason "
                            "requested_max_tokens actual_output_tokens actual_prompt_tokens "
                            "token_count_source output_event_ts error "
                            "stream_done_received final_usage_received").split()
        for key in required_request:
            check(key in request, prefix + " missing field: " + key)
        check(request.get("run_id") == manifest.get("run_id") and nonempty(request.get("run_id")),
              prefix + ".run_id must match manifest.run_id")
        request_id = request.get("request_id")
        if nonempty(request_id):
            check(request_id not in identifiers, prefix + ".request_id is duplicated: " + request_id)
            identifiers.add(request_id)
        else:
            errors.append(prefix + ".request_id must be a nonempty string")
        status = request.get("final_status")
        check(status in ("success", "error", "timeout", "cancelled"),
              prefix + ".final_status is not supported")
        for key in ("stream_done_received", "final_usage_received"):
            check(type(request.get(key)) is bool,
                  prefix + ".%s must be a JSON boolean" % key)
        for key in ("planned_send_ts", "send_start_ts", "end_ts"):
            check(number(request.get(key)), prefix + ".%s must be a finite number" % key)
        for key in ("response_open_ts", "first_output_ts", "last_output_ts"):
            check(request.get(key) is None or number(request.get(key)),
                  prefix + ".%s must be null or a finite number" % key)
        planned, sent, finished = (request.get(key) for key in
                                   ("planned_send_ts", "send_start_ts", "end_ts"))
        request_time_ok = all(number(value) for value in (planned, sent, finished))
        if request_time_ok:
            check(planned <= sent <= finished, prefix + " requires planned_send_ts <= send_start_ts <= end_ts")
            if window_ok:
                check(start <= planned <= sent <= finished <= end,
                      prefix + " request lifetime must be inside the full manifest window")
        opened = request.get("response_open_ts")
        if number(opened) and number(sent) and number(finished):
            check(sent <= opened <= finished, prefix + ".response_open_ts is outside request lifetime")
        event_times = request.get("output_event_ts")
        events_ok = isinstance(event_times, list) and all(number(value) for value in event_times)
        check(events_ok, prefix + ".output_event_ts must be an array of finite numbers")
        first, last = request.get("first_output_ts"), request.get("last_output_ts")
        if events_ok:
            check(all(a <= b for a, b in zip(event_times, event_times[1:])),
                  prefix + ".output_event_ts must be nondecreasing")
            if event_times:
                check(first == event_times[0] and last == event_times[-1],
                      prefix + " first/last_output_ts must match the first/last payload event")
                check(number(opened), prefix + " output events require response_open_ts")
                if number(opened) and number(finished):
                    check(all(opened <= event <= finished for event in event_times),
                          prefix + " output events must be inside response lifetime")
            else:
                check(first is None and last is None,
                      prefix + " empty output events require null first/last_output_ts")
        if isinstance(policy, dict) and policy.get("channel_evidence_version") == 1:
            indices = request.get("first_channel_event")
            check(isinstance(indices, dict) and set(indices) <= {"content", "reasoning_content"}
                  and events_ok and all(integer(i) and i < len(event_times) for i in indices.values())
                  and (min(indices.values(), default=None) == (0 if event_times else None)),
                  prefix + ".first_channel_event must identify the first nonempty events")
            count = request.get("server_content_tokens")
            total = request.get("actual_output_tokens")
            check("server_content_tokens" in request and (count is None or
                  (integer(count) and integer(total) and count <= total)),
                  prefix + ".server_content_tokens must be null or within the final completion count")
        maximum = request.get("requested_max_tokens")
        check(integer(maximum, 1), prefix + ".requested_max_tokens must be a positive integer")
        for key in ("actual_output_tokens", "actual_prompt_tokens"):
            value = request.get(key)
            check(value is None or integer(value), prefix + ".%s must be null or a nonnegative integer" % key)
        actual = request.get("actual_output_tokens")
        prompt = request.get("actual_prompt_tokens")
        source = request.get("token_count_source")
        check(source in (None, "server_usage"), prefix + ".token_count_source must be null/server_usage")
        if actual is not None or prompt is not None:
            check(source == "server_usage", prefix + " actual counts require token_count_source=server_usage")
        if source == "server_usage":
            check(actual is not None or prompt is not None, prefix + " server_usage requires at least one actual count")
        if integer(actual) and integer(maximum, 1):
            check(actual <= maximum, prefix + " actual_output_tokens exceeds requested_max_tokens for n=1")
        reason = request.get("finish_reason")
        check(reason is None or nonempty(reason), prefix + ".finish_reason must be null or a nonempty string")
        terminal = (
            ("finish_reason_ts", reason is not None),
            ("final_usage_ts", request.get("final_usage_received") is True),
            ("stream_done_ts", request.get("stream_done_received") is True),
        )
        if any(key in request for key, _ in terminal):
            stamps = []
            for key, observed in terminal:
                value = request.get(key)
                check(key in request and (number(value) if observed else value is None),
                      prefix + ".%s must match the observed stream event" % key)
                if number(value):
                    stamps.append(value)
            if stamps:
                check(number(opened) and number(finished) and opened <= stamps[0] <= stamps[-1] <= finished,
                      prefix + " terminal events must be inside response lifetime")
                check(all(a <= b for a, b in zip(stamps, stamps[1:])),
                      prefix + " terminal events must be nondecreasing")
                check(not number(last) or last <= stamps[0],
                      prefix + " terminal events must follow the last nonempty output")
        error = request.get("error")
        if status == "success":
            check(request.get("stream_done_received") is True,
                  prefix + " success requires stream_done_received=true")
            check(request.get("final_usage_received") is True,
                  prefix + " success requires final_usage_received=true")
            check(reason in ("stop", "length"), prefix + " success requires finish_reason stop/length")
            check(integer(actual) and integer(prompt), prefix + " success requires actual output and prompt usage counts")
            check(source == "server_usage", prefix + " success requires server_usage; quotas are not actual tokens")
            check(number(opened), prefix + " success requires response_open_ts")
            check(error is None, prefix + " success requires error=null")
            if integer(actual) and events_ok:
                check((actual > 0) == bool(event_times),
                      prefix + " success payload presence must agree with actual_output_tokens")
        elif status in ("error", "timeout", "cancelled"):
            check(nonempty(error), prefix + " failed requests require a nonempty error string")

    if errors:
        return errors, warnings, {}
    wall = end - start
    successes = [request for request in requests if request["final_status"] == "success"]
    intervals = [(request["send_start_ts"], request["end_ts"]) for request in requests]
    integral = in_flight_integral(intervals, start, end)
    residence_sum = math.fsum(end_time - start_time for start_time, end_time in intervals)
    if not math.isclose(integral, residence_sum, rel_tol=1e-10, abs_tol=1e-9):
        return ["in-flight time integral does not reconcile with summed request residence time"], warnings, {}
    tokens = sum(request["actual_output_tokens"] for request in successes)
    ttft = [request["first_output_ts"] - request["send_start_ts"] for request in successes
            if request["first_output_ts"] is not None]
    e2e = [request["end_ts"] - request["send_start_ts"] for request in successes]
    metrics = {
        "n_requests": len(requests), "n_success": len(successes),
        "n_failed": len(requests) - len(successes),
        "failure_rate": (len(requests) - len(successes)) / len(requests),
        "wall_s": wall, "effective_output_tokens": tokens,
        "output_token_throughput": tokens / wall,
        "request_throughput": len(successes) / wall,
        "ttft_p50_s": quantile(ttft, 0.5), "ttft_p99_s": quantile(ttft, 0.99),
        "e2e_p50_s": quantile(e2e, 0.5), "e2e_p99_s": quantile(e2e, 0.99),
        "ttft_sample_count": len(ttft), "e2e_sample_count": len(e2e),
        "observed_mean_in_flight": integral / wall,
        "in_flight_integral_request_s": integral,
        "summed_request_residence_s": residence_sum,
    }
    if isinstance(policy, dict) and policy.get("channel_evidence_version") == 1:
        lags = [request["send_start_ts"] - request["planned_send_ts"] for request in requests]
        metrics.update(send_lag_p99_s=quantile(lags, 0.99), send_lag_max_s=max(lags))
    if not ttft:
        warnings.append("TTFT is unavailable: no successful request has a nonempty output payload")
    elif len(ttft) < len(successes):
        warnings.append("TTFT excludes successful zero-output requests; inspect ttft_sample_count")
    if len(successes) < 100:
        warnings.append("Fewer than 100 successful requests: interpolated p99 has limited tail resolution")
    if manifest["profiler_enabled"]:
        warnings.append("Profiler enabled: this run cannot establish an unprofiled performance gain")
    if manifest["run_purpose"] == "diagnostic":
        warnings.append("Diagnostic run: measurement validity does not authorize performance promotion")
    if manifest["quality_status"] != "pass":
        warnings.append("Quality has not passed; measurement validity is not quality approval")
    if not manifest["effective_config_verified"]:
        warnings.append("Effective serving configuration is declared unverified")
    warnings.append("Usage, completion status and identity hashes are collector declarations; validation does not independently attest the live server")
    return errors, warnings, metrics


def read_verified_requests(run_dir, validated):
    """Read request rows only if their bytes match the inspected originals."""
    data = (Path(run_dir) / "requests.jsonl").read_bytes()
    if hashlib.sha256(data).hexdigest() != validated["source_hashes"]["requests.jsonl"]:
        raise ValueError("requests.jsonl changed after benchmark verification")
    return [parse_json(line) for line in data.decode("utf-8").splitlines()]


def inspect_run(run_dir):
    manifest_path = run_dir / "manifest.json"
    requests_path = run_dir / "requests.jsonl"
    manifest_bytes = manifest_path.read_bytes()
    requests_bytes = requests_path.read_bytes()
    manifest = parse_json(manifest_bytes.decode("utf-8"))
    requests = []
    for line_number, line in enumerate(requests_bytes.decode("utf-8").splitlines(), 1):
        if not line.strip():
            raise InputError("requests.jsonl contains blank line %d" % line_number)
        try:
            requests.append(parse_json(line))
        except (ValueError, TypeError) as exc:
            raise InputError("requests.jsonl line %d: %s" % (line_number, exc)) from exc
    source_hashes = {
        "manifest.json": hashlib.sha256(manifest_bytes).hexdigest(),
        "requests.jsonl": hashlib.sha256(requests_bytes).hexdigest(),
    }
    errors, warnings, metrics = validate(manifest, requests)
    for name, hash_key, canonical in (("workload.json", "workload_sha256", True),
                                      ("metadata.json", "metadata_sha256", False)):
        path = run_dir / name
        if path.exists():
            raw = path.read_bytes()
            source_hashes[name] = hashlib.sha256(raw).hexdigest()
            value = parse_json(raw.decode("utf-8"))
            expected_type = list if canonical else dict
            if not isinstance(value, expected_type) or not value:
                errors.append(name + " must contain a nonempty " + expected_type.__name__)
            bad_values = nonfinite_paths(value, name)
            errors.extend(bad_values)
            expected_hash = manifest.get(hash_key) if isinstance(manifest, dict) else None
            if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
                errors.append("manifest.%s must contain 64 hexadecimal digits when %s is present" % (hash_key, name))
            elif not bad_values:
                actual_hash = canonical_hash(value) if canonical else source_hashes[name]
                if actual_hash != expected_hash.lower():
                    errors.append("%s does not match manifest.%s" % (name, hash_key))
            if name == "metadata.json" and isinstance(value, dict) and isinstance(manifest, dict):
                try:
                    if canonical_hash(implementation_fields(value)) != canonical_hash(implementation_fields(manifest)):
                        errors.append("metadata.json implementation fields must match manifest exactly")
                except ValueError as exc:
                    errors.append("implementation identity: " + str(exc))
                for key in ("scenario", "config_sha256", "environment_sha256",
                            "metric_contract_sha256", "quality_status",
                            "effective_config_verified", "profiler_enabled", "run_purpose"):
                    if (key not in value or type(value.get(key)) is not type(manifest.get(key))
                            or value.get(key) != manifest.get(key)):
                        errors.append("metadata.json.%s must match manifest.%s exactly" % (key, key))
            if name == "workload.json" and isinstance(value, list):
                workload_by_id = {}
                for index, item in enumerate(value, 1):
                    if not isinstance(item, dict):
                        errors.append("workload[%d] must be an object" % index)
                        continue
                    identity = item.get("request_id")
                    if not nonempty(identity):
                        errors.append("workload[%d].request_id must be a nonempty string" % index)
                    elif identity in workload_by_id:
                        errors.append("workload request_id is duplicated: " + identity)
                    else:
                        workload_by_id[identity] = item
                    if not isinstance(item.get("prompt_text"), str):
                        errors.append("workload[%d].prompt_text must be a string" % index)
                    if not integer(item.get("output_tokens"), 1):
                        errors.append("workload[%d].output_tokens must be a positive integer quota" % index)
                request_by_id = {request["request_id"]: request for request in requests
                                 if isinstance(request, dict) and nonempty(request.get("request_id"))}
                if len(value) != len(requests) or set(workload_by_id) != set(request_by_id):
                    errors.append("workload and request records must have equal lengths and identical request_id sets")
                for identity in workload_by_id.keys() & request_by_id.keys():
                    if isinstance(manifest, dict) and isinstance(manifest.get("client_measurement_policy"), dict):
                        if manifest["client_measurement_policy"].get("mode") == "open_loop":
                            offset = workload_by_id[identity].get("arrival_offset_ms")
                            origin = manifest.get("start_ts")
                            if (not number(offset) or offset < 0 or not number(origin)
                                    or request_by_id[identity].get("planned_send_ts") != origin + offset / 1000):
                                errors.append("planned send time differs from arrival schedule for request_id=" + identity)
                    quota = workload_by_id[identity].get("output_tokens")
                    measured_quota = request_by_id[identity].get("requested_max_tokens")
                    if type(quota) is not type(measured_quota) or quota != measured_quota:
                        errors.append("workload quota differs from requested_max_tokens for request_id=" + identity)
        else:
            errors.append(name + " is required; the saved original input is missing")
    if errors:
        metrics = {}
    return {
        "schema_version": 1, "valid": not errors, "errors": errors, "warnings": warnings,
        "manifest": manifest, "source_hashes": source_hashes, "metrics": metrics,
        "metric_definitions": {
            "output_token_throughput": "sum(actual_output_tokens of final success requests) / full manifest wall_s",
            "request_throughput": "n_success / full manifest wall_s",
            "latency_population": "successful requests; TTFT additionally requires a nonempty payload",
            "percentile_method": "linear interpolation at (n-1)*p in sorted samples",
            "observed_mean_in_flight": "integral of all request lifetimes [send_start_ts,end_ts) / full manifest wall_s",
        },
    }


def safe_json(value):
    """Invalid non-finite inputs are described in errors, never emitted as JSON NaN."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_nonfinite": str(value)}
    if isinstance(value, dict):
        return {key: safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_json(item) for item in value]
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new JSON report; existing paths are refused")
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise InputError("output already exists; refusing to overwrite: %s" % args.output)
        report = inspect_run(args.run_dir)
        encoded = json.dumps(safe_json(report), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError) as exc:
        print("input/IO error: %s" % exc, file=sys.stderr)
        return 2
    print("valid=%s; report=%s" % (str(report["valid"]).lower(), args.output))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Finite, auditable chat-completions benchmark (Python 3.9+, standard library).

This is a bounded thread client, not a claim that a service is saturated. It
measures successful server-reported completion tokens over the entire run
window. SSE output events are chunks, never assumed to be individual tokens.
No retries, warmup traffic, service changes, or automatic quality claims occur.
"""

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

if __package__:
    from . import implementation_identity
else:
    if __name__ == "__main__":
        sys.dont_write_bytecode = True  # Standalone tools, including --help, leave the suite read-only.
    import implementation_identity


MAX_SSE_LINE = 4 * 1024 * 1024
MAX_SSE_EVENT = 8 * 1024 * 1024
MAX_WORKERS = 1024
SUCCESS_REASONS = {"stop", "length"}


class InputError(ValueError):
    pass


class StreamError(ValueError):
    pass


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def strict_json(text):
    def reject_constant(value):
        raise ValueError("non_finite_json_number")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def read_inputs(args):
    metadata_bytes = Path(args.metadata).read_bytes()
    try:
        metadata = strict_json(metadata_bytes)
    except (ValueError, UnicodeError) as exc:
        raise InputError("metadata must be valid finite JSON") from exc
    if not isinstance(metadata, dict):
        raise InputError("metadata must be an object")
    if not isinstance(metadata.get("scenario"), str) or not metadata["scenario"].strip():
        raise InputError("metadata.scenario must be a nonempty string")
    for key in ("config_sha256", "environment_sha256", "metric_contract_sha256"):
        if not isinstance(metadata.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", metadata[key]):
            raise InputError("metadata.%s must be a lowercase SHA-256 digest" % key)
    if metadata.get("quality_status") not in ("pass", "fail", "not_evaluated"):
        raise InputError("metadata.quality_status must be pass/fail/not_evaluated")
    if metadata.get("run_purpose") not in ("performance", "diagnostic"):
        raise InputError("metadata.run_purpose must explicitly be performance or diagnostic")
    for key in ("effective_config_verified", "profiler_enabled"):
        if not isinstance(metadata.get(key), bool):
            raise InputError("metadata.%s must be an explicit JSON boolean" % key)

    try:
        implementation_identity.implementation_fields(metadata)
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    workload = []
    seen = set()
    with Path(args.dataset).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = strict_json(line)
            except ValueError as exc:
                raise InputError("dataset line %d is not valid finite JSON" % line_number) from exc
            if not isinstance(item, dict):
                raise InputError("dataset line %d must be an object" % line_number)
            request_id = item.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                raise InputError("dataset request_id must be a nonempty string")
            if request_id in seen:
                raise InputError("dataset contains duplicate request_id")
            if not isinstance(item.get("prompt_text"), str):
                raise InputError("dataset prompt_text must be a string")
            if not is_int(item.get("output_tokens")) or item["output_tokens"] < 1:
                raise InputError("dataset output_tokens must be a positive integer")
            arrival = item.get("arrival_offset_ms")
            if args.mode == "open_loop":
                if not is_number(arrival) or arrival < 0:
                    raise InputError("open_loop requires a finite nonnegative arrival_offset_ms on every request")
            elif arrival is not None:
                raise InputError("closed_loop cannot ignore arrival_offset_ms; choose --mode open_loop or explicitly use a workload with null/missing offsets")
            seen.add(request_id)
            workload.append(item)
            if args.max_requests is not None and len(workload) >= args.max_requests:
                break
    if not workload:
        raise InputError("dataset has no selected requests")
    return metadata, metadata_bytes, workload


def parse_endpoint(base_url):
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise InputError("invalid base URL") from exc
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise InputError("base URL must be HTTP(S), without credentials, query, or fragment")
    path = parsed.path.rstrip("/")
    if path in ("", "/v1"):
        path = "/v1/chat/completions"
    elif not path.endswith("/chat/completions"):
        raise InputError("base URL path must be empty, /v1, or a full /chat/completions endpoint")
    if any(ord(char) < 32 or ord(char) == 127 for char in base_url):
        raise InputError("base URL contains a control character")
    return {"scheme": parsed.scheme, "host": parsed.hostname,
            "port": port or (443 if parsed.scheme == "https" else 80),
            "path": path, "base_url": base_url.rstrip("/")}


class DeadlineConnection(http.client.HTTPConnection):
    """Direct connection to pre-resolved addresses, with a cancellable socket.

    DNS is resolved once before the measurement window. The watchdog shuts down
    the socket at the absolute request deadline, including during slow headers
    and trickled SSE lines. TLS verifies the original URL hostname.
    """

    def __init__(self, endpoint, addresses, deadline, tls_context):
        super().__init__(endpoint["host"], endpoint["port"],
                         timeout=max(0.001, deadline - time.perf_counter()))
        self.endpoint = endpoint
        self.addresses = addresses
        self.deadline = deadline
        self.tls_context = tls_context
        self.expired = threading.Event()
        self._wire_socket = None
        self._socket_lock = threading.Lock()

    def remaining(self):
        remaining = self.deadline - time.perf_counter()
        if self.expired.is_set() or remaining <= 0:
            raise TimeoutError("request_deadline_exceeded")
        return remaining

    def abort(self):
        self.expired.set()
        with self._socket_lock:
            sock = self._wire_socket
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

    def _install_socket(self, sock):
        with self._socket_lock:
            if self.expired.is_set():
                sock.close()
                raise TimeoutError("request_deadline_exceeded")
            self._wire_socket = sock
            self.sock = sock

    def refresh_timeout(self):
        remaining = self.remaining()
        with self._socket_lock:
            if self._wire_socket is not None:
                self._wire_socket.settimeout(remaining)

    def connect(self):
        last_error = None
        for family, socktype, proto, _canonname, address in self.addresses:
            self.remaining()
            sock = socket.socket(family, socktype, proto)
            self._install_socket(sock)
            try:
                sock.settimeout(self.remaining())
                sock.connect(address)
                if self.endpoint["scheme"] == "https":
                    # Install the SSLSocket before handshaking so the deadline
                    # can interrupt a stalled TLS handshake too.
                    sock = self.tls_context.wrap_socket(
                        sock, server_hostname=self.endpoint["host"],
                        do_handshake_on_connect=False)
                    self._install_socket(sock)
                    sock.settimeout(self.remaining())
                    sock.do_handshake()
                return
            except (OSError, TimeoutError) as exc:
                last_error = exc
                sock.close()
                self.remaining()
        if last_error is not None:
            raise last_error
        raise OSError("no_resolved_address")


def parse_usage(value):
    if not isinstance(value, dict):
        raise StreamError("invalid_usage")
    prompt, completion = value.get("prompt_tokens"), value.get("completion_tokens")
    if not is_int(prompt) or not is_int(completion) or prompt < 0 or completion < 0:
        raise StreamError("invalid_usage_token_counts")
    return prompt, completion


def consume_event(data, received_ts, record, output):
    """Consume one SSE data event. Return True only for a valid DONE marker."""
    if data.strip() == "[DONE]":
        record["stream_done_ts"] = received_ts
        return True
    try:
        payload = strict_json(data)
    except (ValueError, UnicodeError) as exc:
        raise StreamError("invalid_sse_json") from exc
    if not isinstance(payload, dict) or "error" in payload:
        raise StreamError("server_error_event")
    choices = payload.get("choices", [])
    if not isinstance(choices, list) or len(choices) > 1:
        raise StreamError("unsupported_choices")
    if choices:
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("index", 0) != 0:
            raise StreamError("unsupported_choice_index")
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            raise StreamError("invalid_delta")
        has_output = False
        for channel in ("content", "reasoning_content"):
            value = delta.get(channel)
            if value is not None and not isinstance(value, str):
                raise StreamError("unsupported_output_payload")
            if value:
                if record["finish_reason"] is not None:
                    raise StreamError("output_after_finish")
                output[channel].append(value)
                has_output = True
        if delta.get("tool_calls") or delta.get("function_call"):
            raise StreamError("unsupported_tool_output")
        if has_output:
            record["output_event_ts"].append(received_ts)
            if record["first_output_ts"] is None:
                record["first_output_ts"] = received_ts
            record["last_output_ts"] = received_ts
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise StreamError("invalid_finish_reason")
            if record["finish_reason"] is not None:
                raise StreamError("duplicate_finish_reason")
            record["finish_reason"] = reason
            record["finish_reason_ts"] = received_ts
    if payload.get("usage") is not None:
        prompt, completion = parse_usage(payload["usage"])
        record["actual_prompt_tokens"] = prompt
        record["actual_output_tokens"] = completion
        record["token_count_source"] = "server_usage"
        if record["finish_reason"] is None:
            raise StreamError("usage_before_finish")
        record["final_usage_received"] = True
        record["final_usage_ts"] = received_ts
    return False


def read_sse(response, connection, raw_handle, record, output, api_key):
    data_lines = []
    event_bytes = 0
    secret_bytes = api_key.encode("utf-8") if api_key else None
    while True:
        connection.refresh_timeout()
        line = response.readline(MAX_SSE_LINE + 1)
        received_ts = time.perf_counter()
        if not line:
            raise StreamError("stream_ended_without_done")
        if len(line) > MAX_SSE_LINE:
            raise StreamError("sse_line_too_large")
        # Authorization headers are never serialized. If a server echoes the
        # credential, redact it in the retained stream and mark that fact.
        retained = line
        if secret_bytes and secret_bytes in retained:
            retained = retained.replace(secret_bytes, b"[REDACTED_API_KEY]")
            record["raw_response_redacted"] = True
        raw_handle.write(retained)
        connection.remaining()
        stripped = line.rstrip(b"\r\n")
        if not stripped:
            if data_lines:
                try:
                    event = b"\n".join(data_lines).decode("utf-8")
                except UnicodeError as exc:
                    raise StreamError("invalid_sse_utf8") from exc
                if consume_event(event, received_ts, record, output):
                    record["stream_done_received"] = True
                    break
            data_lines = []
            event_bytes = 0
        elif stripped.startswith(b"data:"):
            value = stripped[5:]
            if value.startswith(b" "):
                value = value[1:]
            event_bytes += len(value)
            if event_bytes > MAX_SSE_EVENT:
                raise StreamError("sse_event_too_large")
            data_lines.append(value)
        elif stripped == b"data":
            data_lines.append(b"")
        # SSE comments, id, event, and retry fields do not contain output.


def request_payload(item, args):
    payload = {
        "model": args.model, "messages": [{"role": "user", "content": item["prompt_text"]}],
        "max_tokens": item["output_tokens"], "n": 1,
        "stream": True, "stream_options": {"include_usage": True},
        "temperature": args.temperature, "top_p": args.top_p, "seed": args.seed,
    }
    if args.ignore_eos:
        payload["ignore_eos"] = True
    return payload


def run_request(index, item, body, args, endpoint, addresses, tls_context,
                run_id, start_ts, run_dir, api_key, cancel_event):
    planned = (start_ts + item["arrival_offset_ms"] / 1000.0
               if args.mode == "open_loop" else None)
    if planned is not None:
        if cancel_event.wait(max(0.0, planned - time.perf_counter())):
            return None
    if cancel_event.is_set():
        return None
    send_start = time.perf_counter()
    if planned is None:
        planned = send_start
    record = {
        "run_id": run_id, "request_id": item["request_id"],
        "planned_send_ts": planned, "send_start_ts": send_start,
        "send_lag_s": max(0.0, send_start - planned),
        "response_open_ts": None, "first_output_ts": None, "last_output_ts": None,
        "finish_reason_ts": None, "final_usage_ts": None, "stream_done_ts": None,
        "end_ts": None, "final_status": "error", "finish_reason": None,
        "requested_max_tokens": item["output_tokens"],
        "actual_output_tokens": None, "actual_prompt_tokens": None,
        "token_count_source": None, "output_event_ts": [], "error": None,
        "stream_done_received": False, "final_usage_received": False,
        "raw_request_path": "raw/%08d.request.json" % index,
        "raw_response_path": "raw/%08d.sse" % index,
        "output_path": "raw/%08d.output.json" % index,
        "raw_response_redacted": False,
    }
    connection = DeadlineConnection(endpoint, addresses, send_start + args.timeout, tls_context)
    timer = threading.Timer(max(0.0, connection.deadline - time.perf_counter()), connection.abort)
    timer.daemon = True
    timer.start()
    output = {"content": [], "reasoning_content": []}
    response = None
    raw_handle = None
    try:
        raw_handle = (run_dir / record["raw_response_path"]).open("xb")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        connection.request("POST", endpoint["path"], body=body, headers=headers)
        connection.refresh_timeout()
        response = connection.getresponse()
        record["response_open_ts"] = time.perf_counter()
        connection.remaining()
        record["http_status"] = response.status
        if response.status != 200:
            raise StreamError("http_status_%d" % response.status)
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "text/event-stream":
            raise StreamError("unexpected_content_type")
        read_sse(response, connection, raw_handle, record, output, api_key)
        if record["finish_reason"] not in SUCCESS_REASONS:
            raise StreamError("missing_or_unsupported_finish_reason")
        if not record["final_usage_received"]:
            raise StreamError("missing_final_usage")
        if record["actual_output_tokens"] > 0 and not record["output_event_ts"]:
            raise StreamError("positive_usage_without_output_payload")
        if record["actual_output_tokens"] == 0 and record["output_event_ts"]:
            raise StreamError("output_payload_with_zero_usage")
        connection.remaining()
        record["final_status"] = "success"
    except (TimeoutError, socket.timeout):
        record["final_status"] = "timeout"
        record["error"] = "request_deadline_exceeded"
    except (StreamError, OSError, http.client.HTTPException, ValueError) as exc:
        if connection.expired.is_set() or time.perf_counter() >= connection.deadline:
            record["final_status"] = "timeout"
            record["error"] = "request_deadline_exceeded"
        else:
            # Never serialize arbitrary exception messages (which may contain
            # server text or headers). StreamError messages are fixed codes.
            record["error"] = str(exc) if isinstance(exc, StreamError) else "transport_" + type(exc).__name__
    finally:
        record["end_ts"] = time.perf_counter()
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()
        if raw_handle is not None:
            raw_handle.close()
    retained_output = {key: "".join(parts) for key, parts in output.items()}
    if api_key:
        retained_output = {key: value.replace(api_key, "[REDACTED_API_KEY]")
                           for key, value in retained_output.items()}
    with (run_dir / record["output_path"]).open("xb") as handle:
        handle.write(canonical_bytes(retained_output))
    return record


def measurement_policy(args):
    return {
        "version": 1, "mode": args.mode, "concurrency": args.concurrency,
        "workers": args.workers, "effective_parallelism": min(args.concurrency, args.workers),
        "timeout_s": args.timeout, "model": args.model, "seed": args.seed,
        "temperature": args.temperature, "top_p": args.top_p,
        "ignore_eos": args.ignore_eos, "n": 1, "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": "per_request_output_tokens", "selection": "first_N_in_file_order",
        "max_requests": args.max_requests, "retries": 0,
        "clock": "client_perf_counter", "dns": "resolved_once_before_run_window",
        "connection": "fresh_direct_connection_per_request_verified_TLS_no_proxy",
        "request_window": "before_HTTP_call_through_DONE_or_failure_including_connect_and_headers",
        "run_window": "before_worker_start_through_all_request_completion_including_arrival_wait_and_client_collection",
        "output_count": "server_usage.completion_tokens_only_for_successful_requests",
        "output_events": "nonempty_content_or_reasoning_content_SSE_data_events_not_tokens",
        "success": "HTTP_200_SSE_DONE_stop_or_length_final_usage_supported_output",
        "arrival_policy": "preserve_open_loop_schedule_record_actual_send_lag_no_drop",
        "sse_max_line_bytes": MAX_SSE_LINE, "sse_max_event_bytes": MAX_SSE_EVENT,
        "raw_artifact_io": "buffered_per_request_stream_write_in_measured_client_path",
    }


def execute(args):
    endpoint = parse_endpoint(args.base_url)
    metadata, metadata_bytes, workload = read_inputs(args)
    api_key = os.environ.get(args.api_key_env, "")
    if api_key and any(ord(char) < 32 or ord(char) == 127 for char in api_key):
        raise InputError("API key environment value contains a control character")
    source_hashes = {
        "benchmark.py": digest(Path(__file__).read_bytes()),
        "implementation_identity.py": digest(Path(implementation_identity.__file__).read_bytes()),
    }
    run_dir = Path(args.run_dir)
    if run_dir.exists() or run_dir.is_symlink():
        raise InputError("run directory already exists; refusing to overwrite")
    if args.workers < args.concurrency:
        print("Client parallelism is capped at %d by --workers, below --concurrency=%d; "
              "inspect observed in-flight counts and open-loop send_lag_s before interpreting service capacity."
              % (args.workers, args.concurrency), file=sys.stderr)
    # Resolution and TLS context initialization are outside the measurement
    # window. Resolver delays are not represented as serving latency.
    addresses = socket.getaddrinfo(endpoint["host"], endpoint["port"],
                                   type=socket.SOCK_STREAM)
    tls_context = ssl.create_default_context() if endpoint["scheme"] == "https" else None
    payloads = [canonical_bytes(request_payload(item, args)) for item in workload]
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "raw").mkdir()
    for filename, data in (("metadata.json", metadata_bytes),
                           ("workload.json", canonical_bytes(workload))):
        with (run_dir / filename).open("xb") as handle:
            handle.write(data)
    for index, body in enumerate(payloads):
        with (run_dir / ("raw/%08d.request.json" % index)).open("xb") as handle:
            handle.write(body)
    policy = measurement_policy(args)
    run_id = str(uuid.uuid4())
    manifest = {
        "schema_version": 1, "run_id": run_id,
        "measurement_mode": "finite_batch", "clock": "client_perf_counter",
        "n_requests": len(workload), "workload_sha256": digest(canonical_bytes(workload)),
        "metadata_sha256": digest(metadata_bytes),
        "benchmark_hash_algorithm": "sha256-canonical-source-map-v1",
        "benchmark_source_hashes": source_hashes,
        "benchmark_sha256": digest(canonical_bytes(source_hashes)),
        "client_measurement_policy": policy,
        "client_measurement_policy_sha256": digest(canonical_bytes(policy)),
        "client_arguments": vars(args).copy(), "base_url": endpoint["base_url"],
        "endpoint_path": endpoint["path"],
        "resolved_addresses": [list(address[4]) for address in addresses],
        "client_runtime": {"python": platform.python_version(), "platform": platform.platform(),
                           "cpu_count": os.cpu_count(),
                           "clock_resolution_s": time.get_clock_info("perf_counter").resolution},
        "metadata_declarations_are_caller_supplied": True,
        "api_key_present": bool(api_key),
        "limitations": [
            "Finite batch includes ramp, arrival waits, failures, drain and client overhead; not steady-state capacity.",
            "Thread and concurrency caps can delay open-loop sends; inspect send_lag_s and observed in-flight counts.",
            "No proof of server saturation, quality, effective configuration, or unchanged host interference is supplied by this client.",
            "Usage is trusted server completion-token accounting (including reasoning if the server includes it); it is not visible-text retokenization.",
            "SSE events can contain zero, one, or multiple tokens; no token ITL is measured.",
            "Blocking DNS resolution occurs before the run window; request deadline covers subsequent connect/TLS/headers/body.",
            "A fresh connection and a watchdog thread per request, Python parsing, and raw artifact I/O add client overhead.",
            "Only independent n=1 text/reasoning requests are replayed; dataset session fields do not create conversational dependencies.",
        ],
    }
    for key in ("scenario", "config_sha256", "environment_sha256", "metric_contract_sha256",
                "quality_status", "effective_config_verified", "profiler_enabled", "run_purpose"):
        manifest[key] = metadata[key]
    manifest.update({key: metadata[key] for key in implementation_identity.FIELDS if key in metadata})
    order = list(range(len(workload)))
    if args.mode == "open_loop":
        order.sort(key=lambda index: (workload[index]["arrival_offset_ms"], index))
    cancel_event = threading.Event()
    start_ts = time.perf_counter()
    manifest["start_ts"] = start_ts
    # A provisional manifest is immutable evidence of an incomplete run. The
    # final manifest only exists after every selected request is accounted for.
    with (run_dir / "manifest.started.json").open("xb") as handle:
        handle.write(canonical_bytes(manifest))
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(args.concurrency, args.workers))
    records = []
    futures = []
    try:
        with (run_dir / "requests.jsonl").open("x", encoding="utf-8") as handle:
            for index in order:
                futures.append(executor.submit(
                    run_request, index, workload[index], payloads[index], args, endpoint,
                    addresses, tls_context, run_id, start_ts, run_dir, api_key, cancel_event))
            for future in concurrent.futures.as_completed(futures):
                record = future.result()
                if record is not None:
                    records.append(record)
                    handle.write(canonical_bytes(record).decode("utf-8") + "\n")
                    handle.flush()
    except BaseException:
        cancel_event.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True)
    manifest["end_ts"] = time.perf_counter()
    if len(records) != len(workload):
        raise RuntimeError("incomplete_request_accounting")
    with (run_dir / "manifest.json").open("xb") as handle:
        handle.write(canonical_bytes(manifest))
    failures = sum(record["final_status"] != "success" for record in records)
    return 1 if failures else 0


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base-url", required=True, help="http(s)://host[:port][/v1], or full chat-completions URL")
    result.add_argument("--model", required=True)
    result.add_argument("--dataset", required=True, help="finite workload JSONL")
    result.add_argument("--run-dir", required=True, help="new directory; existing paths are rejected")
    result.add_argument("--metadata", required=True, help="JSON with scenario, three identity hashes, quality/config declarations, profiler_enabled and run_purpose")
    result.add_argument("--concurrency", type=int, default=32, help="maximum simultaneous requests (default: 32)")
    result.add_argument("--workers", type=int, default=32, help="thread pool cap; effective parallelism=min(concurrency,workers), maximum 1024")
    result.add_argument("--timeout", type=float, default=300.0, help="absolute per-request seconds from HTTP call start, not an idle timeout")
    result.add_argument("--max-requests", type=int, help="use first N nonblank JSONL records, in file order")
    result.add_argument("--seed", type=int, default=42, help="same explicitly recorded sampling seed for every request")
    result.add_argument("--mode", choices=("closed_loop", "open_loop"), default="closed_loop",
                        help="closed_loop requires null/missing arrival offsets; open_loop preserves every planned offset")
    result.add_argument("--temperature", type=float, default=0.0)
    result.add_argument("--top-p", type=float, default=1.0)
    result.add_argument("--ignore-eos", action="store_true", help="explicit vLLM extension; omitted by default")
    result.add_argument("--api-key-env", default="OPENAI_API_KEY", help="environment-variable name; never pass a credential as an argument")
    return result


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.concurrency < 1 or args.workers < 1 or args.workers > MAX_WORKERS:
        arguments.error("concurrency must be positive; workers must be in [1, 1024]")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        arguments.error("timeout must be finite and positive")
    if args.max_requests is not None and args.max_requests < 1:
        arguments.error("max-requests must be positive")
    if not math.isfinite(args.temperature) or args.temperature < 0:
        arguments.error("temperature must be finite and nonnegative")
    if not math.isfinite(args.top_p) or not 0 < args.top_p <= 1:
        arguments.error("top-p must be in (0, 1]")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env):
        arguments.error("api-key-env must be an environment-variable name")
    if not args.model:
        arguments.error("model must not be empty")
    try:
        status = execute(args)
    except KeyboardInterrupt:
        print("Interrupted; retained artifacts are incomplete and no final manifest is issued.", file=sys.stderr)
        return 2
    except (InputError, OSError, ValueError, RuntimeError) as exc:
        # InputError contains only static/local validation diagnostics. Other
        # errors are class-only to avoid echoing server data or credentials.
        detail = str(exc) if isinstance(exc, InputError) else type(exc).__name__
        print("Benchmark error: " + detail, file=sys.stderr)
        return 2
    print("Run artifacts saved; %s. Validate with validate_run.py before comparing runs."
          % ("all requests completed" if status == 0 else "one or more requests failed"))
    return status


if __name__ == "__main__":
    sys.exit(main())

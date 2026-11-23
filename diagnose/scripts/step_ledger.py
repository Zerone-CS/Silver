"""Reconcile the finite-batch token count with uniquely owned logical engine steps."""

from collections import Counter, defaultdict
from pathlib import Path

from diagnose.scripts.profile import file_hash
from evaluate.scripts import compare_trials, validate_run


WORK_FIELDS = (
    "prefill_tokens", "decode_tokens", "draft_tokens", "accepted_draft_tokens",
    "recomputed_tokens", "prefix_hit_tokens", "num_preemptions",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def source_index(observations):
    index = {}
    sources = observations["sources"]
    require(isinstance(sources, list), "sources must be a list of file references")
    for ref in sources:
        require(isinstance(ref, dict) and validate_run.nonempty(ref.get("path")),
                "each source needs a file path")
        path = Path(ref["path"])
        require(path.is_absolute(), "source path must be absolute: " + str(path))
        name = str(path.resolve(strict=True))
        require(name not in index, "source recorded twice: " + name)
        require(file_hash(path) == ref.get("sha256"), "source fingerprint mismatch: " + name)
        index[name] = ref["sha256"]
    return index


def source_location(value, index):
    require(isinstance(value, dict) and validate_run.nonempty(value.get("path")),
            "missing source location")
    name = value["path"]
    # Most locations come from already resolved paths. Avoid filesystem calls per event.
    if name not in index:
        name = str(Path(name).resolve(strict=True))
    require(name in index, "source location is outside the fingerprinted inventory: " + name)
    if "line" in value:
        require(validate_run.integer(value["line"], 1), "source line numbers start at 1")
    return dict(value, path=name)


def step_key(row):
    require(isinstance(row, dict), "step observation must be an object")
    replica = row.get("replica")
    step = row.get("step_id")
    require(validate_run.nonempty(replica) and validate_run.nonempty(step),
            "replica and step_id must be nonempty strings")
    return replica, step


def make_steps(observations, index):
    steps = {}
    for entry in observations["inventory"]:
        key = step_key(entry)
        require(key not in steps, "duplicate logical step in inventory: " + str(key))
        require(validate_run.nonempty(entry.get("owner_rank")), "step owner_rank must be explicit")
        duration = entry.get("duration_us")
        require(duration is None or (validate_run.number(duration) and duration >= 0),
                "step duration_us must be finite and nonnegative")
        steps[key] = {
            "replica": key[0], "step_id": key[1], "owner_rank": entry["owner_rank"],
            "duration_us": duration, "source": source_location(entry.get("source"), index),
            "effective_output_tokens_mapped": 0, "output_ranges": [], "work": [],
        }
    return steps


def observation_target(row, steps, requests):
    key = step_key(row)
    require(key in steps, "observation references an absent logical step: " + str(key))
    step = steps[key]
    require(row.get("owner_rank", step["owner_rank"]) == step["owner_rank"],
            "observation rank differs from the logical step owner: " + str(key))
    req = row.get("request_id")
    require(validate_run.nonempty(req) and req in requests, "unknown benchmark request_id: " + str(req))
    return step, req


def attach_work(observations, steps, requests, index):
    # A row describes work for one logical request in one step. Summing TP copies
    # or two counters with the same name and different scopes would double count.
    seen = set()
    for row in observations["work"]:
        step, req = observation_target(row, steps, requests)
        key = (step["replica"], step["step_id"], req)
        require(key not in seen, "duplicate request participation: " + str(key))
        seen.add(key)
        item = {"request_id": req, "source": source_location(row.get("source"), index)}
        for field in WORK_FIELDS:
            if row.get(field) is not None:
                require(validate_run.integer(row[field]), "invalid work count: " + field)
                item[field] = row[field]
        step["work"].append(item)


def attach_output(observations, steps, requests, index):
    ranges_by_request = defaultdict(list)
    for row in observations["outputs"]:
        step, req = observation_target(row, steps, requests)
        start = row.get("output_start")
        count = row.get("output_count")
        require(validate_run.integer(start) and validate_run.integer(count, 1),
                "output_start must be nonnegative and output_count positive")
        end = start + count
        request = requests[req]
        successful = request["final_status"] == "success"
        if successful:
            require(end <= request["actual_output_tokens"],
                    "output range exceeds successful final usage: " + req)
            step["effective_output_tokens_mapped"] += count
        ranges_by_request[req].append((start, end))
        step["output_ranges"].append({
            "request_id": req, "output_start": start, "output_count": count,
            "counted_in_N": successful, "source": source_location(row.get("source"), index),
        })

    # Intervals, rather than individual token objects, bound memory by export rows.
    mapped = {}
    for req, spans in ranges_by_request.items():
        previous_end = 0
        total = 0
        for start, end in sorted(spans):
            require(start >= previous_end, "overlapping or duplicate output positions: " + req)
            total += end - start
            previous_end = end
        mapped[req] = total
    return mapped


def finish_steps(steps):
    for step in steps.values():
        rows = step["work"]
        sums = {}
        reporting = {}
        for field in WORK_FIELDS:
            values = [row[field] for row in rows if field in row]
            sums[field] = sum(values) if values else None
            reporting[field] = len(values)
        step["work_observed_sums"] = sums
        step["work_reporting_rows"] = reporting
        step["observed_request_ids"] = sorted(
            {row["request_id"] for row in rows} | {row["request_id"] for row in step["output_ranges"]})


def checked_requests(run_dir, validated):
    requests = validate_run.read_verified_requests(run_dir, validated)
    return {request["request_id"]: request for request in requests}


def request_results(requests, mapped):
    rows = []
    for line, (req, request) in enumerate(requests.items(), 1):
        success = request["final_status"] == "success"
        observed = mapped.get(req, 0)
        usage = request["actual_output_tokens"]
        rows.append({
            "request_id": req, "requests_jsonl_line": line, "final_status": request["final_status"],
            "finish_reason": request["finish_reason"], "actual_output_tokens": usage,
            "requested_max_tokens": request["requested_max_tokens"],
            "counted_output_tokens": usage if success else 0,
            "observed_output_positions": observed, "mapped_effective_tokens": observed if success else 0,
            "unmapped_effective_tokens": usage - observed if success else 0,
        })
    return rows


def check_inventory(observations, steps, index, issues):
    expected = observations.get("expected_steps_by_replica")
    evidence = observations.get("inventory_evidence")
    observed = Counter(replica for replica, _ in steps)
    if observations["scope"] != "full_run":
        issues.append("step inventory does not cover the full diagnostic benchmark window")
    if expected is not None:
        require(isinstance(expected, dict) and expected and
                all(validate_run.nonempty(k) and validate_run.integer(v) for k, v in expected.items()),
                "expected_steps_by_replica must map every replica to an independently observed count")
        require(all(k in expected and observed[k] <= expected[k] for k in observed),
                "observed inventory exceeds the independent step count or contains another replica")
        if any(observed[k] < value for k, value in expected.items()):
            issues.append("observed logical step counts are below the independent replica totals")
    if evidence is not None:
        evidence = source_location(evidence, index)
        # Independent summary/tally can live in the same export with a distinct locator.
        for step in steps.values():
            inventory_row = step["source"]
            if inventory_row["path"] == evidence["path"]:
                require(inventory_row.get("line") and evidence.get("line")
                        and inventory_row["line"] != evidence["line"],
                        "the step inventory cannot supply its own independent count evidence")
    if expected is None or evidence is None:
        issues.append("independent full-window step counts or their source are unavailable")
    if not steps:
        issues.append("no logical steps observed")
    return dict(observed), expected, evidence


def build(run_dir, observations=None):
    """Build a diagnostic report. Gaps are reportable; contradictory evidence raises ValueError."""
    run_dir = Path(run_dir).resolve(strict=True)
    validated = validate_run.inspect_run(run_dir)
    require(validated["valid"], "benchmark validation failed: " + "; ".join(validated["errors"]))
    manifest = validated["manifest"]
    require(manifest["run_purpose"] == "diagnostic" and manifest["profiler_enabled"] is True,
            "step ledger requires a profiled diagnostic benchmark run")
    requests = checked_requests(run_dir, validated)
    manifest_sha = validated["source_hashes"]["manifest.json"]
    if observations is None:
        observations = {
            "schema_version": 1, "run_id": manifest["run_id"], "manifest_sha256": manifest_sha,
            "step_definition": None, "scope": "partial", "sources": [], "inventory": [],
            "outputs": [], "work": [], "unresolved": ["no engine observation source supplied"],
        }
    require(isinstance(observations, dict) and type(observations.get("schema_version")) is int
            and observations["schema_version"] == 1, "unsupported step observation schema")
    require(observations.get("run_id") == manifest["run_id"]
            and observations.get("manifest_sha256") == manifest_sha,
            "step observations belong to another run or manifest")
    require(observations.get("scope") in ("full_run", "partial"), "unknown step window scope")
    for key in ("sources", "inventory", "outputs", "work", "unresolved"):
        require(isinstance(observations.get(key), list), key + " must be a list")
    require(all(validate_run.nonempty(msg) for msg in observations["unresolved"]),
            "unresolved observations must be strings")
    require(not observations["inventory"] or validate_run.nonempty(observations.get("step_definition")),
            "a logical step definition is required when steps are observed")

    index = source_index(observations)
    steps = make_steps(observations, index)
    attach_work(observations, steps, requests, index)
    mapped = attach_output(observations, steps, requests, index)
    finish_steps(steps)
    rows = request_results(requests, mapped)
    metrics = dict(validated["metrics"])
    n_tokens, wall = metrics["effective_output_tokens"], metrics["wall_s"]
    mapped_tokens = sum(row["mapped_effective_tokens"] for row in rows)
    issues = []
    observed, expected, evidence = check_inventory(observations, steps, index, issues)
    if mapped_tokens != n_tokens:
        issues.append("some successful final output positions have no logical step attribution")
    complete = not issues
    count = len(steps) if complete else None
    metrics.update(N=n_tokens, T=wall, K=count, R=count / wall if complete else None,
                   Y_bar=n_tokens / count if complete else None)
    lengths = Counter(row["counted_output_tokens"] for row in rows if row["final_status"] == "success")
    identity = {k: manifest.get(k) for k in (*compare_trials.IDENTITIES, "scenario", "config_sha256",
                                            "implementation_sha256", "measurement_mode", "base_url",
                                            "profiler_enabled", "run_purpose")}
    identity["model"] = manifest["client_measurement_policy"].get("model")
    return {
        "schema_version": 1, "run_id": manifest["run_id"], "manifest_sha256": manifest_sha,
        "identity": identity, "scope": observations["scope"], "step_definition": observations.get("step_definition"),
        "metrics": metrics, "requests": rows, "steps": list(steps.values()),
        "window": {k: manifest[k] for k in ("clock", "start_ts", "end_ts")},
        "benchmark_sources": [{"path": str(run_dir / name), "sha256": digest}
                              for name, digest in validated["source_hashes"].items()],
        "sources": observations["sources"],
        "coverage": {
            "status": "complete" if complete else ("partial" if steps else "unavailable"),
            "mapped_output_tokens": mapped_tokens, "unmapped_output_tokens": n_tokens - mapped_tokens,
            "token_fraction": mapped_tokens / n_tokens if n_tokens else None,
            "observed_steps": len(steps), "observed_steps_by_replica": observed,
            "expected_steps_by_replica": expected, "inventory_evidence": evidence,
            "issues": issues,
        },
        "finish_reasons": dict(Counter(row["finish_reason"] for row in rows if row["final_status"] == "success")),
        "terminal_statuses": dict(Counter(row["final_status"] for row in rows)),
        "length_distribution": [{"output_tokens": length, "successful_requests": frequency}
                                for length, frequency in sorted(lengths.items())],
        "quality_status": manifest["quality_status"], "validator_warnings": validated["warnings"],
        "native_observations": observations.get("native_observations", []),
        "unresolved": observations["unresolved"] + issues,
    }

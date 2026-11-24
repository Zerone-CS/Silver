#!/usr/bin/env python3
"""Rebuild a paired round decision from immutable collector originals."""
import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from evaluate.scripts import benchmark, compare_trials, validate_run
from evaluate.scripts.replay import verify_stream

ROUND_POLICIES = ("quality_policy", "goodput_policy")
GOODPUT_SEMANTICS = {
    "latency_origin": ("send_start", "planned_arrival"),
    "first_output_channel": ("any", "content", "reasoning_content"),
    "token_source": ("server_completion_tokens", "server_content_tokens"),
}
REQUEST_GOODPUT = "qualified_requests_per_s"

def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def record_file(index, path, expected=None):
    path = Path(path).resolve(strict=True)
    actual = file_hash(path)
    if expected is not None:
        require(actual == expected, "file hash differs: " + str(path))
    previous = index.setdefault(str(path), actual)
    require(previous == actual, "file changed during assessment: " + str(path))
    return path


def local_path(path):
    return Path(path).resolve(strict=True)


def source_file(index, ref, base, locate=local_path):
    require(isinstance(ref, dict), "missing artifact reference")
    value = compare_trials.text(ref.get("path"), "artifact.path")
    expected = compare_trials.digest(ref.get("sha256"), "artifact.sha256")
    return record_file(index, locate(base / value), expected.lower())


def read_object(path):
    value, _ = compare_trials.read_json(path)
    return compare_trials.obj(value, str(path))


def identity_of(manifest):
    return {name: manifest.get(name) for name in ("config_sha256", "implementation_sha256")}


def case_of(manifest):
    return {key: manifest[key] for key in compare_trials.IDENTITIES + ("measurement_mode",)}


def check_observation(path, identity, *, base_url=None, model=None):
    data = read_object(path)
    compare_trials.version(data, "deployment observation")
    for key in ("instance_id", "base_url", "model"):
        compare_trials.text(data.get(key), "observed." + key)
    config = compare_trials.obj(data.get("effective_config"), "observed.effective_config")
    require(config and validate_run.canonical_hash(config) == identity["config_sha256"],
            "observed effective config differs from planned config")
    require("implementation_sha256" in data and
            data["implementation_sha256"] == identity.get("implementation_sha256"),
            "observed implementation differs from planned implementation")
    require(base_url is None or data["base_url"] == base_url, "observed endpoint mismatch")
    require(model is None or data["model"] == model, "observed served model mismatch")
    return data


def check_attachments(report_path, document, sealed, index, locate=local_path):
    declared = document.get("evidence")
    require(document.get("verified") is True and isinstance(declared, list) and declared
            and isinstance(sealed, list) and len(declared) == len(sealed),
            "verification report lacks sealed observations")
    files = []
    for name, ref in zip(declared, sealed):
        name = compare_trials.text(name, "evidence path")
        path = source_file(index, ref, report_path.parent, locate)
        require(path == locate(report_path.parent / name), "evidence list differs from sealed metadata")
        require(path.stat().st_size > 0, "empty deployment evidence: " + str(path))
        files.append(path)
    return files


def warmup_overlap(warm, measured, rule):
    # Count measured prompts whose first N Unicode characters occurred in warmup.
    # Tokenizer boundaries and actual cache hits are separate observables.
    length = rule["characters"]
    prefixes = {job["prompt_text"][:length] for job in warm if len(job["prompt_text"]) >= length}
    observed = sum(job["prompt_text"][:length] in prefixes for job in measured)
    require(observed == rule["matching_requests"], "warmup shared prefix count differs from contract")
    return {"characters": length, "matching_requests": observed, "measured_requests": len(measured)}


def verify_site_inputs(run, inputs, observation, details, artifacts, index):
    environment = inputs["environment"]
    snapshot_id = environment.get("model_snapshot_sha256")
    policy = inputs["metric_contract"].get("site_warmup_policy")
    if snapshot_id is None and policy is None and "model_snapshot_sha256" not in details:
        return  # Older and non-site runs use their original evidence contract.
    require(policy is not None and snapshot_id is not None and snapshot_id == details.get("model_snapshot_sha256"),
            "site observation and run environment disagree on model snapshot")
    from scripts.site_model_snapshot import content_id
    receipt = observation.parent / "model-snapshot.json"
    require(receipt in artifacts, "site model snapshot receipt is missing")
    snapshot = read_object(receipt)
    hashes = environment.get("model_hashes")
    require(isinstance(hashes, dict) and hashes and snapshot.get("files_sha256") == hashes
            and snapshot.get("content_sha256") == content_id(hashes) == snapshot_id,
            "site model snapshot receipt disagrees with environment")
    receipt = observation.parent / "warmup-receipt.json"
    require(receipt in artifacts, "site warmup receipt is missing")
    warmup = read_object(receipt)
    results = warmup.get("round_results")
    require(warmup.get("policy") == policy and isinstance(results, list) and len(results) == policy["rounds"],
            "site warmup receipt differs from measurement contract")
    if policy["mode"] == "no_workload_warmup":
        require(policy["rounds"] == 0, "unexpected workload warmup requests")
        return
    source = observation.parent / "warmup-input.jsonl"
    require(source in artifacts and index[str(source)] == policy["dataset_sha256"],
            "site warmup dataset differs from frozen bytes")
    with source.open(encoding="utf-8") as stream:
        warm = [benchmark.strict_json(line) for line in stream if line.strip()]
    if "shared_prefix" in policy:
        measured, _ = compare_trials.read_json(run / "workload.json")
        require(warmup_overlap(warm, measured, policy["shared_prefix"]) == policy["shared_prefix"],
                "reported warmup overlap differs from measured workload")
    selected = [job["request_id"] for job in warm]
    require(policy["mode"] in ("explicit_dataset", "measured_first_concurrency") and policy["rounds"] >= 1,
            "invalid site warmup mode or number of rounds")
    if policy["mode"] == "measured_first_concurrency":
        measured, _ = compare_trials.read_json(run / "workload.json")
        require(measured == warm, "measured workload changed after selecting warmup requests")
        selected = selected[:policy["concurrency"]]
    require(selected and all([row["request_id"] for row in batch] == selected for batch in results),
            "site warmup responses do not match the frozen request schedule")
    require(all(row.get("response_id") and row.get("finish_reason") in ("length", "stop")
                for batch in results for row in batch), "site warmup has an incomplete response")


def verify_deployment(run, manifest, identity, index, locate=local_path):
    metadata = read_object(run / "metadata.json")
    sources = compare_trials.obj(metadata.get("source_inputs"), "metadata.source_inputs")
    inputs = {}
    for name in ("config", "environment", "metric_contract"):
        path = source_file(index, sources.get(name), run, locate)
        inputs[name] = read_object(path)
        require(validate_run.canonical_hash(inputs[name]) == manifest[name + "_sha256"],
                name + " source content differs from run identity")
    config_document = source_file(index, sources.get("config_evidence"), run, locate)
    evidence = read_object(config_document)
    artifacts = check_attachments(config_document, evidence, metadata.get("config_evidence_artifacts"), index, locate)
    observation = locate(config_document.parent / compare_trials.text(evidence.get("observation"), "observation"))
    require(observation in artifacts and any(not f.samefile(observation) for f in artifacts),
            "expected separate observed config snapshot and supporting service evidence")
    details = check_observation(observation, identity, base_url=manifest["base_url"],
                                model=manifest["client_measurement_policy"]["model"])
    verify_site_inputs(run, inputs, observation, details, artifacts, index)
    if identity.get("implementation_sha256"):
        require(manifest.get("implementation_verified") is True, "running implementation lacks verified evidence")
        impl = metadata["implementation_identity"]
        document = read_object(source_file(index, impl["source"], run, locate))
        require(document == impl["document"], "implementation source differs from manifest")
        proof = compare_trials.obj(metadata.get("implementation_evidence"), "implementation evidence")
        path = source_file(index, proof["source"], run, locate)
        receipt = read_object(path)
        require(receipt.get("implementation_sha256") == identity["implementation_sha256"],
                "implementation evidence cites another build")
        check_attachments(path, receipt, proof.get("artifacts"), index, locate)
    if "quality_report" in sources:
        doc = read_object(source_file(index, sources["quality_report"], run, locate))
        require(doc.get("status") == metadata.get("quality_status") and
                doc.get("method") == metadata.get("quality_method") and
                doc.get("scope") == metadata.get("quality_scope"), "quality declaration differs from source")
    return details


def quality_rules(contract_path, contract, index, locate=local_path):
    policies = compare_trials.obj(contract.get("quality_policy"), "contract.quality_policy").get("scenarios")
    require(isinstance(policies, dict) and set(policies) == set(contract["scenarios"]),
            "quality policy must cover every declared scenario")
    result = {}
    for scenario, policy in policies.items():
        compare_trials.obj(policy, scenario + ".quality")
        method = policy.get("method")
        require(method in ("exact_match", "exact_match_rate", "python_score_v1"), "unsupported quality method")
        if method == "python_score_v1":
            required = {"method", "reference", "scorer", "timeout_s", "min_mean_score"}
            require(set(policy) == required, "unexpected or missing Python scorer rule fields")
            minimum = compare_trials.number(policy["min_mean_score"], "min_mean_score", 0, 1)
            require(compare_trials.number(policy["timeout_s"], "timeout_s") > 0, "scorer timeout must be positive")
            scorer = source_file(index, policy["scorer"], contract_path.parent, locate)
            require(scorer.suffix == ".py", "scorer must reference a Python source file")
            policy = {**policy, "scorer": {"path": str(scorer), "sha256": index[str(scorer)]}}
        else:
            required = {"method", "field", "reference"}
            if method == "exact_match_rate":
                required.add("min_fraction")
            require(set(policy) == required, "unexpected or missing quality rule fields")
            minimum = compare_trials.number(policy.get("min_fraction", 1.0), "min_fraction", 0, 1)
            require(policy["field"] in ("content", "reasoning_content"), "unsupported quality output field")
        require(minimum > 0, "quality minimum must be positive")
        ref = source_file(index, policy["reference"], contract_path.parent, locate)
        gold = read_object(ref)
        require(gold and all(k.strip() for k in gold), "reference must be a nonempty request_id map")
        if method != "python_score_v1":
            require(all(isinstance(v, str) for v in gold.values()), "exact-match reference must contain strings")
        result[scenario] = (policy, minimum, gold)
    return result


def validate_policy(contract_path, locate=local_path):
    path = locate(contract_path)
    contract = read_object(path)
    compare_trials.validate_contract({k: v for k, v in contract.items() if k not in ROUND_POLICIES})
    index = {}
    goodput_rules(contract, quality_rules(path, contract, index, locate))
    return [{"path": name, "sha256": sha} for name, sha in sorted(index.items())]


def goodput_rules(contract, quality_by_scenario):
    """Validate request-level SLOs before any metrics are used for promotion."""
    policies = {}
    if "goodput_policy" in contract:
        policy = compare_trials.obj(contract["goodput_policy"], "contract.goodput_policy")
        compare_trials.keys(policy, ("scenarios",), "contract.goodput_policy")
        policies = compare_trials.obj(policy["scenarios"], "contract.goodput_policy.scenarios")
        require(policies and policies.keys() <= contract["scenarios"].keys(),
                "goodput_policy must cover at least one declared scenario and no unknown scenarios")
    for name, spec in contract["scenarios"].items():
        gates = [spec.get("primary"), spec.get("goodput_primary"), *spec["guardrails"]]
        if any(rule and rule["metric"] in (compare_trials.GOODPUT_METRIC, REQUEST_GOODPUT) for rule in gates):
            require(name in policies, "goodput metric requires goodput_policy for scenario " + name)
    parsed = {}
    for name, policy in policies.items():
        where = "contract.goodput_policy.scenarios." + name
        fields = ("max_ttft_s", "max_e2e_s")
        if quality_by_scenario[name][0]["method"] == "python_score_v1":
            fields += ("min_request_score",)
        compare_trials.obj(policy, where)
        compare_trials.keys(policy, (*fields, *(key for key in GOODPUT_SEMANTICS if key in policy)), where)
        parsed[name] = {key: compare_trials.number(policy[key], where + "." + key, 0) for key in fields[:2]}
        for key, choices in GOODPUT_SEMANTICS.items():
            if key in policy:
                require(policy[key] in choices, where + "." + key + " must be one of " + ", ".join(choices))
                parsed[name][key] = policy[key]
        # Request goodput is an explicit opt-in even for legacy collectors.
        spec = contract["scenarios"][name]
        if any(r and r["metric"] == REQUEST_GOODPUT for r in (spec.get("primary"), *spec["guardrails"])):
            parsed[name].setdefault("token_source", GOODPUT_SEMANTICS["token_source"][0])
        if "min_request_score" in policy:
            minimum = compare_trials.number(policy["min_request_score"], where + ".min_request_score", 0, 1)
            require(minimum > 0, where + ".min_request_score must be positive")
            parsed[name]["min_request_score"] = minimum
    return parsed


def external_scores(policy, items, index):
    # Run only the exact program bytes pinned in the operator's contract. The
    # scorer gets sample data on stdin, without timing, side or deployment labels.
    ref = policy["scorer"]
    source = Path(ref["path"]).read_bytes()
    require(hashlib.sha256(source).hexdigest() == ref["sha256"], "scorer source changed")
    index[str(Path(ref["path"]).resolve())] = ref["sha256"]
    executable = record_file(index, sys.executable)
    payload = benchmark.canonical_bytes({"schema_version": 1, "items": items})
    with TemporaryDirectory(prefix="round-score-") as directory:
        directory = Path(directory)
        script, output = directory / "scorer.py", directory / "scores.json"
        script.write_bytes(source)
        input_path, errors = directory / "samples.json", directory / "stderr.log"
        input_path.write_bytes(payload)
        with input_path.open("rb") as inp, output.open("wb") as out, errors.open("wb") as err:
            with subprocess.Popen([sys.executable, "-I", "-B", str(script)], stdin=inp, stdout=out, stderr=err,
                                  cwd=directory, start_new_session=True) as process:
                try:
                    code = process.wait(timeout=policy["timeout_s"])
                except BaseException as exc:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    if isinstance(exc, subprocess.TimeoutExpired):
                        raise ValueError("quality scorer timed out") from exc
                    raise
        if code:
            with errors.open("rb") as err:
                detail = err.read(1024).decode("utf-8", errors="replace").strip()
            raise ValueError("quality scorer exited with code {}: {}".format(code, detail))
        response, response_sha = compare_trials.read_json(output)
    compare_trials.obj(response, "scorer response")
    compare_trials.version(response, "scorer response")
    require(set(response) == {"schema_version", "scores"}, "unexpected scorer response fields")
    scores = compare_trials.obj(response["scores"], "per-request scorer scores")
    require(set(scores) == {item["request_id"] for item in items}, "scorer result IDs differ from measured requests")
    for item in items:
        key = item["request_id"]
        scores[key] = compare_trials.number(scores[key], "score for " + key, 0, 1)
        require(item["status"] == "success" or scores[key] == 0, "scorer gave credit to a failed request: " + key)
    return {"scores": scores, "mean_score": math.fsum(scores.values()) / len(items),
            "scorer": ref, "python": {"path": str(executable), "sha256": index[str(executable)],
                                     "version": sys.version}, "scorer_response_sha256": response_sha,
            "scorer_input_sha256": hashlib.sha256(payload).hexdigest(),
            "item_sha256": {item["request_id"]: validate_run.canonical_hash(item) for item in items}}


def score_run(run, validated, rule, index, *, stream_facts=None):
    run = Path(run).resolve(strict=True)
    manifest = validated["manifest"]
    workload, workload_sha = compare_trials.read_json(run / "workload.json")
    require(workload_sha == validated["source_hashes"]["workload.json"], "workload changed after verification")
    policy, minimum, gold = rule
    require(set(gold) == {job["request_id"] for job in workload}, "gold must cover exactly the measured workload")
    jobs = {job["request_id"]: (i, job) for i, job in enumerate(workload)}
    args = SimpleNamespace(**manifest["client_measurement_policy"])
    wrong_ids = []
    scorer_items = {}
    external = policy["method"] == "python_score_v1"
    for row in validate_run.read_verified_requests(run, validated):
        request_id = row["request_id"]
        i, job = jobs[request_id]
        raw = {}
        for key, suffix in (("raw_request_path", "request.json"), ("raw_response_path", "sse"),
                            ("output_path", "output.json")):
            relative = "raw/%08d.%s" % (i, suffix)
            require(row.get(key) == relative, "raw file does not belong to request: " + request_id)
            path = (run / relative).resolve(strict=True)
            path.relative_to(run)
            raw[key] = record_file(index, path)
        body = read_object(raw["raw_request_path"])
        require(body == benchmark.request_payload(job, args), "raw request differs from workload or client policy")
        output = read_object(raw["output_path"])
        if row["final_status"] == "success":
            require(row.get("http_status") == 200, "successful request lacks recorded HTTP 200")
            facts = verify_stream(raw["raw_response_path"], row, output)
            if stream_facts is not None:
                stream_facts[request_id] = facts
        if external:
            scorer_items[request_id] = {
                "request_id": request_id, "prompt_text": job["prompt_text"], "reference": gold[request_id],
                "status": row["final_status"], "output": output if row["final_status"] == "success" else None}
        elif row["final_status"] != "success" or output[policy["field"]] != gold[request_id]:
            wrong_ids.append(request_id)
    result = {"run_id": manifest["run_id"], **identity_of(manifest), "n": len(jobs),
              "reference_sha256": policy["reference"]["sha256"], "method": policy["method"]}
    if external:
        # Workload order makes the exact scorer input reproducible from original files.
        items = [scorer_items[job["request_id"]] for job in workload]
        result.update(external_scores(policy, items, index), min_mean_score=minimum, run_path=str(run))
        observed = result["mean_score"]
    else:
        observed = (len(jobs) - len(wrong_ids)) / len(jobs)
        result.update(mismatch_or_failure_ids=wrong_ids, fraction=observed, field=policy["field"], min_fraction=minimum)
    result["passed"] = observed >= minimum and manifest["quality_status"] != "fail"
    return result


def diagnostic_stream_facts(run, validated, index):
    """Reconstruct channel evidence from the same originals when no scorer is requested."""
    workload, digest = compare_trials.read_json(run / "workload.json")
    require(digest == validated["source_hashes"]["workload.json"], "workload changed after verification")
    requests = {row["request_id"]: row for row in validate_run.read_verified_requests(run, validated)}
    facts = {}
    for i, job in enumerate(workload):
        key = job["request_id"]
        row = requests[key]
        if row["final_status"] == "success":
            stream = record_file(index, run / ("raw/%08d.sse" % i))
            output = record_file(index, run / ("raw/%08d.output.json" % i))
            facts[key] = verify_stream(stream, row, read_object(output))
    return facts


def measure_goodput(run, validated, thresholds, quality, *, stream_facts=None):
    """Count final output from requests satisfying every supplied requirement."""
    scores = quality.get("scores") if quality is not None else None
    wrong = set(quality.get("mismatch_or_failure_ids", ())) if quality is not None else set()
    minimum = thresholds.get("min_request_score")
    if scores is not None:
        require(minimum is not None, "declare min_request_score for individual-request goodput with python_score_v1")
    else:
        require(minimum is None, "min_request_score requires a graded per-request scorer")
    ttft, e2e = thresholds.get("max_ttft_s"), thresholds.get("max_e2e_s")
    origin = thresholds.get("latency_origin", "send_start")
    channel = thresholds.get("first_output_channel", "any")
    source = thresholds.get("token_source", "server_completion_tokens")
    policy = validated.get("manifest", {}).get("client_measurement_policy", {})
    if origin == "planned_arrival":
        require(policy.get("mode") == "open_loop", "planned_arrival requires a verified open-loop arrival schedule")
    if stream_facts is None and (channel != "any" or source == "server_content_tokens"):
        stream_facts = diagnostic_stream_facts(Path(run), validated, {})
    excluded = {reason: [] for reason in ("failed", "quality", "ttft", "e2e")}
    qualified = []
    missing_content_counts = []
    server_tokens = content_tokens = 0
    for request in validate_run.read_verified_requests(run, validated):
        key = request["request_id"]
        if request["final_status"] != "success":
            excluded["failed"].append(key)
            continue
        quality_failed = key in wrong or (scores is not None and scores[key] < minimum)
        first = request["first_output_ts"]
        if channel != "any":
            event = stream_facts[key]["first_channel_event"].get(channel)
            first = None if event is None else request["output_event_ts"][event]
        start = request["planned_send_ts"] if origin == "planned_arrival" else request["send_start_ts"]
        late_first = ttft is not None and (first is None or first - start > ttft)
        late_end = e2e is not None and request["end_ts"] - start > e2e
        for reason, failed in (("quality", quality_failed), ("ttft", late_first), ("e2e", late_end)):
            if failed:
                excluded[reason].append(key)
        if quality_failed or late_first or late_end:
            continue
        qualified.append(key)
        server_tokens += request["actual_output_tokens"]
        if source == "server_content_tokens":
            content = stream_facts[key]["server_content_tokens"]
            if content is None:
                missing_content_counts.append(key)
            else:
                content_tokens += content
    wall = validated["metrics"]["wall_s"]
    count = server_tokens if source == "server_completion_tokens" else content_tokens
    if missing_content_counts:
        count = None
    result = {"thresholds": thresholds, "qualifying_request_ids": qualified, "excluded_request_ids": excluded,
              "qualifying_output_tokens": count, "wall_s": wall,
              "output_tokens_per_s": count / wall if count is not None else None}
    if policy.get("channel_evidence_version") == 1 or any(key in thresholds for key in GOODPUT_SEMANTICS):
        result.update(latency_origin=origin, first_output_channel=channel, token_source=source,
                      qualified_requests_per_s=len(qualified) / wall,
                      qualified_server_completion_tokens_per_s=server_tokens / wall,
                      missing_content_token_request_ids=missing_content_counts)
    return result


def goodput_metrics(report):
    metrics = {compare_trials.GOODPUT_METRIC: report["output_tokens_per_s"]}
    if REQUEST_GOODPUT in report:
        metrics[REQUEST_GOODPUT] = report[REQUEST_GOODPUT]
    return metrics


def native_contract(contract, baseline, candidate):
    result = {k: v for k, v in contract.items() if k not in ROUND_POLICIES}
    for side in (baseline, candidate):
        compare_trials.digest(side.get("config_sha256"), "planned config")
        if side.get("implementation_sha256") is not None:
            compare_trials.digest(side["implementation_sha256"], "planned implementation")
    require(baseline != candidate, "baseline and candidate must identify different treatments")
    pair = [side.get("implementation_sha256") for side in (baseline, candidate)]
    if pair[0] != pair[1]:
        require(all(pair) and baseline["config_sha256"] == candidate["config_sha256"],
                "native source comparison requires two distinct implementations and a fixed config")
        change = dict(zip(("baseline_sha256", "candidate_sha256"), pair))
        require(result.setdefault("implementation_change", change) == change, "implementation_change differs from plan")
    else:
        require("implementation_change" not in result, "implementation_change requires two distinct implementations")
    compare_trials.validate_contract(result)
    return result


def assess(contract_path, evidence_path, baseline_identity, candidate_identity, *, locate=local_path):
    try:
        return _assess(contract_path, evidence_path, baseline_identity, candidate_identity, locate)
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError("invalid or missing round evidence field: {}".format(exc)) from exc


def _assess(contract_path, evidence_path, baseline_identity, candidate_identity, locate):
    contract_path = locate(contract_path)
    evidence_path = locate(evidence_path)
    contract, contract_hash = compare_trials.read_json(contract_path)
    evidence, evidence_hash = compare_trials.read_json(evidence_path)
    native = native_contract(contract, baseline_identity, candidate_identity)
    compare_trials.validate_trial(evidence, native)
    index = {str(contract_path): contract_hash, str(evidence_path): evidence_hash}
    rules = quality_rules(contract_path, contract, index, locate)
    goodput_by_scenario = goodput_rules(contract, rules)
    cases, runs, locations, quality, goodput = {}, [], {}, {}, {}
    with TemporaryDirectory(prefix="round-recheck-") as scratch:
        scratch = Path(scratch).resolve()
        trial = {"schema_version": 1, "pairs": []}
        for i, pair in enumerate(evidence["pairs"]):
            scenario = pair["scenario"]
            rebuilt = {"scenario": scenario}
            for side, identity in (("baseline", baseline_identity), ("candidate", candidate_identity)):
                run = locate(evidence_path.parent / pair[side])
                require(run.is_dir(), "expected original run directory: " + str(run))
                validated = validate_run.inspect_run(run)
                require(validated["valid"], "invalid run {}: {}".format(run, validated["errors"]))
                manifest = validated["manifest"]
                require(manifest["scenario"] == scenario and identity_of(manifest) == identity,
                        "run belongs to another scenario or deployment: " + str(run))
                case = case_of(manifest)
                require(cases.setdefault(scenario, case) == case, "case or environment drift: " + scenario)
                for name, sha in validated["source_hashes"].items():
                    record_file(index, run / name, sha)
                deployment = verify_deployment(run, manifest, identity, index, locate)
                stream_facts = {}
                quality_result = score_run(run, validated, rules[scenario], index, stream_facts=stream_facts)
                quality[manifest["run_id"]] = quality_result
                if scenario in goodput_by_scenario:
                    report = measure_goodput(run, validated, goodput_by_scenario[scenario], quality_result,
                                             stream_facts=stream_facts)
                    goodput[manifest["run_id"]] = report
                    validated["metrics"].update(goodput_metrics(report))
                # The native comparator reads quality from the manifest. Give it the
                # post-run verdict in a derived in-memory copy; leave originals intact.
                manifest["quality_status"] = "pass" if quality_result["passed"] else "fail"
                if identity.get("implementation_sha256") and "implementation_change" not in native:
                    # The native configuration comparator accepts only its original
                    # schema. Both loaded implementations were verified against the
                    # same frozen hash above; retain their original evidence in runs.
                    for field in compare_trials.FIELDS:
                        manifest.pop(field, None)
                elif identity.get("implementation_sha256"):
                    manifest["quality_implementation_sha256"] = identity["implementation_sha256"]
                    manifest["implementation_quality_source"] = {
                        "path": str(run / "requests.jsonl"), "sha256": validated["source_hashes"]["requests.jsonl"]}
                name = "{}-{}.json".format(i, side)
                target = scratch / name
                target.write_text(json.dumps(validated, ensure_ascii=False, allow_nan=False), encoding="utf-8")
                rebuilt[side] = name
                locations[str(target)] = str(run)
                runs.append({"side": side, "scenario": scenario, "path": str(run),
                             "run_id": manifest["run_id"], "instance_id": deployment["instance_id"], "identity": identity,
                             "source_hashes": validated["source_hashes"]})
            trial["pairs"].append(rebuilt)
        contract_copy, trial_copy = scratch / "contract.json", scratch / "trial.json"
        contract_copy.write_text(json.dumps(native, ensure_ascii=False), encoding="utf-8")
        trial_copy.write_text(json.dumps(trial, ensure_ascii=False), encoding="utf-8")
        comparison, _ = compare_trials.compare(contract_copy, trial_copy)
        if baseline_identity.get("implementation_sha256") and "implementation_change" not in native:
            comparison["fixed_implementation_sha256"] = baseline_identity["implementation_sha256"]
        for data in comparison["validated_inputs"]:
            data["path"] = locations[data["path"]]
        for data in comparison["pairs"]:
            for key in ("baseline_path", "candidate_path"):
                data[key] = locations[data[key]]
    result = {"recommendation": comparison["recommendation"], "comparison": comparison,
            "fingerprints": {"contract_sha256": contract_hash, "evidence_sha256": evidence_hash,
                             "validator_sha256": file_hash(validate_run.__file__),
                             "comparator_sha256": file_hash(compare_trials.__file__),
                             "round_evidence_sha256": file_hash(__file__)},
            "case_identity": cases, "runs": runs, "quality": quality,
            "evidence_files": [{"path": path, "sha256": digest} for path, digest in sorted(index.items())],
            "run_ids": [r["run_id"] for r in runs],
            "requests_hashes": [r["source_hashes"]["requests.jsonl"] for r in runs]}
    if goodput:
        result["goodput"] = goodput
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    describe = sub.add_parser("describe-run", help="derive case and deployment identities from a complete run")
    describe.add_argument("run", type=Path)
    ref = sub.add_parser("artifact-ref", help="print a pinned file reference")
    ref.add_argument("file", type=Path)
    check = sub.add_parser("check", help="rebuild a round report from originals")
    for key in ("contract", "evidence", "baseline_identity", "candidate_identity"):
        check.add_argument(key, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "artifact-ref":
            result = {"path": str(args.file.resolve(strict=True)), "sha256": file_hash(args.file)}
        elif args.action == "describe-run":
            report = validate_run.inspect_run(args.run)
            require(report["valid"], str(report["errors"]))
            m = report["manifest"]
            result = {"identity": identity_of(m), "case_identity": {m["scenario"]: case_of(m)}}
        else:
            result = assess(args.contract, args.evidence, read_object(args.baseline_identity),
                            read_object(args.candidate_identity))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, benchmark.StreamError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("recommendation", "adopt") == "adopt" else 1


if __name__ == "__main__":
    raise SystemExit(main())

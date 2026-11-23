#!/usr/bin/env python3
"""Compare predeclared paired, independently validated finite-batch runs.

No service is contacted. Input files are read-only and the report is created
exclusively. Exit codes: 0 adopt, 1 defer/revert, 2 invalid input or I/O error.
"""

import argparse
import hashlib
import json
import math
import random
import re
import sys
from pathlib import Path

if __package__:
    from .implementation_identity import FIELDS, implementation_fields
else:
    if __name__ == "__main__":
        sys.dont_write_bytecode = True  # Standalone tools, including --help, leave the suite read-only.
    from implementation_identity import FIELDS, implementation_fields


IDENTITIES = (
    "workload_sha256", "environment_sha256", "metric_contract_sha256",
    "benchmark_sha256", "client_measurement_policy_sha256",
)
GOODPUT_METRIC = "goodput"
HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_RESAMPLE_DRAWS = 20_000_000
MAX_EFFECT_CELLS = 100_000


class InputError(ValueError):
    pass


def fail(message):
    raise InputError(message)


def reject_constant(value):
    fail("JSON contains non-finite number: " + value)


def unique_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            fail("Duplicate JSON object key: " + key)
        result[key] = value
    return result


def check_finite_tree(value, where="JSON"):
    # JSON permits exponents such as 1e999 that overflow Python floats.
    if isinstance(value, float) and not math.isfinite(value):
        fail(where + " contains a non-finite number")
    if isinstance(value, dict):
        for key, item in value.items():
            check_finite_tree(item, where + "." + key)
    elif isinstance(value, list):
        for item in value:
            check_finite_tree(item, where)


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        fail(str(path) + " exceeds the 32 MiB input limit")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant,
                           object_pairs_hook=unique_keys)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        fail("Cannot parse {}: {}".format(path, exc))
    check_finite_tree(value, str(path))
    return value, hashlib.sha256(raw).hexdigest()


def obj(value, where):
    if not isinstance(value, dict):
        fail(where + " must be a JSON object")
    return value


def version(value, where):
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        fail(where + ".schema_version must be integer 1")


def keys(value, required, where):
    missing = set(required) - set(value)
    extra = set(value) - set(required)
    if missing:
        fail(where + " missing fields: " + ", ".join(sorted(missing)))
    if extra:
        fail(where + " has unsupported fields: " + ", ".join(sorted(extra)))


def integer(value, where, low, high):
    if type(value) is not int or not low <= value <= high:
        fail("{} must be an integer in [{}, {}]".format(where, low, high))
    return value


def number(value, where, low=None, high=None):
    if type(value) not in (int, float):
        fail(where + " must be a finite number, not a boolean or string")
    try:
        value = float(value)
    except OverflowError:
        fail(where + " is outside finite numeric range")
    if not math.isfinite(value):
        fail(where + " must be finite")
    if low is not None and value < low or high is not None and value > high:
        fail("{} must be in [{}, {}]".format(where, low, high))
    return value


def text(value, where):
    if not isinstance(value, str) or not value.strip():
        fail(where + " must be a nonempty string")
    return value


def digest(value, where):
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        fail(where + " must be a 64-digit SHA-256 hex string")
    return value.lower()


def validate_metric_rule(rule, kind, where):
    obj(rule, where)
    if kind == "primary":
        keys(rule, ("metric", "direction", "min_relative_improvement"), where)
    else:
        allowed = {"metric", "direction", "max_relative_regression", "min_value", "max_value"}
        if set(rule) - allowed or "metric" not in rule:
            fail(where + " has missing or unsupported guardrail fields")
        if not any(key in rule for key in ("max_relative_regression", "min_value", "max_value")):
            fail(where + " must declare a relative or absolute guardrail limit")
        if ("direction" in rule) != ("max_relative_regression" in rule):
            fail(where + " requires direction and max_relative_regression together")
        for key in ("min_value", "max_value"):
            if key in rule:
                number(rule[key], where + "." + key, 0)
        if "min_value" in rule and "max_value" in rule and rule["min_value"] > rule["max_value"]:
            fail(where + ".min_value cannot exceed max_value")
    text(rule["metric"], where + ".metric")
    if "direction" in rule and rule["direction"] not in ("higher", "lower"):
        fail(where + ".direction must be higher or lower")
    threshold = "min_relative_improvement" if kind == "primary" else "max_relative_regression"
    if threshold in rule:
        number(rule[threshold], where + "." + threshold, 0, 100)


def validate_contract(contract):
    obj(contract, "contract")
    required = ("schema_version", "min_pairs", "confidence", "bootstrap_samples", "seed", "scenarios")
    if "implementation_change" in contract:
        required += ("implementation_change",)
        change = obj(contract["implementation_change"], "contract.implementation_change")
        keys(change, ("baseline_sha256", "candidate_sha256"), "contract.implementation_change")
        baseline = digest(change["baseline_sha256"], "implementation_change.baseline_sha256")
        candidate = digest(change["candidate_sha256"], "implementation_change.candidate_sha256")
        if baseline == candidate:
            fail("implementation_change must declare distinct baseline and candidate implementations")
    keys(contract, required, "contract")
    version(contract, "contract")
    integer(contract["min_pairs"], "contract.min_pairs", 2, 10_000)
    number(contract["confidence"], "contract.confidence", 0.80, 0.999)
    integer(contract["bootstrap_samples"], "contract.bootstrap_samples", 100, 100_000)
    integer(contract["seed"], "contract.seed", 0, 2**32 - 1)
    scenarios = obj(contract["scenarios"], "contract.scenarios")
    if not 1 <= len(scenarios) <= 100:
        fail("contract.scenarios must contain 1 to 100 scenarios")
    primary_count = 0
    for name, scenario in scenarios.items():
        text(name, "scenario name")
        where = "contract.scenarios." + name
        obj(scenario, where)
        fields = ("primary", "guardrails")
        if "goodput_primary" in scenario:
            fields += ("goodput_primary",)
        keys(scenario, fields, where)
        if scenario["primary"] is not None:
            validate_metric_rule(scenario["primary"], "primary", where + ".primary")
            primary_count += 1
        if "goodput_primary" in scenario:
            rule = scenario["goodput_primary"]
            validate_metric_rule(rule, "primary", where + ".goodput_primary")
            if rule["metric"] != GOODPUT_METRIC or rule["direction"] != "higher":
                fail(where + ".goodput_primary must require higher goodput")
            if (scenario["primary"] or {}).get("metric") == GOODPUT_METRIC:
                fail(where + " declares goodput twice as a primary rule")
            primary_count += 1
        if not isinstance(scenario["guardrails"], list) or len(scenario["guardrails"]) > 50:
            fail(where + ".guardrails must be an array of at most 50 rules")
        seen = set()
        for index, rule in enumerate(scenario["guardrails"]):
            validate_metric_rule(rule, "guardrail", where + ".guardrails[{}]".format(index))
            if rule["metric"] in seen:
                fail(where + ": duplicate guardrail metric " + rule["metric"])
            seen.add(rule["metric"])
        if scenario["primary"] is None and not scenario["guardrails"] and "goodput_primary" not in scenario:
            fail(where + " has neither a primary metric nor a guardrail")
    if not primary_count:
        fail("At least one scenario needs a primary metric to establish an optimization benefit")


def validate_trial(trial, contract):
    obj(trial, "trial")
    keys(trial, ("schema_version", "pairs"), "trial")
    version(trial, "trial")
    if not isinstance(trial["pairs"], list) or not 1 <= len(trial["pairs"]) <= 10_000:
        fail("trial.pairs must contain 1 to 10000 pairs")
    n_effects = 0
    for index, pair in enumerate(trial["pairs"]):
        where = "trial.pairs[{}]".format(index)
        obj(pair, where)
        keys(pair, ("scenario", "baseline", "candidate"), where)
        name = text(pair["scenario"], where + ".scenario")
        if name not in contract["scenarios"]:
            fail(where + ": scenario not declared in contract: " + name)
        text(pair["baseline"], where + ".baseline")
        text(pair["candidate"], where + ".candidate")
        spec = contract["scenarios"][name]
        n_effects += len(spec["guardrails"]) + (spec["primary"] is not None) + ("goodput_primary" in spec)
    if n_effects > MAX_EFFECT_CELLS:
        fail("Too many pair-by-metric effects; limit is {}".format(MAX_EFFECT_CELLS))
    if n_effects * contract["bootstrap_samples"] > MAX_RESAMPLE_DRAWS:
        fail("Bootstrap work exceeds {} effect draws; reduce pairs, metrics, or samples"
             .format(MAX_RESAMPLE_DRAWS))


def read_run(path, scenario, seen, input_index, side, change):
    resolved = Path(path).resolve(strict=True)
    stat = resolved.stat()
    physical_id = (stat.st_dev, stat.st_ino)
    if physical_id in seen["files"]:
        fail("One validated run file was reused, including a symlink/hardlink: " + str(path))
    seen["files"].add(physical_id)
    data, validated_sha = read_json(resolved)
    obj(data, str(resolved))
    version(data, str(resolved))
    if type(data.get("valid")) is not bool:
        fail(str(resolved) + ": valid must be a JSON boolean")
    for field in ("errors", "warnings"):
        if not isinstance(data.get(field), list) or any(not isinstance(s, str) for s in data[field]):
            fail(str(resolved) + ": " + field + " must be an array of strings")
    if data["valid"] and data["errors"]:
        fail(str(resolved) + ": valid=true contradicts nonempty errors")
    manifest = obj(data.get("manifest"), str(resolved) + ".manifest")
    metrics = obj(data.get("metrics"), str(resolved) + ".metrics")
    source_hashes = obj(data.get("source_hashes"), str(resolved) + ".source_hashes")
    result = {
        "path": str(resolved), "validated_file_sha256": validated_sha,
        "valid": data["valid"], "validator_errors": data["errors"],
        "validator_warnings": data["warnings"], "manifest": manifest,
        "source_hashes": source_hashes, "metrics": metrics,
        "eligibility_issues": [],
    }
    input_index.append(result)
    if change is None and any(key in manifest for key in FIELDS):
        fail("Run contains implementation fields without a declared implementation_change")
    if not data["valid"]:
        result["eligibility_issues"].append("validator reported valid=false")
        # An invalid run can legitimately have an incomplete manifest. It never
        # contributes to an effect estimate or a promotion decision.
        return result
    if change is not None:
        try:
            fields = implementation_fields(manifest)
        except ValueError as exc:
            fail("{}: {}".format(resolved, exc))
        if fields.get("implementation_sha256") != change[side + "_sha256"].lower():
            fail("{}: missing or mismatched {} implementation_sha256".format(resolved, side))
        if not fields["implementation_verified"]:
            result["eligibility_issues"].append("deployed implementation has not been verified")
    version(manifest, str(resolved) + ".manifest")
    run_id = text(manifest.get("run_id"), str(resolved) + ".manifest.run_id")
    if run_id in seen["run_ids"]:
        fail("run_id reused across pairs or scenarios: " + run_id)
    seen["run_ids"].add(run_id)
    if manifest.get("scenario") != scenario:
        fail(str(resolved) + ": manifest.scenario does not match pair scenario")
    for key in IDENTITIES + ("config_sha256",):
        digest(manifest.get(key), str(resolved) + ".manifest." + key)
    for key in ("manifest.json", "requests.jsonl", "workload.json", "metadata.json"):
        digest(source_hashes.get(key), str(resolved) + ".source_hashes." + key)
    request_hash = source_hashes["requests.jsonl"].lower()
    if request_hash in seen["requests_hashes"]:
        fail("Identical requests.jsonl reused as independent run: " + str(resolved))
    seen["requests_hashes"].add(request_hash)
    for key, value in source_hashes.items():
        digest(value, str(resolved) + ".source_hashes." + key)
    mode = text(manifest.get("measurement_mode"), str(resolved) + ".manifest.measurement_mode")
    if mode != "finite_batch":
        result["eligibility_issues"].append("unsupported measurement_mode: " + mode)
    if type(manifest.get("profiler_enabled")) is not bool:
        fail(str(resolved) + ": profiler_enabled must be a JSON boolean")
    if manifest["profiler_enabled"]:
        result["eligibility_issues"].append("profiler_enabled=true")
    if type(manifest.get("effective_config_verified")) is not bool:
        fail(str(resolved) + ": effective_config_verified must be a JSON boolean")
    if not manifest["effective_config_verified"]:
        result["eligibility_issues"].append("effective configuration has not been verified")
    if manifest.get("quality_status") not in ("pass", "fail", "not_evaluated"):
        fail(str(resolved) + ": unsupported quality_status")
    if manifest["quality_status"] != "pass":
        result["eligibility_issues"].append("quality_status=" + manifest["quality_status"])
    if manifest.get("run_purpose") not in ("performance", "diagnostic"):
        fail(str(resolved) + ": run_purpose must be performance or diagnostic")
    if manifest["run_purpose"] == "diagnostic":
        result["eligibility_issues"].append("run_purpose=diagnostic")
    for key in ("diagnostic", "diagnostic_only"):
        if key in manifest:
            if type(manifest[key]) is not bool:
                fail(str(resolved) + ": " + key + " must be a JSON boolean")
            if manifest[key]:
                result["eligibility_issues"].append(key + "=true")
    for key in ("run_kind", "purpose"):
        if key in manifest and manifest[key] == "diagnostic":
            result["eligibility_issues"].append(key + "=diagnostic")
    return result


def same_identities(baseline, candidate, scenario):
    for key in IDENTITIES + ("measurement_mode",):
        left = baseline["manifest"][key]
        right = candidate["manifest"][key]
        if key.endswith("sha256"):
            left, right = left.lower(), right.lower()
        if left != right:
            fail("{}: baseline/candidate {} mismatch".format(scenario, key))


def quantile(sorted_values, q):
    position = (len(sorted_values) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def stable_mean(values):
    # Division before summation avoids overflow of otherwise finite means.
    count = len(values)
    return math.fsum(value / count for value in values)


def strictly_above(value, boundary):
    """Do not interpret floating-point roundoff at a boundary as evidence."""
    return value > boundary and not math.isclose(value, boundary, rel_tol=1e-12, abs_tol=1e-15)


def bootstrap(values, samples, confidence, rng):
    count = len(values)
    estimates = []
    for _ in range(samples):
        estimates.append(stable_mean([values[rng.randrange(count)] for _ in range(count)]))
    estimates.sort()
    alpha = (1.0 - confidence) / 2.0
    return {"lower": quantile(estimates, alpha), "upper": quantile(estimates, 1.0 - alpha)}


def evaluate_rule(rule, kind, pairs, contract, scenario, rng):
    metric = rule["metric"]
    relative = kind == "primary" or "max_relative_regression" in rule
    result = {"metric": metric, "direction": rule.get("direction"), "kind": kind,
              "rule": rule, "paired_values": [], "missing_or_undefined": [],
              "status": "inconclusive", "observed_violations": []}
    effects = []
    defined_absolute_pairs = 0
    for pair in pairs:
        item = {"pair_index": pair["pair_index"],
                "baseline_run_id": pair["baseline"]["manifest"].get("run_id"),
                "candidate_run_id": pair["candidate"]["manifest"].get("run_id")}
        result["paired_values"].append(item)
        if pair["eligibility_issues"]:
            item["excluded_reasons"] = pair["eligibility_issues"]
            result["missing_or_undefined"].append({"pair_index": pair["pair_index"],
                                                   "reason": "pair is ineligible"})
            continue
        undefined = []
        for side in ("baseline", "candidate"):
            value = pair[side]["metrics"].get(metric)
            item[side] = value
            if value is None:
                if relative or side == "candidate":
                    undefined.append(side + " metric is missing or null")
            else:
                number(value, "{}.{}.{}".format(scenario, side, metric), 0)
        if kind == "guardrail" and item["candidate"] is not None:
            for bound, failed in (("min_value", lambda actual, limit: actual < limit),
                                  ("max_value", lambda actual, limit: actual > limit)):
                if bound in rule and failed(item["candidate"], rule[bound]):
                    result["observed_violations"].append({
                        "pair_index": pair["pair_index"], "constraint": bound,
                        "candidate_value": item["candidate"], "limit": rule[bound]})
        if undefined:
            result["missing_or_undefined"].append({"pair_index": pair["pair_index"],
                                                   "reason": "; ".join(undefined)})
            continue
        defined_absolute_pairs += 1
        if not relative:
            continue
        baseline, candidate = float(item["baseline"]), float(item["candidate"])
        if baseline == 0:
            result["missing_or_undefined"].append({"pair_index": pair["pair_index"],
                                                   "reason": "baseline is zero; relative effect is undefined"})
            continue
        # Positive effect always means improvement for primary, and regression
        # for guardrail. Lower primary: (baseline-candidate)/baseline.
        relative_change = candidate / baseline - 1.0
        effect = relative_change if rule["direction"] == "higher" else -relative_change
        if kind == "guardrail":
            effect = -effect
        if not math.isfinite(effect):
            result["missing_or_undefined"].append({"pair_index": pair["pair_index"],
                                                   "reason": "relative effect exceeds finite numeric range"})
            continue
        item["relative_improvement" if kind == "primary" else "relative_regression"] = effect
        effects.append(effect)
        if kind == "guardrail" and strictly_above(effect, rule["max_relative_regression"]):
            result["observed_violations"].append({"pair_index": pair["pair_index"],
                                                  "constraint": "max_relative_regression",
                                                  "relative_regression": effect})
    result["n_defined_pairs"] = len(effects) if relative else defined_absolute_pairs
    result["mean_relative_improvement" if kind == "primary" else "mean_relative_regression"] = (
        stable_mean(effects) if effects else None)
    result["confidence_interval"] = None
    enough = result["n_defined_pairs"] >= contract["min_pairs"]
    if enough and relative:
        result["confidence_interval"] = bootstrap(effects, contract["bootstrap_samples"],
                                                   contract["confidence"], rng)
    if kind == "guardrail" and result["observed_violations"]:
        result["status"] = "violated"
        result["reason"] = "at least one eligible observed candidate violates a declared absolute or relative limit"
    elif result["missing_or_undefined"]:
        result["reason"] = "some declared pairs have no eligible, defined relative effect"
    elif not enough:
        result["reason"] = "fewer independent pairs than contract.min_pairs"
    elif kind == "primary":
        if strictly_above(result["confidence_interval"]["lower"], rule["min_relative_improvement"]):
            result["status"] = "passed"
            result["reason"] = "lower confidence bound strictly exceeds minimum improvement"
        elif strictly_above(0, result["confidence_interval"]["upper"]):
            result["status"] = "regressed"
            result["reason"] = "upper confidence bound is below zero improvement"
        else:
            result["reason"] = "confidence interval does not establish the required improvement"
    elif not relative:
        result["status"] = "passed"
        result["reason"] = "all eligible observed candidates satisfy the declared absolute limits"
    elif not strictly_above(result["confidence_interval"]["upper"], rule["max_relative_regression"]):
        result["status"] = "passed"
        result["reason"] = "no observed violation and upper confidence bound is within limit"
    else:
        result["reason"] = "confidence interval does not establish compliance with the regression limit"
    return result


def compare(contract_path, trial_path):
    contract, contract_sha = read_json(contract_path)
    trial, trial_sha = read_json(trial_path)
    validate_contract(contract)
    validate_trial(trial, contract)
    seen = {"files": set(), "run_ids": set(), "requests_hashes": set()}
    inputs = []
    grouped = {scenario: [] for scenario in contract["scenarios"]}
    trial_base = Path(trial_path).resolve().parent
    change = contract.get("implementation_change")
    for index, pair in enumerate(trial["pairs"]):
        scenario = pair["scenario"]
        loaded = {"pair_index": index, "scenario": scenario}
        for side in ("baseline", "candidate"):
            loaded[side] = read_run(trial_base / pair[side], scenario, seen, inputs, side, change)
        loaded["eligibility_issues"] = [side + ": " + issue for side in ("baseline", "candidate")
                                        for issue in loaded[side]["eligibility_issues"]]
        if loaded["baseline"]["valid"] and loaded["candidate"]["valid"]:
            same_identities(loaded["baseline"], loaded["candidate"], scenario)
        grouped[scenario].append(loaded)
    # A trial compares one baseline deployment with one candidate deployment
    # across every required workload. Scenario-specific client/workload
    # identities are allowed, but cannot hide a different serving treatment.
    deployment_configs = {}
    implementation_context = None
    for scenario, pairs in grouped.items():
        identities = {}
        for pair in pairs:
            for side in ("baseline", "candidate"):
                run = pair[side]
                if not run["valid"]:
                    continue
                fields = IDENTITIES + ("measurement_mode", "config_sha256")
                signature = tuple(run["manifest"][key].lower() if key.endswith("sha256")
                                  else run["manifest"][key] for key in fields)
                if side in identities and signature != identities[side]:
                    fail("{}: {} identities/config drift across repeated runs".format(scenario, side))
                identities[side] = signature
                config = run["manifest"]["config_sha256"].lower()
                if change is not None:
                    context = (config, run["manifest"]["environment_sha256"].lower())
                    if implementation_context is not None and context != implementation_context:
                        fail("implementation_change requires identical config and environment across all runs")
                    implementation_context = context
                if side in deployment_configs and config != deployment_configs[side]["config"]:
                    fail("{}: {} deployment config drift across required scenarios; "
                         "all scenarios must test the same {} serving configuration "
                         "(first seen in {})".format(
                             scenario, side, side, deployment_configs[side]["scenario"]))
                deployment_configs.setdefault(side, {"config": config, "scenario": scenario})
    rng = random.Random(contract["seed"])
    # Adding the extra goodput test leaves the existing bootstrap draws reproducible.
    goodput_rng = random.Random(contract["seed"])
    report = {
        "schema_version": 1, "input_valid": True, "recommendation": "defer",
        "mechanism_status": "unresolved", "errors": [], "reasons": [],
        "contract": contract, "contract_sha256": contract_sha, "trial_sha256": trial_sha,
        "method": {
            "name": "paired_percentile_bootstrap_of_mean_relative_effect",
            "resampling_unit": "one complete independent baseline/candidate run pair",
            "confidence": contract["confidence"], "bootstrap_samples": contract["bootstrap_samples"],
            "seed": contract["seed"], "quantile_interpolation": "linear",
            "relative_effect_boundary_roundoff_tolerance": {"relative": 1e-12, "absolute": 1e-15},
            "primary_effect": "higher: candidate/baseline - 1; lower: 1 - candidate/baseline",
            "guardrail_effect": "lower: candidate/baseline - 1; higher: 1 - candidate/baseline",
            "estimand": "equal-weight mean of paired relative effects, not ratio of pooled means",
            "deployment_scope": "one baseline serving config and one candidate serving config across every required scenario",
            "limitations": [
                "Pairing, independent repetitions, stable environment, and predeclared design are assumptions; file validation cannot prove them.",
                "Percentile bootstrap intervals can be unreliable with few pairs or nearly identical samples; min_pairs is not a statistical guarantee.",
                "Intervals are marginal per metric/scenario, not simultaneous family-wise coverage; exploratory repeated testing and selection can invalidate nominal confidence.",
                "Selection among multiple candidate configurations requires an independent confirmation experiment; this report does not correct for candidate selection.",
                "Absolute guardrails check every eligible observed candidate run; passing observed limits does not guarantee future tail behavior or zero future failures.",
                "A performance effect does not identify its mechanism; mechanism_status remains unresolved.",
                "quality_status and effective_config_verified are caller declarations; this comparison does not independently evaluate quality or inspect effective server configuration.",
                "No confidence interval is extrapolated to untested workloads, versions, output semantics, or quality criteria.",
            ],
        },
        "scenarios": {}, "pairs": [], "validated_inputs": inputs,
    }
    if change is not None:
        report["method"]["deployment_scope"] = (
            "one predeclared baseline and candidate implementation, with a fixed serving config "
            "and environment across every required scenario")
        report["method"]["limitations"].append(
            "Implementation and quality evidence are caller-supplied, fingerprinted deployment records. "
            "Verify their applicability to the measured server before adopting.")
    statuses = []
    has_ineligible = False
    for scenario in sorted(grouped):
        pairs = grouped[scenario]
        spec = contract["scenarios"][scenario]
        current = {"n_pairs": len(pairs), "min_pairs": contract["min_pairs"],
                   "primary": None, "guardrails": [], "issues": []}
        if not pairs:
            current["issues"].append("required scenario is missing from trial")
        elif len(pairs) < contract["min_pairs"]:
            current["issues"].append("insufficient independent paired runs")
        for pair in pairs:
            has_ineligible = has_ineligible or bool(pair["eligibility_issues"])
            report["pairs"].append({"pair_index": pair["pair_index"], "scenario": scenario,
                                    "baseline_path": pair["baseline"]["path"],
                                    "candidate_path": pair["candidate"]["path"],
                                    "baseline_run_id": pair["baseline"]["manifest"].get("run_id"),
                                    "candidate_run_id": pair["candidate"]["manifest"].get("run_id"),
                                    "eligibility_issues": pair["eligibility_issues"]})
        if spec["primary"] is not None:
            current["primary"] = evaluate_rule(spec["primary"], "primary", pairs, contract, scenario, rng)
            statuses.append(current["primary"]["status"])
        for rule in spec["guardrails"]:
            evaluated = evaluate_rule(rule, "guardrail", pairs, contract, scenario, rng)
            current["guardrails"].append(evaluated)
            statuses.append(evaluated["status"])
        evaluated_rules = ([current["primary"]] if current["primary"] else []) + current["guardrails"]
        if "goodput_primary" in spec:
            evaluated = evaluate_rule(spec["goodput_primary"], "primary", pairs, contract, scenario, goodput_rng)
            current["goodput_primary"] = evaluated
            evaluated_rules.append(evaluated)
            statuses.append(evaluated["status"])
        current["status"] = ("passed" if not current["issues"]
                             and all(result["status"] == "passed" for result in evaluated_rules) else "not_passed")
        report["scenarios"][scenario] = current
    if has_ineligible:
        report["reasons"].append("At least one run is ineligible; no promotion decision may use it.")
    if "violated" in statuses:
        report["recommendation"] = "revert"
        report["reasons"].append("At least one eligible observed guardrail regression exceeds its declared limit; stop promotion. This is not a causal mechanism claim.")
    elif "regressed" in statuses:
        report["recommendation"] = "revert"
        report["reasons"].append("An eligible primary-metric interval indicates regression under the declared paired design.")
    elif not has_ineligible and all(s["status"] == "passed" for s in report["scenarios"].values()):
        report["recommendation"] = "adopt"
        report["reasons"].append("All required scenarios establish their declared benefit or regression protection, subject to the recorded assumptions and caller declarations.")
    else:
        report["reasons"].append("The declared evidence does not establish every required improvement and constraint; inspect scenario/rule reasons before collecting or changing anything.")
    return report, 0 if report["recommendation"] == "adopt" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, help="Predeclared comparison contract JSON")
    parser.add_argument("--trial", required=True, help="Paired validated-run paths JSON")
    parser.add_argument("--output", required=True, help="New report path; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        report, exit_code = compare(args.contract, args.trial)
    except (InputError, OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        report = {"schema_version": 1, "input_valid": False, "recommendation": "defer",
                  "mechanism_status": "unresolved", "errors": [str(exc)],
                  "reasons": ["Invalid or unreadable input; no candidate benefit or harm was established."]}
        exit_code = 2
    try:
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        with open(args.output, "x", encoding="utf-8") as stream:
            stream.write(serialized)
    except (OSError, ValueError) as exc:
        print("Cannot create comparison report: {}".format(exc), file=sys.stderr)
        return 2
    print("{}: {}".format(report["recommendation"], args.output))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

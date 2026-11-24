#!/usr/bin/env python3
"""Inspect diagnostic throughput, reconcile logical steps and compare two like-for-like runs."""
import argparse
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import sys
import tempfile

if __name__ == "__main__":
    sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diagnose.scripts import profile, step_ledger
from diagnose.scripts.request_map import read_diagnostic, request_map
from evaluate.scripts import compare_trials
from scripts import round_evidence


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_ref(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": profile.file_hash(path)}


def emit(text, output):
    if output is None:
        sys.stdout.write(text)
        return
    # Publish completed bytes only; a concurrent writer cannot replace an existing report.
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as stream:
        tmp = Path(stream.name)
    try:
        tmp.write_text(text, encoding="utf-8")
        os.link(tmp, output)
    finally:
        tmp.unlink()


def goodput(run, report, args):
    """Score only when the caller supplies a quality or individual-request SLO policy."""
    overrides = {key: getattr(args, key, None) for key in
                 ("max_ttft_s", "max_e2e_s", "min_request_score", *round_evidence.GOODPUT_SEMANTICS)}
    thresholds = {key: value for key, value in overrides.items() if value is not None}
    for name in ("max_ttft_s", "max_e2e_s", "min_request_score"):
        if name in thresholds:
            require(math.isfinite(thresholds[name]) and thresholds[name] >= 0,
                    name + " must be finite and nonnegative")
    if args.min_request_score is not None:
        require(args.quality_contract and 0 < args.min_request_score <= 1,
                "--min-request-score requires a quality contract and a threshold in (0,1]")
    if not args.quality_contract and not thresholds:
        return None
    checked = read_diagnostic(run)
    core_hashes = {Path(ref["path"]).name: ref["sha256"] for ref in report["benchmark_sources"]}
    require(checked["source_hashes"] == core_hashes,
            "run changed during goodput verification")
    quality = None
    contract_digest = None
    inputs = {}
    facts = None
    if args.quality_contract:
        path = args.quality_contract.resolve(strict=True)
        contract, contract_digest = compare_trials.read_json(path)
        inputs[str(path)] = contract_digest
        scenario = checked["manifest"]["scenario"]
        rules = round_evidence.quality_rules(path, contract, inputs)
        if scenario in contract.get("goodput_policy", {}).get("scenarios", {}):
            frozen = round_evidence.goodput_rules(contract, rules)[scenario]
            for key, value in thresholds.items():
                default = round_evidence.GOODPUT_SEMANTICS[key][0] if key in round_evidence.GOODPUT_SEMANTICS else None
                require(frozen.get(key, default) == value, "diagnostic override differs from frozen goodput policy: " + key)
            thresholds = frozen
        facts = {}
        quality = round_evidence.score_run(run, checked, rules[scenario], inputs, stream_facts=facts)
    elif thresholds.get("first_output_channel", "any") != "any" or thresholds.get("token_source") == "server_content_tokens":
        facts = round_evidence.diagnostic_stream_facts(run, checked, inputs)
    report = round_evidence.measure_goodput(run, checked, thresholds, quality, stream_facts=facts)
    for name, digest in core_hashes.items():
        require(profile.file_hash(run / name) == digest, "benchmark changed during goodput verification: " + name)
    return {**report, "quality_contract_sha256": contract_digest, "quality_evaluation": quality,
            "sources": [dict(path=p, sha256=h) for p, h in sorted(inputs.items())]}


def analyze(run, sources=None, bundle=None, timeline_map=None, score_args=None, verified_bundle=None):
    """Compose native and site adapters without interpreting a saved analysis report as raw input."""
    require(run is not None or bundle is not None, "supply a benchmark --run or a profile --bundle")
    if run is None:
        run = bundle / "diagnostic-run"
    run = run.resolve(strict=True)
    observations = None
    if sources:
        from diagnose.scripts.step_sources import read_sources
        spec_ref = file_ref(sources)
        observations = read_sources(sources, run)
    report = step_ledger.build(run, observations)
    report["profile"] = None
    report["timeline"] = None
    report["goodput"] = None
    report["analysis_inputs"] = []
    if sources:
        require(file_ref(sources) == spec_ref, "source spec changed during analysis")
        report["analysis_inputs"].append(spec_ref)
    if bundle:
        captured = verified_bundle if verified_bundle is not None else profile.verify_bundle(bundle)
        require(captured["valid"], "profile archive invalid: " + str(captured["errors"]))
        require(captured["run_id"] == report["run_id"] and
                captured["diagnostic_source_hashes"] == {
                    Path(r["path"]).name: r["sha256"] for r in report["benchmark_sources"]},
                "profile and diagnostic benchmark originals do not match")
        report["profile"] = {key: captured[key] for key in ("manifest_sha256", "spec_sha256",
                             "profile_policy_sha256", "run_binding_status")}
        bundle_ref = file_ref(bundle / "manifest.json")
        require(bundle_ref["sha256"] == captured["manifest_sha256"], "profile bundle changed during analysis")
        report["analysis_inputs"].append(bundle_ref)
    if timeline_map:
        from diagnose.scripts.step_timeline import analyze as timeline
        require(bundle is not None, "--timeline-map requires a verified --bundle")
        keys = {(step["replica"], step["step_id"]) for step in report["steps"]}
        report["timeline"] = timeline(bundle, timeline_map, report["run_id"],
                                      manifest_sha256=report["manifest_sha256"], step_keys=keys or None)
        report["analysis_inputs"].append(report["timeline"]["sources"][0])
    if score_args is not None:
        report["goodput"] = goodput(run, report, score_args)
    return report


def relation(a, b, comparable=True):
    available = comparable and a is not None and b is not None
    return {"baseline": a, "candidate": b, "delta": b - a if available else None,
            "ratio": b / a if available and a else None}


def compare(a, b):
    require(a["run_id"] != b["run_id"] and a["manifest_sha256"] != b["manifest_sha256"],
            "independent runs must have distinct identities")
    for key in compare_trials.IDENTITIES + ("scenario", "measurement_mode", "model", "base_url"):
        require(a["identity"][key] == b["identity"][key], "case mismatch: " + key)
    ap, bp = a["profile"], b["profile"]
    if ap and bp:
        require(ap["profile_policy_sha256"] == bp["profile_policy_sha256"], "profile policy mismatch")
    notes = []
    if not ap or not bp:
        notes.append("Profiler settings were not verified for both sides. Compare diagnostic timings conditionally.")
    same_steps = bool(a["step_definition"]) and a["step_definition"] == b["step_definition"]
    if not same_steps:
        notes.append("A common logical step definition was not established.")
    rows = {key: relation(a["metrics"][key], b["metrics"][key],
                          same_steps if key in ("K", "R", "Y_bar") else True)
            for key in ("N", "T", "output_token_throughput", "K", "R", "Y_bar", "n_requests",
                        "n_success", "n_failed", "failure_rate", "ttft_p99_s", "e2e_p99_s")}
    rows["token_coverage"] = relation(a["coverage"]["token_fraction"], b["coverage"]["token_fraction"])
    if a["goodput"] and b["goodput"]:
        require(a["goodput"]["thresholds"] == b["goodput"]["thresholds"], "goodput threshold mismatch")
        # Raw evaluation inputs differ across runs. The pinned policy and scorer must agree.
        require(a["goodput"].get("quality_contract_sha256") == b["goodput"].get("quality_contract_sha256"),
                "goodput quality policy mismatch")
        rows["goodput"] = relation(a["goodput"]["output_tokens_per_s"], b["goodput"]["output_tokens_per_s"])
        for metric in ("qualified_requests_per_s", "qualified_server_completion_tokens_per_s"):
            if metric in a["goodput"] and metric in b["goodput"]:
                rows[metric] = relation(a["goodput"][metric], b["goodput"][metric])
    evidence = [s for s in (a, b) if s["coverage"]["status"] != "complete"]
    if evidence:
        notes.append("Incomplete request/step reconciliation. Inspect coverage.issues and unresolved per side.")
    scale = a["metrics"]["T"] / b["metrics"]["T"]
    step_pairs = matched_steps(a["steps"], b["steps"]) if same_steps and not evidence else []
    return {"kind": "diagnostic_pair", "rows": rows,
            "throughput_factors": {"candidate_tokens_over_baseline_tokens": rows["N"]["ratio"],
                                   "baseline_wall_over_candidate_wall": scale,
                                   "candidate_throughput_over_baseline_throughput": rows["output_token_throughput"]["ratio"]},
            "largest_matched_step_duration_changes": step_pairs[:10], "matched_step_count": len(step_pairs),
            "longest_observed_steps": {"baseline": slow_steps(a), "candidate": slow_steps(b)},
            "notes": notes, "decision_scope": "mechanism exploration; formal acceptance uses unprofiled paired trials"}


def step_ref(row):
    return {k: row[k] for k in ("replica", "step_id", "duration_us", "effective_output_tokens_mapped", "source")}


def slow_steps(report):
    steps = (s for s in report["steps"] if s["duration_us"] is not None)
    return [step_ref(s) for s in sorted(steps, key=lambda s: -s["duration_us"])[:10]]


def matched_steps(baseline, candidate):
    # Step counters are local to each run. Match only unique groups of identical delivered positions.
    sides = []
    for steps in (baseline, candidate):
        index = defaultdict(list)
        for step in steps:
            if step["duration_us"] is None:
                continue
            ranges = sorted((r["request_id"], r["output_start"], r["output_count"])
                            for r in step["output_ranges"] if r["counted_in_N"])
            positions = []
            for req, start, count in ranges:
                if positions:
                    last_req, first, previous_count = positions[-1]
                    if last_req == req and first + previous_count == start:
                        positions[-1] = (req, first, previous_count + count)
                        continue
                positions.append((req, start, count))
            if positions:
                index[tuple(positions)].append(step)
        sides.append(index)
    changes = []
    for key in sorted(sides[0].keys() & sides[1].keys()):
        left, right = sides[0][key], sides[1][key]
        if len(left) == len(right) == 1:
            changes.append({"baseline": step_ref(left[0]), "candidate": step_ref(right[0]),
                            "matched_output_ranges": key, "delta_us": right[0]["duration_us"] - left[0]["duration_us"]})
    return sorted(changes, key=lambda item: -abs(item["delta_us"]))


def markdown_pair(report):
    lines = ["# 诊断运行 A/B 对照", "", "| 指标 | A | B | B − A | B / A |", "|---|---:|---:|---:|---:|"]
    labels = {"N": "成功请求的输出 token", "T": "整批墙钟（s）", "K": "逻辑 step 数",
              "R": "逻辑 step/s", "Y_bar": "有效 token/step", "token_coverage": "token 关联比例",
              "output_token_throughput": "有效输出 token/s", "goodput": "满足约束的输出 token/s",
              "qualified_requests_per_s": "合格请求/s", "qualified_server_completion_tokens_per_s": "合格请求的服务端 completion token/s"}
    for name, row in report["comparison"]["rows"].items():
        cells = ["—" if x is None else format(x, ".7g") for x in row.values()]
        lines.append("| " + " | ".join([labels.get(name, name), *cells]) + " |")
    for name in ("baseline", "candidate"):
        ledger = report[name]
        lines.extend(["", "## " + name, "", "运行 ID `" + ledger["run_id"] + "`",
                      "", "逐步证据覆盖情况为 `" + ledger["coverage"]["status"] + "`。", ""])
        for issue in ledger["unresolved"]:
            lines.append("- " + issue)
        for step in report["comparison"]["longest_observed_steps"][name]:
            lines.append("- {} / {} · {:.7g} μs · {}".format(
                step["replica"], step["step_id"], step["duration_us"], json.dumps(step["source"], ensure_ascii=False)))
    changes = report["comparison"]["largest_matched_step_duration_changes"]
    if changes:
        lines.extend(["", "## 同一组输出位置的步耗时变化", "",
                      "| A 副本 / step | B 副本 / step | B − A（μs） |", "|---|---|---:|"])
        for change in changes:
            left, right = change["baseline"], change["candidate"]
            lines.append("| {} / {} | {} / {} | {:.7g} |".format(
                left["replica"], left["step_id"], right["replica"], right["step_id"], change["delta_us"]))
    lines.extend(["", *report["comparison"]["notes"], ""])
    return "\n".join(lines)


def cli(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    one = sub.add_parser("run", help="reconcile one diagnostic benchmark or sealed bundle")
    pair = sub.add_parser("compare", help="rebuild both ledgers and compare a fixed case")
    mapping = sub.add_parser("request-map", help="emit a CSV of unique response IDs from retained SSE")
    mapping.add_argument("--run", required=True, type=Path)
    for cmd in (one, pair, mapping):
        cmd.add_argument("--output", type=Path, help="new file; default: stdout")
    for cmd in (one, pair):
        cmd.add_argument("--quality-contract", type=Path, help="trusted campaign contract for optional actual output scoring")
        cmd.add_argument("--min-request-score", type=float)
        cmd.add_argument("--max-ttft-s", type=float)
        cmd.add_argument("--max-e2e-s", type=float)
        for key, choices in round_evidence.GOODPUT_SEMANTICS.items():
            cmd.add_argument("--" + key.replace("_", "-"), choices=choices)
    one.add_argument("--run", type=Path)
    for flag in ("sources", "bundle", "timeline-map"):
        one.add_argument("--" + flag, type=Path)
    for side in ("baseline", "candidate"):
        pair.add_argument("--" + side, type=Path, help="original diagnostic run (or use --" + side + "-bundle)")
        for flag in ("sources", "bundle", "timeline-map"):
            pair.add_argument("--" + side + "-" + flag, type=Path)
    pair.add_argument("--format", choices=("json", "markdown"), default="json")
    args = p.parse_args(argv)
    try:
        if args.action == "request-map":
            emit(request_map(args.run), args.output)
            return 0
        if args.action == "run":
            result = analyze(args.run, args.sources, args.bundle, args.timeline_map, args)
        else:
            result = {"schema_version": 1}
            for side in ("baseline", "candidate"):
                result[side] = analyze(getattr(args, side), getattr(args, side + "_sources"),
                                       getattr(args, side + "_bundle"), getattr(args, side + "_timeline_map"), args)
            result["comparison"] = compare(result["baseline"], result["candidate"])
            if args.format == "markdown":
                emit(markdown_pair(result), args.output)
                return 0
        emit(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", args.output)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        print("step_analysis: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(cli())

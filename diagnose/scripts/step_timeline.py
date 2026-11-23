#!/usr/bin/env python3
"""Attribute sealed Chrome trace spans to logical engine steps using explicit IDs."""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from diagnose.scripts import profile


STAGES = {"scheduler", "runner", "device", "communication", "wait"}


def read_document(path):
    raw = path.read_bytes()
    doc = json.loads(raw, object_pairs_hook=profile.strict_object)
    profile.check(isinstance(doc, dict), "expected JSON object: " + str(path))
    return doc, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def identifier(value):
    if type(value) is int or (isinstance(value, str) and value.strip()):
        return str(value)
    return None


def rules_for(mapping, policy):
    required = {"schema_version", "tool", "step_field", "rules"}
    optional = {"replica_field", "replica_by_rank", "correlation", "clock_alignment"}
    profile.check(required <= set(mapping) <= required | optional and
                  type(mapping["schema_version"]) is int and mapping["schema_version"] == 1,
                  "invalid timeline mapping schema")
    profile.check(mapping["tool"] == policy["tool"], "timeline mapping tool/version differs from sealed profile")
    profile.check(profile.nonempty(mapping["step_field"]), "missing step_field")
    profile.check(("replica_field" in mapping) != ("replica_by_rank" in mapping),
                  "use one replica_field or replica_by_rank")
    if "replica_field" in mapping:
        profile.check(profile.nonempty(mapping["replica_field"]), "invalid replica_field")
    else:
        replicas = mapping["replica_by_rank"]
        profile.check(isinstance(replicas, dict) and set(replicas) == set(policy["ranks"])
                      and all(identifier(v) is not None for v in replicas.values()),
                      "replica_by_rank must cover the sealed ranks")
    if "correlation" in mapping:
        corr = mapping["correlation"]
        profile.keys(corr, ("producer_field", "consumer_field"), "rank-local correlation")
        profile.check(all(profile.nonempty(path) for path in corr.values()), "empty correlation field")
    profile.check(isinstance(mapping["rules"], list) and mapping["rules"], "missing stage rules")
    rules = []
    for rule in mapping["rules"]:
        profile.keys(rule, ("stage", "field", "pattern"), "stage rule")
        profile.check(rule["stage"] in STAGES and profile.nonempty(rule["field"])
                      and profile.nonempty(rule["pattern"]), "invalid stage rule")
        try:
            rules.append((rule["stage"], rule["field"], re.compile(rule["pattern"])))
        except re.error as exc:
            raise ValueError("invalid stage regular expression: " + str(exc)) from exc
    return rules


def aligned_clock(mapping, base, report, sources):
    if "clock_alignment" not in mapping:
        return False
    ref = mapping["clock_alignment"]
    profile.keys(ref, ("path", "sha256"), "clock_alignment")
    doc, source = read_document((base / ref["path"]).resolve(strict=True))
    profile.check(source["sha256"] == ref["sha256"], "clock alignment receipt hash differs")
    policy = report["profile_policy"]
    profile.check(doc.get("run_id") == report["run_id"] and doc.get("clock") == policy["window"]["clock"]
                  and doc.get("ranks") == policy["ranks"] and profile.nonempty(doc.get("method")),
                  "clock alignment receipt must identify this run, clock and all ranks")
    evidence = doc.get("evidence")
    profile.keys(evidence, ("path", "sha256"), "clock alignment evidence")
    evidence_path = (Path(source["path"]).parent / evidence["path"]).resolve(strict=True)
    digest = profile.file_hash(evidence_path)
    profile.check(evidence_path != Path(source["path"]) and evidence_path.stat().st_size
                  and digest == evidence["sha256"], "clock synchronization evidence missing or changed")
    sources.extend([source, {"path": str(evidence_path), "sha256": digest}])
    return True


def widths(spans):
    """Time with at least one/two simultaneous spans, and sum with multiplicity."""
    deltas = Counter()
    total = Decimal(0)
    for span in spans:
        start, end = span["start"], span["end"]
        deltas[start] += 1
        deltas[end] -= 1
        total += end - start
    stamps = sorted(deltas)
    count = 0
    previous = stamps[0]
    union = overlap = Decimal(0)
    for stamp in stamps:
        if count:
            union += stamp - previous
        if count >= 2:
            overlap += stamp - previous
        count += deltas[stamp]
        previous = stamp
    return {"sum_span_us": str(total), "wall_union_us": str(union), "overlap_wall_us": str(overlap),
            "observed_extent_us": str(stamps[-1] - stamps[0]), "spans": len(spans)}


def observation(span):
    return {"rank": span["rank"], "pid": span["pid"], "tid": span["tid"], "stage": span["stage"],
            "name": span["name"], "start_us": str(span["start"]), "end_us": str(span["end"]),
            "clipped": span["clipped"], "join": span["join"], "source": span["source"]}


def summarize_step(key, spans, aligned):
    lanes = defaultdict(list)
    stages = defaultdict(list)
    for span in spans:
        if span["pid"] is not None and span["tid"] is not None:
            lanes[(span["rank"], span["pid"], span["tid"])].append(span)
        stages[span["stage"]].append(span)
    longest = sorted(spans, key=lambda s: s["end"] - s["start"], reverse=True)[:5]
    return {"replica": key[0], "step_id": key[1], "ranks": sorted({s["rank"] for s in spans}, key=int),
            "aligned": widths(spans) if aligned else None,
            "stages": {stage: (widths(items) if aligned else {"spans": len(items),
                        "sum_span_us": str(sum(s["end"] - s["start"] for s in items))})
                       for stage, items in sorted(stages.items())},
            "lanes": [{"rank": rank, "pid": pid, "tid": tid, **widths(items)}
                      for (rank, pid, tid), items in sorted(lanes.items())],
            "longest_spans": [observation(s) for s in longest],
            "last_observed_span": observation(max(spans, key=lambda s: s["end"])) if aligned else None}


def event_intervals(events):
    """Pair nested synchronous B/E events only on their own pid/tid track."""
    stacks = defaultdict(list)
    for index, event in enumerate(events):
        phase = event["ph"]
        if phase not in ("B", "E"):
            yield index, event, None
            continue
        lane = identifier(event.get("pid")), identifier(event.get("tid"))
        if not all(lane):
            yield index, event, None
        elif phase == "B":
            stacks[lane].append((index, event))
        elif not stacks[lane]:
            yield index, event, None
        else:
            begin_index, begin = stacks[lane].pop()
            duration = profile.number(event["ts"]) - profile.number(begin["ts"])
            if duration < 0 or (event.get("name") and event["name"] != begin.get("name")):
                # The nesting on this track is ambiguous until its pending stack is exhausted.
                yield begin_index, begin, None
                yield index, event, None
                for pending_index, pending in stacks.pop(lane):
                    yield pending_index, pending, None
            else:
                yield begin_index, dict(begin, ph="X", dur=duration), index
    for pending in stacks.values():
        for index, event in pending:
            yield index, event, None


def collect_rank(rank, path, spec, window, mapping, rules, inventory, grouped, counts):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        doc = json.load(stream, parse_float=Decimal, object_pairs_hook=profile.strict_object)
    events = doc["traceEvents"] if isinstance(doc, dict) else doc
    step_field = mapping["step_field"]
    replica_field = mapping.get("replica_field")
    rank_replica = identifier(mapping.get("replica_by_rank", {}).get(rank))

    def event_ids(event):
        replica = identifier(profile.field(event, replica_field)) if replica_field else rank_replica
        return replica, identifier(profile.field(event, step_field))

    # IDs can repeat; only a unique rank-local logical step can propagate to a consumer.
    corr = mapping.get("correlation")
    origins = defaultdict(dict)
    filename = profile.trace_path(spec, rank)
    if corr:
        for index, event in enumerate(events):
            token = identifier(profile.field(event, corr["producer_field"]))
            key = event_ids(event)
            if token and all(key):
                origins[token].setdefault(key, index)
    left, right = window
    for index, event, end_index in event_intervals(events):
        kinds = {stage for stage, field, pattern in rules
                 if (value := profile.field(event, field)) is not None and pattern.fullmatch(str(value))}
        if not kinds:
            continue
        if event["ph"] != "X":
            counts["selected_non_x_events"] += 1
            continue
        begin = profile.number(event["ts"])
        finish = begin + profile.number(event["dur"])
        start, end = max(begin, left), min(finish, right)
        if end <= start:
            continue
        counts["selected_spans"] += 1
        if len(kinds) != 1:
            counts["ambiguous_stage"] += 1
            continue
        key = event_ids(event)
        origin = {"type": "explicit_step"}
        token = identifier(profile.field(event, corr["consumer_field"])) if corr else None
        candidates = origins.get(token, {})
        if all(key) and candidates and key not in candidates:
            counts["conflicting_step_key"] += 1
            continue
        if not all(key):
            if len(candidates) != 1:
                counts["ambiguous_correlation" if len(candidates) > 1 else "missing_step_key"] += 1
                continue
            candidate = next(iter(candidates))
            if any(v is not None and v != candidate[i] for i, v in enumerate(key)):
                counts["conflicting_step_key"] += 1
                continue
            key = candidate
            origin = {"type": "rank_local_correlation", "correlation": token, "producer_file": filename,
                      "producer_event_index": candidates[key]}
        if inventory is not None and key not in inventory:
            counts["outside_inventory"] += 1
            continue
        source = {"file": filename, "event_index": index}
        if end_index is not None:
            source["end_event_index"] = end_index
        grouped[key].append({"stage": kinds.pop(), "rank": rank, "pid": identifier(event.get("pid")),
                             "tid": identifier(event.get("tid")), "name": str(event.get("name", "")),
                             "start": start, "end": end, "clipped": (start != begin or end != finish),
                             "join": origin, "source": source})
        counts["attributed_spans"] += 1


def analyze(bundle_dir, mapping_path, run_id, *, manifest_sha256=None, step_keys=None):
    """Summarize explicitly attributed spans; logical keys are (replica, step_id) pairs."""
    bundle = Path(bundle_dir).resolve(strict=True)
    mapping_path = Path(mapping_path).resolve(strict=True)
    report = profile.verify_bundle(bundle)
    profile.check(report["valid"], "invalid profile bundle: " + "; ".join(report["errors"]))
    profile.check(report["run_id"] == run_id, "profile run ID differs from the ledger")
    benchmark_sha = report["diagnostic_source_hashes"]["manifest.json"]
    profile.check(manifest_sha256 is None or benchmark_sha == manifest_sha256, "benchmark manifest hash differs")
    mapping, mapping_ref = read_document(mapping_path)
    rules = rules_for(mapping, report["profile_policy"])
    sources = [mapping_ref, {"path": str(bundle / "manifest.json"), "sha256": report["manifest_sha256"]}]
    aligned = aligned_clock(mapping, mapping_path.parent, report, sources)
    inventory = None if step_keys is None else set(step_keys)
    spec, spec_ref = read_document(bundle / "spec.json")
    manifest, manifest_ref = read_document(bundle / "manifest.json")
    profile.check(spec_ref["sha256"] == report["spec_sha256"] and
                  manifest_ref["sha256"] == report["manifest_sha256"], "profile bundle changed during analysis")
    grouped, counts, issues = defaultdict(list), Counter(), []
    if report["run_binding_status"] == "operator_paired":
        issues.append("trace/run association lacks a shared field or fresh capture")
    else:
        for rank in report["profile_policy"]["ranks"]:
            rel = profile.trace_path(spec, rank)
            path = bundle / rel
            window = tuple(profile.number(report["coverage"][rank][field])
                           for field in ("window_start_us", "window_end_us"))
            collect_rank(rank, path, spec, window, mapping, rules, inventory, grouped, counts)
            expected = manifest["files"][rel]
            profile.check(profile.file_hash(path) == expected, "trace changed during timeline analysis")
            sources.append({"path": str(path), "sha256": expected})
    if not aligned:
        issues.append("no shared-clock evidence for comparisons across lanes or ranks")
    if not grouped:
        issues.append("no attributed complete spans inside the profile window")
    if counts["attributed_spans"] != counts["selected_spans"] or counts["selected_non_x_events"]:
        issues.append("some selected events lack a supported span or unique association")
    missing = sorted(inventory - set(grouped)) if inventory is not None else []
    if missing:
        issues.append("some inventory steps have no associated span")
    observed_stages = {span["stage"] for spans in grouped.values() for span in spans}
    absent_stages = sorted({stage for stage, _, _ in rules} - observed_stages)
    if absent_stages:
        issues.append("some mapped stages have no attributed span")
    steps = [summarize_step(key, grouped[key], aligned) for key in sorted(grouped)]
    longest = sorted(steps, key=lambda s: Decimal(s["aligned"]["observed_extent_us"]), reverse=True)[:10] if aligned else []
    return {"schema_version": 1, "run_id": run_id, "manifest_sha256": benchmark_sha,
            "status": ("unavailable" if not steps else "partial" if issues else "observed"),
            "coverage": {"selected_spans": counts["selected_spans"], "attributed_spans": counts["attributed_spans"],
                         "event_counts": dict(counts), "observed_steps": len(steps), "unobserved_step_keys": missing,
                         "unobserved_stages": absent_stages,
                         "declared_inventory_size": len(inventory) if inventory is not None else None},
            "steps": steps, "longest_steps": [{"replica": s["replica"], "step_id": s["step_id"],
             "observed_extent_us": s["aligned"]["observed_extent_us"],
             "last_observed_span": s["last_observed_span"]} for s in longest],
            "clock_alignment_evidenced": aligned, "run_binding_status": report["run_binding_status"],
            "sources": sources, "unresolved": issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        output = analyze(args.bundle, args.mapping, args.run_id)
        print(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False))
    except profile.READ_ERRORS as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()

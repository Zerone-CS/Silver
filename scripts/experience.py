#!/usr/bin/env python3
"""Index completed campaign rounds and query the evidence behind earlier decisions."""

import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import statistics
import subprocess
import sys


CAMPAIGN = Path(__file__).resolve().with_name("campaign.py")
DECISIONS = ("adopt", "revert", "abandon")
OUTCOMES = ("positive", "negative", "inconclusive", "infrastructure_failure")
CONTEXT_KEYS = {
    "model", "model_sha256", "hardware", "topology", "vllm", "vllm_ascend", "cann",
    "workload_family", "scenario", "workload_sha256", "measurement_mode", "concurrency",
    "requests", "input_p50_tokens", "output_p50_tokens", "max_ttft_s", "max_e2e_s",
    "min_request_score", "quality_method", "primary_metric", "goodput_primary",
    "latency_origin", "first_output_channel", "token_source",
}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def campaign_output(directory, command, *options):
    result = subprocess.run(
        [sys.executable, "-B", str(CAMPAIGN), command, "--campaign", str(directory), *options],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode:
        raise ValueError("campaign " + command + " failed: " + result.stderr.strip())
    return json.loads(result.stdout)


def export_round(directory, number):
    record = campaign_output(directory, "export", "--round", str(number))
    if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record["schema_version"] != 1:
        raise ValueError("unsupported campaign export schema")
    if type(record.get("round")) is not int or record["round"] != number or record.get("decision") not in DECISIONS:
        raise ValueError("campaign export must identify the requested completed round")
    if record.get("site_release", {}).get("status") == "pending":
        raise ValueError("site release is pending; retry archiving after attaching the release proof")
    for field in ("campaign_id", "scope"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError("missing export field: " + field)
    for side in ("baseline", "candidate"):
        identity = record.get(side)
        if side == "candidate" and record["decision"] == "abandon" and side in record and identity is None:
            continue
        if not isinstance(identity, dict):
            raise ValueError("missing deployment identity: " + side)
        if not valid_hash(identity.get("config_sha256")):
            raise ValueError("invalid export identity: " + side + ".config_sha256")
        implementation = identity.get("implementation_sha256", "")
        if implementation is not None and not valid_hash(implementation):
            raise ValueError("invalid export identity: " + side + ".implementation_sha256")
    evidence = record.get("evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("evidence_files"), list):
        raise ValueError("missing round evidence")
    if not evidence["evidence_files"]:
        raise ValueError("empty round evidence")
    for reference in evidence["evidence_files"]:
        if (not isinstance(reference, dict) or not isinstance(reference.get("path"), str)
                or not Path(reference["path"]).is_absolute() or not valid_hash(reference.get("sha256"))):
            raise ValueError("invalid round evidence reference")
    return record


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{64}", value) is not None


def context_fields(data):
    if not isinstance(data, dict) or set(data) - CONTEXT_KEYS:
        raise ValueError("context must contain only supported context fields")
    for key, value in data.items():
        if not (
            isinstance(value, str) and value.strip()
            or type(value) is bool
            or type(value) in (int, float) and math.isfinite(value) and value >= 0
        ):
            raise ValueError("invalid value for context field " + key)
    return data


def _read_pinned(reference, lines=False):
    if not isinstance(reference, dict) or not valid_hash(reference.get("sha256")):
        return None
    try:
        data = Path(reference["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != reference["sha256"]:
            return None
        return [json.loads(row) for row in data.splitlines()] if lines else json.loads(data)
    except (OSError, ValueError, UnicodeError, TypeError, KeyError):
        return None


def _pinned_run_file(row, name):
    digest = row.get("source_hashes", {}).get(name)
    path = row.get("path")
    return {"path": str(Path(path) / name), "sha256": digest} if path and digest else None


def _run_context(run, read_sources):
    manifest = run.get("manifest", {})
    policy = manifest.get("client_measurement_policy", {})
    fields = {
        "model": policy.get("model"),
        "scenario": manifest.get("scenario"),
        "workload_sha256": manifest.get("workload_sha256"),
        "measurement_mode": manifest.get("measurement_mode"),
        "concurrency": policy.get("concurrency"),
        "requests": manifest.get("n_requests"),
    }
    provenance = {}
    metadata_ref = _pinned_run_file(run, "metadata.json")
    metadata = _read_pinned(metadata_ref) if read_sources else None
    env_ref = metadata.get("source_inputs", {}).get("environment") if isinstance(metadata, dict) else None
    env = _read_pinned(env_ref)
    if isinstance(env, dict):
        packages = env["packages"] if isinstance(env.get("packages"), dict) else {}
        hashes = env.get("model_hashes", {})
        deployed = {
            "model": env.get("model"),
            "model_sha256": hashes.get("model.safetensors") if isinstance(hashes, dict) else None,
            "hardware": env.get("hardware") or env.get("npu_model"),
            "topology": env.get("topology"),
            "vllm": packages.get("vllm"),
            "vllm_ascend": packages.get("vllm-ascend"),
            "cann": packages.get("cann") or env.get("cann_version"),
        }
        for key, value in deployed.items():
            if isinstance(value, str) and value.strip():
                fields[key] = value
                provenance[key] = env_ref
    requests_ref = _pinned_run_file(run, "requests.jsonl")
    rows = _read_pinned(requests_ref, lines=True) if read_sources else None
    if rows:
        for key, original in (("input_p50_tokens", "actual_prompt_tokens"),
                              ("output_p50_tokens", "actual_output_tokens")):
            tokens = [row[original] for row in rows if row.get("final_status") == "success"
                      and type(row.get(original)) is int]
            if tokens:
                fields[key] = statistics.median(tokens)
                provenance[key] = requests_ref
    return {k: v for k, v in fields.items() if v is not None}, provenance


def _assessment_outcome(record, assessment):
    comparison = assessment.get("comparison") or {}
    recommendation = assessment.get("recommendation") or comparison.get("recommendation")
    if comparison.get("input_valid") is True and recommendation == "revert":
        return "negative"
    if comparison.get("input_valid") is True and recommendation == "adopt" and record["decision"] == "adopt":
        return "positive"
    evidence = record["evidence"]
    if evidence.get("failure_kind") == "infrastructure_failure" or any(
            attempt.get("failure_kind") == "infrastructure_failure"
            for attempt in evidence.get("invalid_attempts", [])):
        return "infrastructure_failure"
    return "inconclusive"


def round_context(record, supplied=None, read_sources=False):
    evidence = record["evidence"]
    assessments = evidence.get("assessments", [])
    assessment = assessments[-1] if assessments else {"comparison": evidence.get("comparison")}
    comparison = assessment.get("comparison") or {}
    rows = {row.get("manifest", {}).get("run_id"): row for row in comparison.get("validated_inputs", [])}
    by_scenario = {}
    for pair in comparison.get("pairs", []):
        by_scenario.setdefault(pair.get("scenario"), pair)
    scenarios = sorted(name for name in set(by_scenario) | set(comparison.get("scenarios", {}))
                       if isinstance(name, str)) or [record.get("scenario") or "unspecified"]
    outcome = _assessment_outcome(record, assessment)
    results = []
    for scenario in scenarios:
        pair = by_scenario.get(scenario, {})
        baseline_id = pair.get("baseline_run_id")
        fields, sources = _run_context(rows.get(baseline_id, {}), read_sources)
        fields["scenario"] = scenario
        quality = assessment.get("quality", {}).get(baseline_id, {})
        if quality.get("method"):
            fields["quality_method"] = quality["method"]
        goodput = assessment.get("goodput", {}).get(baseline_id, {})
        fields.update({k: v for k, v in goodput.get("thresholds", {}).items() if k in CONTEXT_KEYS})
        rules = comparison.get("scenarios", {}).get(scenario, {})
        primary = rules.get("primary") or {}
        if primary.get("rule", {}).get("metric"):
            fields["primary_metric"] = primary["rule"]["metric"]
        if primary or rules.get("goodput_primary"):
            fields["goodput_primary"] = fields.get("primary_metric") == "goodput" or bool(rules.get("goodput_primary"))
        for key, value in (supplied or {}).items():
            if key not in fields:
                fields[key] = value
                sources[key] = "operator_context"
        measured_rules = [rule for rule in [primary, rules.get("goodput_primary"), *rules.get("guardrails", [])] if rule]
        scenario_outcome = outcome
        if outcome == "negative" and rules and not any(
                rule.get("status") in ("regressed", "violated") for rule in measured_rules):
            scenario_outcome = "inconclusive"
        results.append({
            "context": fields, "context_sources": sources, "outcome": scenario_outcome,
            "observed_scenario_status": rules.get("status"),
            "metrics": [
                {key: rule[key] for key in ("rule", "status", "mean_relative_improvement", "confidence_interval")
                 if key in rule}
                 for rule in measured_rules
            ],
        })
    return {"outcome": outcome, "assessment_batches": len(assessments), "scenarios": results}


def contextual_card(row, scenario, wanted):
    record = row["round_record"]
    fields = scenario["context"]
    # Keep unmeasured conditions distinct from contrary evidence.
    matches, conflicts, unknown = {}, {}, []
    for key, value in wanted.items():
        if key not in fields:
            unknown.append(key)
        elif fields[key] == value:
            matches[key] = value
        else:
            conflicts[key] = {"query": value, "record": fields[key]}
    return {
        "record_id": "{}:{}:{}".format(record["campaign_id"], record["round"], fields["scenario"]),
        "outcome": scenario["outcome"], "decision": record["decision"],
        "assessment_batches": row["derived"]["assessment_batches"],
        **scenario, "matches": matches, "conflicts": conflicts, "unknown": unknown,
        "hypothesis": record.get("hypothesis"), "observation": record.get("observation"),
        "change": record.get("change"), "agent_notes": row["agent_notes"],
        "source": {"campaign_id": record["campaign_id"], "round": record["round"],
                   "export_sha256": row["export_sha256"], "directory": row["source_directory"]},
    }


def save(database, directory, record, note, supplied=None):
    payload = encode(record)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    key = (record["campaign_id"], record["round"])
    with closing(sqlite3.connect(database, timeout=30)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS experience_rounds (
            campaign_id TEXT NOT NULL, round_number INTEGER NOT NULL,
            decision TEXT NOT NULL, scope TEXT NOT NULL, source_directory TEXT NOT NULL,
            export_sha256 TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(campaign_id, round_number))""")
        db.execute("""CREATE TABLE IF NOT EXISTS experience_notes (
            id INTEGER PRIMARY KEY, campaign_id TEXT NOT NULL, round_number INTEGER NOT NULL,
            content TEXT NOT NULL, UNIQUE(campaign_id, round_number, content),
            FOREIGN KEY(campaign_id, round_number) REFERENCES experience_rounds(campaign_id, round_number))""")
        db.execute("""CREATE TABLE IF NOT EXISTS experience_context (
            campaign_id TEXT NOT NULL, round_number INTEGER NOT NULL, content TEXT NOT NULL,
            PRIMARY KEY(campaign_id, round_number),
            FOREIGN KEY(campaign_id, round_number) REFERENCES experience_rounds(campaign_id, round_number))""")
        previous = db.execute(
            "SELECT export_sha256 FROM experience_rounds WHERE campaign_id=? AND round_number=?", key,
        ).fetchone()
        if previous and previous[0] != digest:
            raise ValueError("conflicting export for an existing campaign/round; original evidence retained")
        if previous is None:
            db.execute(
                "INSERT INTO experience_rounds VALUES (?, ?, ?, ?, ?, ?, ?)",
                (*key, record["decision"], record["scope"], str(directory), digest, payload),
            )
        indexed = db.execute(
            "SELECT content FROM experience_context WHERE campaign_id=? AND round_number=?", key).fetchone()
        if indexed is None or supplied:
            derived = encode(round_context(record, supplied, read_sources=True))
            if indexed is None:
                db.execute("INSERT INTO experience_context VALUES (?, ?, ?)", (*key, derived))
            elif indexed[0] != derived:
                raise ValueError("structured context conflicts with the saved evidence")
        added_note = False
        if any(note.values()):
            added_note = bool(db.execute(
                """INSERT INTO experience_notes(campaign_id, round_number, content) VALUES(?, ?, ?)
                   ON CONFLICT(campaign_id, round_number, content) DO NOTHING""",
                (*key, encode(note)),
            ).rowcount)
    return {"campaign_id": key[0], "round": key[1], "export_sha256": digest,
            "added_round": previous is None, "added_note": added_note}


def search(args):
    if not args.db.exists():
        return []
    filters, values = [], []
    for column, value in (("campaign_id", args.campaign_id), ("decision", args.decision),
                          ("round_number", args.round)):
        if value is not None:
            filters.append("r." + column + " = ?")
            values.append(value)
    if args.scope:
        filters.append("instr(lower(r.scope),lower(?)) > 0")
        values.append(args.scope)
    if args.text:
        filters.append("""(instr(lower(r.payload),lower(?)) > 0 OR EXISTS (
            SELECT 1 FROM experience_notes n WHERE n.campaign_id=r.campaign_id AND n.round_number=r.round_number
            AND instr(lower(n.content),lower(?)) > 0))""")
        values.extend([args.text, args.text])
    where = " WHERE " + " AND ".join(filters) if filters else ""
    wanted = getattr(args, "context_value", {})
    cards = getattr(args, "cards", False)
    uri = args.db.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("BEGIN")
        has_context = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='experience_context' AND type='table'").fetchone()
        rows = db.execute(
            "SELECT campaign_id, round_number, payload, export_sha256, source_directory FROM experience_rounds r" +
            where + " ORDER BY r.rowid DESC" + ("" if cards else " LIMIT ?"),
            values if cards else (*values, args.limit),
        )
        matches = []
        for campaign_id, number, payload, digest, directory in rows:
            notes = db.execute(
                "SELECT content FROM experience_notes WHERE campaign_id=? AND round_number=? ORDER BY id",
                (campaign_id, number),
            )
            row = {"round_record": json.loads(payload), "export_sha256": digest,
                   "source_directory": directory, "agent_notes": [json.loads(item[0]) for item in notes]}
            if cards:
                indexed = db.execute(
                    "SELECT content FROM experience_context WHERE campaign_id=? AND round_number=?",
                    (campaign_id, number)).fetchone() if has_context else None
                row["derived"] = json.loads(indexed[0]) if indexed else round_context(row["round_record"])
            matches.append(row)
    if cards:
        entries = [contextual_card(row, scenario, wanted)
                   for row in matches for scenario in row["derived"]["scenarios"]]
        result = []
        for outcome in OUTCOMES:
            ranked = sorted((entry for entry in entries if entry["outcome"] == outcome),
                            key=lambda entry: (len(entry["conflicts"]), -len(entry["matches"]), len(entry["unknown"])))
            result.extend(ranked[:args.limit])
        return result
    return matches


def search_cards(database, wanted, limit=2):
    args = argparse.Namespace(db=database, context_value=context_fields(wanted), cards=True, limit=limit,
                              campaign_id=None, decision=None, round=None, scope=None, text=None)
    return search(args)


def sync(database, directory, supplied=None):
    history = campaign_output(directory, "history")
    numbers = sorted({event["round"] for event in history if event["action"] == "close"})
    records, errors = [], []
    indexed = set()
    if numbers and database.is_file() and not supplied:
        campaign_id = campaign_output(directory, "status")["campaign_id"]
        uri = database.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='experience_context'").fetchone():
                indexed = {number for (number,) in db.execute(
                    "SELECT round_number FROM experience_context WHERE campaign_id=?", (campaign_id,))}
    for number in numbers:
        if number in indexed:
            continue
        try:
            records.append(save(database, directory, export_round(directory, number), {}, supplied))
        except (OSError, ValueError, sqlite3.Error) as exc:
            errors.append({"round": number, "error": str(exc)})
    return {"rounds": records, "already_indexed_rounds": sorted(set(numbers) & indexed), "errors": errors}


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="index a completed round using its campaign export")
    catch_up = commands.add_parser("sync", help="archive all verified closed rounds; safe to retry")
    find = commands.add_parser("search", help="search indexed round records and agent notes")
    for command in (add, catch_up, find):
        command.add_argument("--db", type=Path, required=True, help="private SQLite experience database")
        command.add_argument("--context", type=Path, help="JSON with structured applicability fields")
    add.add_argument("--campaign", type=Path, required=True)
    catch_up.add_argument("--campaign", type=Path, required=True)
    add.add_argument("--round", type=positive, required=True)
    add.add_argument("--mechanism", help="Agent's proposed explanation")
    add.add_argument("--scope-note", help="additional conditions limiting this explanation")
    add.add_argument("--counterevidence", help="contradictory observation and its source")
    find.add_argument("--campaign-id")
    find.add_argument("--decision", choices=DECISIONS)
    find.add_argument("--round", type=positive)
    find.add_argument("--scope", help="substring of the round's original scope")
    find.add_argument("--text", help="literal substring in exported records or annotations")
    find.add_argument("--cards", action="store_true", help="compact evidence per outcome, ranked by context match")
    find.add_argument("--limit", type=positive, default=20)
    args = parser.parse_args(argv)
    try:
        supplied = context_fields(json.loads(args.context.read_text(encoding="utf-8"))) if args.context else None
        if args.command == "add":
            directory = args.campaign.resolve(strict=True)
            record = export_round(directory, args.round)
            note = {key: value.strip() for key, value in (
                ("mechanism", args.mechanism), ("scope_note", args.scope_note),
                ("counterevidence", args.counterevidence),
            ) if value and value.strip()}
            result = save(args.db, directory, record, note, supplied)
        elif args.command == "sync":
            result = sync(args.db, args.campaign.resolve(strict=True), supplied)
        else:
            args.cards = args.cards or supplied is not None
            args.context_value = supplied or {}
            result = search(args)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "sync" and result["errors"]:
        return 1


if __name__ == "__main__":
    sys.exit(main())

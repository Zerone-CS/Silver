#!/usr/bin/env python3
"""Freeze, record, and report a budget-matched paired history-ablation study."""

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import tempfile
import time
import uuid

if __package__:
    from .experience import context_fields, encode, search_cards
else:
    from experience import context_fields, encode, search_cards


ARMS = ("history", "control")
STAGES = ("screen", "confirm")
DECISIONS = ("adopt", "revert", "defer", "infrastructure_failure")
FINISH_REASONS = ("budget", "infrastructure_failure", "cancelled")
DB = "study.sqlite"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value, label):
    require(isinstance(value, str) and bool(value.strip()), label + " must be nonempty text")
    return value


def number(value, label, positive=False):
    require(type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0), label + " is invalid")
    return value


def integer(value, label):
    require(type(value) is int and value > 0, label + " must be a positive integer")
    return value


def sha(content):
    return hashlib.sha256(content).hexdigest()


def load(path):
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict), "expected a JSON object at " + str(path))
    return value


@contextmanager
def open_study(path, writing=False):
    uri = (path / DB).resolve().as_uri() + ("?mode=rw" if writing else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=30)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE" if writing else "BEGIN")
        yield db


def keep(db, path, data=None):
    path = path.resolve(strict=True)
    if data is None:
        data = path.read_bytes()
    digest = sha(data)
    db.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)", (digest, data))
    return {"path": str(path), "sha256": digest}


def keep_json(db, path):
    data = path.read_bytes()
    value = json.loads(data)
    require(isinstance(value, dict), "expected a JSON object at " + str(path))
    return value, keep(db, path, data)


def preregister(spec, base):
    require(type(spec.get("schema_version")) is int and spec["schema_version"] == 1, "unsupported spec version")
    require(type(spec.get("seed")) is int, "seed must be an integer")
    text(spec.get("agent_model"), "agent_model")
    require(isinstance(spec.get("agent_settings", {}), dict), "agent_settings must be an object")
    require(isinstance(spec.get("tools"), list) and len(spec["tools"]) > 0, "tools must be declared")
    repeats = integer(spec.get("trajectories_per_arm"), "trajectories_per_arm")
    maximum = integer(spec.get("max_candidates"), "max_candidates")
    budget = number(spec.get("budget_npu_seconds"), "budget_npu_seconds", positive=True)
    require(isinstance(spec.get("families"), list) and bool(spec["families"]), "families must be declared")
    families = {}
    for entry in spec["families"]:
        require(isinstance(entry, dict), "family must be an object")
        family = text(entry.get("id"), "family id")
        require(family not in families, "duplicate family id")
        context_fields(entry.get("context", {}))
        candidates = entry.get("candidates")
        require(isinstance(candidates, list) and len(candidates) >= maximum, "family menu is too small")
        for candidate in candidates:
            text(candidate, "candidate")
        require(len(candidates) == len(set(candidates)) and "stop" not in candidates, "invalid candidate menu")
        npu = number(entry.get("baseline_npu_seconds"), "baseline_npu_seconds")
        minimum = number(entry.get("minimum_next_npu_seconds"), "minimum_next_npu_seconds", positive=True)
        minimum_confirm = number(entry.get("minimum_confirm_npu_seconds", minimum),
                                 "minimum_confirm_npu_seconds", positive=True)
        require(npu + minimum <= budget, "budget cannot admit the first screen")
        number(entry.get("baseline_goodput_tps"), "baseline_goodput_tps")
        workload_path = base / text(entry.get("workload"), "workload")
        brief_path = base / text(entry.get("brief"), "brief")
        workload = workload_path.read_bytes()
        rows = json.loads(workload) if workload.lstrip().startswith(b"[") else [
            json.loads(row) for row in workload.splitlines() if row.strip()]
        require(isinstance(rows, list) and rows and all(isinstance(row, dict) for row in rows),
                "workload must contain requests")
        if "max_requests" in entry:
            rows = rows[:integer(entry["max_requests"], "max_requests")]
        canonical = sha(encode(rows).encode("utf-8"))
        families[family] = {**entry, "context": entry.get("context", {}),
                            "minimum_confirm_npu_seconds": minimum_confirm,
                            "brief": load(brief_path), "workload_sha256": canonical,
                            "workload_path": workload_path, "brief_path": brief_path}
    require(len({entry["workload_sha256"] for entry in families.values()}) == len(families),
            "two families share a workload")
    return families, repeats


def history_snapshot(db, library, families):
    if library is None:
        return None, {name: [] for name in families}, set()
    uri = library.resolve(strict=True).as_uri() + "?mode=ro"
    with tempfile.TemporaryDirectory() as temp:
        copy = Path(temp) / "experience.sqlite"
        with closing(sqlite3.connect(uri, uri=True)) as source, closing(sqlite3.connect(copy)) as target:
            source.backup(target)
        reference = keep(db, copy)
        reference["path"] = str(library.resolve())
        cards = {name: search_cards(copy, family["context"]) for name, family in families.items()}
        # Every historical formal workload is checked, including records outside the retrieved results.
        with closing(sqlite3.connect(copy)) as frozen:
            known = set()
            for (payload,) in frozen.execute("SELECT payload FROM experience_rounds"):
                record = json.loads(payload)
                for assessment in record.get("evidence", {}).get("assessments", []):
                    for case in assessment.get("case_identity", {}).values():
                        if case.get("workload_sha256"):
                            known.add(case["workload_sha256"])
    return reference, cards, known


def initialize(study, spec_path, library):
    spec_path = spec_path.resolve(strict=True)
    spec = load(spec_path)
    families, repeats = preregister(spec, spec_path.parent)
    study.mkdir(parents=False)
    database = study / DB
    try:
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript("""
                CREATE TABLE plan (content TEXT NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE assignments (
                    slot INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, family TEXT NOT NULL,
                    pair_id INTEGER NOT NULL, arm TEXT NOT NULL
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY, trajectory TEXT NOT NULL REFERENCES assignments(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL
                );
                CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, content BLOB NOT NULL);
                CREATE TABLE charges (
                    sha256 TEXT PRIMARY KEY REFERENCES blobs(sha256), trajectory TEXT NOT NULL,
                    start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL
                );
                CREATE TABLE candidate_runs (id TEXT PRIMARY KEY, trajectory TEXT NOT NULL);
            """)
            history, cards, seen = history_snapshot(db, library, families)
            for family in families.values():
                require(family["workload_sha256"] not in seen, "frozen workload was previously indexed")
                for path_key in ("workload_path", "brief_path"):
                    family[path_key] = keep(db, family[path_key])
            known_before = sorted(seen)
            # Seed the assignment order and balance position within each paired repetition.
            rng = random.Random(spec["seed"])
            pairs = [(family, replicate) for family in sorted(families) for replicate in range(repeats)]
            rng.shuffle(pairs)
            first_arms = [ARMS[i % 2] for i in range(len(pairs))]
            rng.shuffle(first_arms)
            assignments = []
            for index, ((family, replicate), first) in enumerate(zip(pairs, first_arms), 1):
                for arm in (first, ARMS[1 - ARMS.index(first)]):
                    assigns = {"slot": len(assignments) + 1, "trajectory": uuid.uuid4().hex, "family": family,
                               "repeat": replicate + 1, "pair": index, "arm": arm}
                    assignments.append(assigns)
                    db.execute("INSERT INTO assignments VALUES (?, ?, ?, ?, ?)",
                               (assigns["slot"], assigns["trajectory"], family, index, arm))
            plan = {"schema_version": 1, "spec": spec, "spec_source": keep(db, spec_path),
                    "families": families, "history_source": history, "history": cards,
                    "history_workload_sha256": known_before, "assignments": assignments}
            raw = encode(plan)
            db.execute("INSERT INTO plan VALUES (?, ?)", (raw, sha(raw.encode("utf-8"))))
    except BaseException:
        database.unlink(missing_ok=True)
        study.rmdir()
        raise
    return {"plan_sha256": sha(raw.encode("utf-8")), "trajectories": len(assignments)}


def plan_and_hash(db):
    result = db.execute("SELECT content, sha256 FROM plan").fetchone()
    require(result is not None and sha(result[0].encode("utf-8")) == result[1], "study manifest hash mismatch")
    return json.loads(result[0]), result[1]


def events_for(db, trajectory):
    return [dict(kind=kind, data=json.loads(payload), at=at) for kind, payload, at in db.execute(
        "SELECT kind,payload,at FROM events WHERE trajectory=? ORDER BY id", (trajectory,))]


def append(db, trajectory, kind, data):
    db.execute("INSERT INTO events (trajectory,kind,payload,at) VALUES (?,?,?,?)",
               (trajectory, kind, encode(data), time.time()))


def state_of(plan, assignment, events):
    family = plan["families"][assignment["family"]]
    spent = family["baseline_npu_seconds"]
    candidates, outcomes = [], []
    pending, stop, confirm = None, None, None
    for event in events:
        kind, data = event["kind"], event["data"]
        if kind == "choice":
            chosen = data["candidate"]
            if chosen == "stop":
                stop = "agent_stop"
            else:
                candidates.append(chosen)
                pending = {"candidate": chosen, "stage": "screen"}
        elif kind == "observe":
            spent += data["npu_seconds"]
            outcomes.append(data)
            pending = None
            if spent > plan["spec"]["budget_npu_seconds"] + 1e-9:
                stop = "budget_exceeded"
            elif data["decision"] == "adopt":
                if data["stage"] == "confirm":
                    stop = "confirmed_benefit"
                    confirm = data
                else:
                    pending = {"candidate": data["candidate"], "stage": "confirm"}
        elif kind == "finish":
            stop = data["reason"]
    if len(candidates) >= plan["spec"]["max_candidates"] and pending is None and stop is None:
        stop = "candidate_limit"
    remaining = plan["spec"]["budget_npu_seconds"] - spent
    invalid = list(dict.fromkeys(row["candidate"] for row in outcomes if row["decision"] in ("revert", "defer")))
    return {"spent_npu_seconds": spent, "remaining_npu_seconds": remaining,
            "candidates": candidates, "pending": pending, "stop_reason": stop, "outcomes": outcomes,
            "invalid_candidates": invalid, "failed_candidates": len(invalid),
            "infrastructure_failures": sum(row["decision"] == "infrastructure_failure" for row in outcomes),
            "first_success_npu_seconds": spent if confirm else None,
            "first_success_npu_hours": spent / 3600 if confirm else None,
            "final_goodput_tps": confirm["goodput_tps"] if confirm else family["baseline_goodput_tps"]}


def current(db, trajectory):
    plan, _ = plan_and_hash(db)
    assignment = next((item for item in plan["assignments"] if item["trajectory"] == trajectory), None)
    require(assignment is not None, "unknown trajectory")
    family = plan["families"][assignment["family"]]
    events = events_for(db, trajectory)
    return plan, assignment, family, events, state_of(plan, assignment, events)


def packet(db, trajectory):
    plan, assigned, family, events, state = current(db, trajectory)
    require(state["stop_reason"] is None and state["pending"] is None, "trajectory is not awaiting an Agent choice")
    if events and events[-1]["kind"] == "packet":
        return events[-1]["data"]
    choices = [item for item in family["candidates"] if item not in state["candidates"]]
    if state["remaining_npu_seconds"] < family["minimum_next_npu_seconds"]:
        choices = []
    records = plan["history"][assigned["family"]] if assigned["arm"] == "history" else []
    result = {
        "schema_version": 1, "trajectory": trajectory, "agent_model": plan["spec"]["agent_model"],
        "agent_settings": plan["spec"].get("agent_settings", {}), "tools": plan["spec"]["tools"],
        "task": "Choose one listed candidate or stop. Return candidate, reasoning, prediction, falsifier and record_ids_used.",
        "context": family["context"], "baseline": family["brief"], "candidates": choices + ["stop"],
        "remaining_npu_seconds": state["remaining_npu_seconds"],
        "choices_remaining": plan["spec"]["max_candidates"] - len(state["candidates"]),
        "experience.search": {"records": records},
        "results": [{k: row[k] for k in ("candidate", "stage", "decision", "goodput_tps",
                                       "npu_seconds", "assessment_feedback")}
                    for row in state["outcomes"]],
    }
    append(db, trajectory, "packet", result)
    return result


def choose(db, trajectory, file):
    _, _, _, events, state = current(db, trajectory)
    choice, ref = keep_json(db, file)
    require(set(choice) == {"candidate", "reasoning", "prediction", "falsifier", "record_ids_used"},
            "choice fields do not match the declared Agent interface")
    if events and events[-1]["kind"] == "choice" and events[-1]["data"]["choice"] == choice:
        return events[-1]["data"]
    require(not state["pending"] and not state["stop_reason"] and events and events[-1]["kind"] == "packet",
            "a packet is required immediately before an Agent choice")
    offer = events[-1]["data"]
    candidate = text(choice.get("candidate"), "candidate")
    require(candidate in offer["candidates"], "candidate was not offered in this packet")
    for key in ("reasoning", "prediction", "falsifier"):
        text(choice.get(key), key)
    citations = choice.get("record_ids_used", [])
    require(isinstance(citations, list), "record_ids_used must be a list")
    for citation in citations:
        text(citation, "record id")
    visible = {row["record_id"] for row in offer["experience.search"]["records"]}
    require(set(citations) <= visible, "Agent cites a record outside its frozen tool response")
    result = dict(choice, choice=choice, source=ref)
    append(db, trajectory, "choice", result)
    return result


def charged_interval(charge):
    start = charge.get("start_mono_ns", charge.get("started_mono_ns"))
    end = charge.get("released_mono_ns", charge.get("end_mono_ns"))
    released = charge.get("real_card_cleanup", charge.get("release_verified"))
    require(type(start) is int and type(end) is int and 0 <= start < end, "invalid monotonic device interval")
    require(released is True, "NPU cleanup must be verified")
    measured = (end - start) / 1e9
    seconds = number(charge.get("seconds", measured), "charge seconds", positive=True)
    require(math.isclose(seconds, measured, abs_tol=1e-8, rel_tol=1e-9), "charge disagrees with monotonic interval")
    return start, end, seconds


def record_charges(db, trajectory, charge_path, charge, charge_ref):
    files = charge.get("leases")
    if files is None:
        items = [(charge, charge_ref)]
    else:
        require(isinstance(files, list) and files, "stage charge needs one or more lease receipts")
        items = [keep_json(db, charge_path.parent / text(name, "lease receipt path")) for name in files]
        contexts = {(item.get("host"), item.get("boot_id"), tuple(item.get("devices", []))) for item, _ in items}
        require(len(contexts) == 1 and all(host and boot and len(devices) == 1 for host, boot, devices in contexts),
                "stage receipts must refer to the same single-die clock domain")
    clock = None
    first = items[0][0]
    if first.get("host") and first.get("boot_id") and first.get("devices"):
        clock = {key: first[key] for key in ("host", "boot_id", "devices")}
        for (value,) in db.execute("SELECT payload FROM events WHERE kind='observe'"):
            old_clock = json.loads(value).get("site_clock")
            require(old_clock is None or old_clock == clock, "device or monotonic clock changed within study")
    costs = []
    for item, reference in items:
        start, end, seconds = charged_interval(item)
        if "proof_sha256" in item:
            proof_path = Path(reference["path"]).parent / text(item.get("proof_path"), "release proof")
            if not proof_path.is_file():
                proof_path = Path(reference["path"]).parent / proof_path.name
            proof, proof_ref = keep_json(db, proof_path)
            require(proof_ref["sha256"] == item["proof_sha256"] and proof.get("free") is True
                    and proof.get("host") == item.get("host") and proof.get("boot_id") == item.get("boot_id")
                    and proof.get("devices") == item.get("devices")
                    and type(proof.get("observed_mono_ns")) is int and start < proof["observed_mono_ns"] <= end,
                    "device-release evidence differs from lease receipt")
        overlap = db.execute("SELECT 1 FROM charges WHERE ? < end_ns AND ? > start_ns", (start, end)).fetchone()
        require(overlap is None, "device interval overlaps another assigned observation")
        db.execute("INSERT INTO charges VALUES (?, ?, ?, ?)", (reference["sha256"], trajectory, start, end))
        costs.append(seconds)
    seconds = math.fsum(costs)
    if files is not None:
        require(math.isclose(number(charge.get("seconds"), "stage charge seconds", positive=True), seconds,
                             abs_tol=1e-8, rel_tol=1e-9), "stage charge is not the sum of its device leases")
    return seconds, clock


def observe(db, trajectory, file):
    plan, _, family, events, state = current(db, trajectory)
    receipt, ref = keep_json(db, file)
    for event in events:
        if event["kind"] == "observe" and event["data"]["source"]["sha256"] == ref["sha256"]:
            return event["data"]
    require(type(receipt.get("schema_version")) is int and receipt["schema_version"] == 1, "unsupported receipt version")
    stage, candidate, decision = receipt.get("stage"), receipt.get("candidate"), receipt.get("decision")
    require(stage in STAGES and decision in DECISIONS, "invalid observation stage or decision")
    require(not state["stop_reason"] and state["pending"] == {"stage": stage, "candidate": candidate},
            "receipt does not match the pending candidate and stage")
    minimum = (family["minimum_confirm_npu_seconds"] if stage == "confirm" else family["minimum_next_npu_seconds"])
    require(state["remaining_npu_seconds"] >= minimum, "stage start violated pre-registered budget admission")
    charge_path = file.parent / text(receipt.get("charge"), "charge")
    charge, charge_ref = keep_json(db, charge_path)
    seconds, site_clock = record_charges(db, trajectory, charge_path, charge, charge_ref)
    assessment_ref, goodput, run_ids, feedback = None, receipt.get("goodput_tps"), [], {}
    if decision == "infrastructure_failure":
        require(goodput is None and (charge.get("error") or receipt.get("error")), "failure requires recorded error")
    else:
        number(goodput, "goodput_tps")
        assessment_path = file.parent / text(receipt.get("assessment"), "assessment")
        assessment, assessment_ref = keep_json(db, assessment_path)
        require(assessment.get("recommendation") == decision, "receipt disagrees with formal assessment")
        if "phase" in assessment:
            require(assessment["phase"] == stage, "assessment phase differs from receipt")
        if "goodput_tps_median" in assessment:
            require(math.isclose(assessment["goodput_tps_median"], goodput, abs_tol=1e-9),
                    "receipt disagrees with measured goodput")
        run_ids = [
            text(run["run_id"], "charge measurement run_id")
            for run in charge.get("measurements", []) if run.get("run_id")
        ]
        checked_runs = [text(run["run_id"], "assessment candidate run_id") for run in assessment.get("runs", [])
                        if run.get("side") == "candidate"]
        if not run_ids:
            run_ids = checked_runs
        elif checked_runs:
            require(set(run_ids) == set(checked_runs), "charge and assessment have different candidate runs")
        require(run_ids and len(run_ids) == len(set(run_ids)), "candidate runs must be independently identified")
        goodputs = assessment.get("goodput", {})
        if goodputs:
            measured = statistics.median(goodputs[run_id]["output_tokens_per_s"] for run_id in run_ids)
            require(math.isclose(measured, goodput, abs_tol=1e-9), "receipt disagrees with formal candidate goodput")
        feedback = {k: assessment[k] for k in ("goodput_primary", "guards") if k in assessment}
        for run_id in run_ids:
            require(not db.execute("SELECT 1 FROM candidate_runs WHERE id=?", (run_id,)).fetchone(),
                    "candidate run reused in another observation")
        db.executemany("INSERT INTO candidate_runs VALUES (?, ?)", ((run_id, trajectory) for run_id in run_ids))
    result = {"candidate": candidate, "stage": stage, "decision": decision, "goodput_tps": goodput,
              "npu_seconds": seconds, "charge": charge_ref, "assessment": assessment_ref,
              "run_ids": run_ids, "source": ref, "assessment_feedback": feedback, "site_clock": site_clock}
    append(db, trajectory, "observe", result)
    return result


def finish(db, trajectory, reason, note):
    _, _, family, _, state = current(db, trajectory)
    if state["stop_reason"] == reason:
        return {"reason": reason, "note": note}
    require(not state["stop_reason"], "trajectory already stopped")
    pending = state["pending"]
    threshold = family["minimum_confirm_npu_seconds"] if pending and pending["stage"] == "confirm" else family["minimum_next_npu_seconds"]
    if reason == "budget":
        require(state["remaining_npu_seconds"] < threshold, "budget still supports the next stage")
    if reason == "infrastructure_failure":
        require(state["infrastructure_failures"] > 0, "no recorded infrastructure failure")
    result = {"reason": reason, "note": text(note, "stop note")}
    append(db, trajectory, "finish", result)
    return result


def summarize(db):
    plan, digest = plan_and_hash(db)
    trajectories = []
    for assignment in plan["assignments"]:
        events = events_for(db, assignment["trajectory"])
        state = state_of(plan, assignment, events)
        packets = [event["data"] for event in events if event["kind"] == "packet"]
        choices = [event["data"] for event in events if event["kind"] == "choice"]
        trajectory = {
            **assignment, **state, "right_censored": bool(state["stop_reason"]) and state["first_success_npu_seconds"] is None,
            "tool_available": True, "library_enabled": assignment["arm"] == "history",
            "records_returned": sorted({r["record_id"] for p in packets for r in p["experience.search"]["records"]}),
            "records_cited": sorted({record for c in choices for record in c["record_ids_used"]}),
            "agent_stopping_reason": next((c["reasoning"] for c in choices if c["candidate"] == "stop"), None),
        }
        trajectories.append(trajectory)

    def group(subset):
        completed = [row for row in subset if row["stop_reason"]]
        confirmed = [row for row in completed if row["first_success_npu_seconds"] is not None]
        def median(rows, key):
            return statistics.median(row[key] for row in rows) if rows else None
        return {
            "planned": len(subset), "completed": len(completed), "confirmed": len(confirmed),
            "confirmation_fraction": len(confirmed) / len(subset) if subset and len(completed) == len(subset) else None,
            "right_censored": sum(row["right_censored"] for row in completed),
            "median_first_success_npu_hours_confirmed_only": median(confirmed, "first_success_npu_hours"),
            "median_spent_npu_hours": (median(completed, "spent_npu_seconds") / 3600 if completed else None),
            "median_invalid_candidates": median(completed, "failed_candidates"),
            "median_final_goodput_tps": median(completed, "final_goodput_tps"),
        }
    arms = {arm: group([row for row in trajectories if row["arm"] == arm]) for arm in ARMS}
    strata = {name: {arm: group([row for row in trajectories if row["family"] == name and row["arm"] == arm])
                     for arm in ARMS} for name in plan["families"]}
    pairs = []
    for pair in sorted({row["pair"] for row in trajectories}):
        paired = {row["arm"]: row for row in trajectories if row["pair"] == pair}
        historical, control = (paired[arm] for arm in ARMS)
        finished = all(row["stop_reason"] for row in paired.values())
        times = [row["first_success_npu_seconds"] for row in (historical, control)]
        pairs.append({"pair": pair, "family": historical["family"], "completed": finished,
                      "same_candidate_sequence": historical["candidates"] == control["candidates"] if finished else None,
                      "delta_first_success_npu_seconds": times[0] - times[1] if None not in times else None,
                      "delta_final_goodput_tps": historical["final_goodput_tps"] - control["final_goodput_tps"] if finished else None})
    return {"schema_version": 1, "plan_sha256": digest, "budget_npu_seconds": plan["spec"]["budget_npu_seconds"],
            "agent_model": plan["spec"]["agent_model"], "arms": arms, "families": strata,
            "paired_comparisons": pairs, "trajectories": trajectories}


def export(db, output):
    plan, digest = plan_and_hash(db)
    rows = {assignment["trajectory"]: events_for(db, assignment["trajectory"]) for assignment in plan["assignments"]}
    blobs = list(db.execute("SELECT sha256,content FROM blobs ORDER BY sha256"))
    require(all(sha(data) == checksum for checksum, data in blobs), "evidence blob checksum mismatch")
    output.mkdir()
    objects = output / "blobs"
    objects.mkdir()
    for checksum, data in blobs:
        (objects / checksum).write_bytes(data)
    (output / "transcript.json").write_text(json.dumps({"plan": plan, "plan_sha256": digest, "events": rows},
                                                        indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output / "report.json").write_text(json.dumps(summarize(db), indent=2, ensure_ascii=False) + "\n",
                                        encoding="utf-8")
    return {"directory": str(output.resolve()), "blobs": len(blobs)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "dispatch", "packet", "choose", "observe", "finish", "report", "export"):
        command = commands.add_parser(name)
        command.add_argument("--study", required=True, type=Path)
        if name in ("packet", "choose", "observe", "finish"):
            command.add_argument("--trajectory", required=True)
    commands.choices["init"].add_argument("--spec", required=True, type=Path)
    commands.choices["init"].add_argument("--library", type=Path)
    commands.choices["choose"].add_argument("--choice", required=True, type=Path)
    commands.choices["observe"].add_argument("--receipt", required=True, type=Path)
    commands.choices["finish"].add_argument("--reason", required=True, choices=FINISH_REASONS)
    commands.choices["finish"].add_argument("--note", required=True)
    commands.choices["export"].add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = initialize(args.study, args.spec, args.library)
        else:
            with open_study(args.study, args.command in ("packet", "choose", "observe", "finish")) as db:
                if args.command == "dispatch":
                    plan, digest = plan_and_hash(db)
                    result = {"plan_sha256": digest, "assignments": plan["assignments"]}
                elif args.command == "packet":
                    result = packet(db, args.trajectory)
                elif args.command == "choose":
                    result = choose(db, args.trajectory, args.choice)
                elif args.command == "observe":
                    result = observe(db, args.trajectory, args.receipt)
                elif args.command == "finish":
                    result = finish(db, args.trajectory, args.reason, args.note)
                elif args.command == "report":
                    result = summarize(db)
                else:
                    result = export(db, args.output)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        parser.exit(2, "experience study: " + str(exc) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

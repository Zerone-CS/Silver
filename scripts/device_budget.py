#!/usr/bin/env python3
"""Durable device occupancy accounting on the site worker's control host."""

import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, label, *, allow_zero=False):
    require(type(value) in (int, float) and math.isfinite(value)
            and (value >= 0 if allow_zero else value > 0), f"invalid {label}")
    return value


def tick(value):
    require(type(value) is int and 0 < value < 2**63, "invalid monotonic timestamp")
    return value


def label(value, field):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= 240, f"invalid {field}")
    return value


def devices_of(devices):
    require(isinstance(devices, (list, tuple)) and bool(devices), "device list required")
    names = [label(value, "device") for value in devices]
    require(len(names) == len(set(names)), "duplicate device names")
    return sorted(names)


class InsufficientDeviceBudget(ValueError):
    """The next lease and its reserve do not fit the remaining budget."""


class Budget:
    """The site owns the actual device lock and persists claim/release around it."""

    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)

    @classmethod
    def initialize(cls, path, limit_die_seconds):
        path = Path(path)
        number(limit_die_seconds, "device budget")
        with path.open("xb"):
            pass
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE policy(limit_die_seconds REAL NOT NULL)")
            db.execute("INSERT INTO policy VALUES(?)", (limit_die_seconds,))
            db.execute(
                """CREATE TABLE leases(
                    action_id TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    boot_id TEXT NOT NULL,
                    devices TEXT NOT NULL,
                    start_mono_ns INTEGER NOT NULL,
                    max_seconds REAL NOT NULL,
                    reserve_die_seconds REAL NOT NULL,
                    cleanup INTEGER NOT NULL,
                    end_mono_ns INTEGER,
                    outcome TEXT,
                    proof_path TEXT,
                    proof_sha256 TEXT,
                    proof_json BLOB
                )"""
            )
        return cls(path)

    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def row_data(row):
        doc = dict(row)
        doc.pop("proof_json")
        doc["devices"] = json.loads(doc["devices"])
        doc["cleanup"] = bool(doc["cleanup"])
        doc["die_seconds"] = (
            (doc["end_mono_ns"] - doc["start_mono_ns"]) / 1e9 * len(doc["devices"])
            if doc["end_mono_ns"] is not None else None
        )
        doc["action_overrun"] = doc["end_mono_ns"] is not None and (
            doc["end_mono_ns"] - doc["start_mono_ns"] > doc["max_seconds"] * 1e9
        )
        return doc

    @classmethod
    def summary(cls, db):
        limit = db.execute("SELECT limit_die_seconds FROM policy").fetchone()[0]
        leases = [cls.row_data(row) for row in db.execute("SELECT * FROM leases ORDER BY rowid")]
        used = math.fsum(lease["die_seconds"] for lease in leases if lease["die_seconds"] is not None)
        pending = [lease["action_id"] for lease in leases if lease["end_mono_ns"] is None]
        return {"limit_die_seconds": limit, "used_die_seconds": used,
                "remaining_die_seconds": max(0.0, limit - used),
                "overrun_die_seconds": max(0.0, used - limit), "pending": pending, "leases": leases}

    def status(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN")
            return self.summary(db)

    def claim(self, action_id, host, boot_id, devices, start_mono_ns, max_seconds,
              reserve_seconds=0, cleanup=False):
        action_id = label(action_id, "action ID")
        host = label(host, "host")
        boot_id = label(boot_id, "boot ID")
        devices = devices_of(devices)
        start = tick(start_mono_ns)
        number(max_seconds, "maximum lease seconds")
        number(reserve_seconds, "reserved die-seconds", allow_zero=True)
        require(type(cleanup) is bool, "cleanup must be boolean")
        args = (action_id, host, boot_id, json.dumps(devices), start,
                max_seconds, reserve_seconds, int(cleanup))
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM leases WHERE action_id=?", (action_id,)).fetchone()
            if existing is not None:
                require(tuple(existing)[:len(args)] == args, "action ID already used with a different claim")
                return {**self.row_data(existing), "existing": True}
            state = self.summary(db)
            require(not state["pending"], "reconcile pending device claim before starting another")
            need = len(devices) * max_seconds + reserve_seconds
            require(math.isfinite(need), "lease budget is not finite")
            if not cleanup and need > state["remaining_die_seconds"]:
                raise InsufficientDeviceBudget("insufficient device budget for lease and reserve")
            for lease in state["leases"]:
                if (lease["host"] == host and lease["boot_id"] == boot_id
                        and set(devices).intersection(lease["devices"])):
                    require(start >= lease["end_mono_ns"], "device lease timestamps overlap or run backward")
            db.execute(
                """INSERT INTO leases(action_id,host,boot_id,devices,start_mono_ns,max_seconds,
                   reserve_die_seconds,cleanup) VALUES(?,?,?,?,?,?,?,?)""", args
            )
            row = db.execute("SELECT * FROM leases WHERE action_id=?", (action_id,)).fetchone()
            return {**self.row_data(row), "existing": False}

    def release(self, action_id, end_mono_ns, proof_path, outcome):
        tick(end_mono_ns)
        label(outcome, "outcome")
        sample = Path(proof_path).resolve(strict=True)
        payload = sample.read_bytes()
        proof = json.loads(payload)
        require(isinstance(proof, dict), "release proof must be an object")
        digest = hashlib.sha256(payload).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM leases WHERE action_id=?", (action_id,)).fetchone()
            require(row is not None, "unknown lease action ID")
            require(end_mono_ns > row["start_mono_ns"], "release must follow device claim")
            require(proof.get("free") is True
                    and proof.get("host") == row["host"] and proof.get("boot_id") == row["boot_id"]
                    and devices_of(proof.get("devices")) == json.loads(row["devices"]),
                    "release proof does not show the claimed device is free on the same host boot")
            observed = tick(proof["observed_mono_ns"])
            require(row["start_mono_ns"] < observed <= end_mono_ns,
                    "release proof was not observed during this claim")
            receipt = (end_mono_ns, outcome, str(sample), digest)
            if row["end_mono_ns"] is not None:
                require(tuple(row)[8:12] == receipt, "release already recorded with different evidence")
                return self.row_data(row)
            db.execute(
                """UPDATE leases SET end_mono_ns=?,outcome=?,proof_path=?,proof_sha256=?,
                   proof_json=? WHERE action_id=?""", (*receipt, payload, action_id)
            )
            row = db.execute("SELECT * FROM leases WHERE action_id=?", (action_id,)).fetchone()
            return self.row_data(row)

    def audit(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN")
            for row in db.execute("SELECT proof_path,proof_sha256,proof_json FROM leases WHERE end_mono_ns IS NOT NULL"):
                require(hashlib.sha256(row["proof_json"]).hexdigest() == row["proof_sha256"],
                        "embedded release proof changed")
                require(Path(row["proof_path"]).read_bytes() == row["proof_json"],
                        "site release proof changed: " + row["proof_path"])
            return self.summary(db)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    commands = {name: subparsers.add_parser(name) for name in ("init", "status", "claim", "release", "audit")}
    for command in commands.values():
        command.add_argument("--db", type=Path, required=True)
    commands["init"].add_argument("--limit-die-seconds", type=float, required=True)
    for name in ("claim", "release"):
        commands[name].add_argument("--action-id", required=True)
    cmd = commands["claim"]
    for name in ("host", "boot-id"):
        cmd.add_argument("--" + name, required=True)
    cmd.add_argument("--device", action="append", required=True)
    cmd.add_argument("--start-mono-ns", type=int, required=True)
    cmd.add_argument("--max-seconds", type=float, required=True)
    cmd.add_argument("--reserve-die-seconds", type=float, default=0)
    cmd.add_argument("--cleanup", action="store_true")
    cmd = commands["release"]
    cmd.add_argument("--end-mono-ns", type=int, required=True)
    cmd.add_argument("--proof", type=Path, required=True)
    cmd.add_argument("--outcome", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = Budget.initialize(args.db, args.limit_die_seconds).status()
        else:
            budget = Budget(args.db)
            if args.command == "claim":
                result = budget.claim(args.action_id, args.host, args.boot_id, args.device,
                                      args.start_mono_ns, args.max_seconds,
                                      reserve_seconds=args.reserve_die_seconds, cleanup=args.cleanup)
            elif args.command == "release":
                result = budget.release(args.action_id, args.end_mono_ns, args.proof, args.outcome)
            else:
                result = getattr(budget, args.command)()
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

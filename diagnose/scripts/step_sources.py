#!/usr/bin/env python3
"""Freeze and read MS Service Profiler and run-bound site CSV evidence."""
import argparse
import csv
import io
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from diagnose.scripts.profile import file_hash, read_json


# The native 26.0 manual distinguishes scheduling, model execution and speculation.
# Preserve the publisher's names and units in this descriptive view.
NATIVE = {
    "ms_batch": ("batch.csv", "name", ("during_time(ms)", "batch_size", "prefill_batch_size",
        "decode_batch_size", "prefill_scheduled_tokens", "decode_scheduled_tokens",
        "total_scheduled_tokens", "used_blocks", "free_blocks", "total_blocks")),
    "ms_request": ("request.csv", "http_rid", ("recv_token_size", "reply_token_size",
        "execution_time(ms)", "queue_wait_time(ms)", "first_token_latency(ms)")),
    "ms_kvcache": ("kvcache.csv", "name", ("used_blocks", "free_blocks", "total_blocks",
        "blocks_allocated", "blocks_freed")),
}
WORK = ("prefill_tokens", "decode_tokens", "draft_tokens", "accepted_draft_tokens",
        "recomputed_tokens", "prefix_hit_tokens", "num_preemptions")
IDS = {"request_id", "backend_id", "response_id"}
BIND = {"run_id", "capture_id", "manifest_sha256"}
KEY = {"replica", "owner_rank", "step_id"}
IDENTIFIERS = IDS | BIND | KEY
COLUMNS = {
    "request_map": BIND | {"request_id", "response_id", "sse_sha256"},
    "bridge": BIND | {"response_id", "backend_id"},
    "seal": BIND | {"replica", "expected_steps"},
    "inventory": BIND | KEY | {"duration_us", "duration_ms"},
    "outputs": BIND | KEY | IDS | {"output_start", "output_count"},
    "work": BIND | KEY | IDS | set(WORK),
}
NULLS = {"", "<NA>", "NA", "NaN", "nan", "None", "null", "-"}
FORMAT = "run-step-csv-v1"


def require(test, message):
    if not test:
        raise ValueError(message)


def integer(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]+", value),
            "expected nonnegative integer, got " + repr(value))
    return int(value)


def number(text):
    value = float(text)
    require(math.isfinite(value) and value >= 0, "invalid nonnegative measurement: " + text)
    return value


def check_table(t):
    kind = t["kind"]
    require(kind in NATIVE or kind in COLUMNS, "unsupported table kind: " + str(kind))
    extra = set(t) - {"kind", "path", "sha256", "columns", "constants", "where"}
    require(not extra, "unknown table options: " + str(sorted(extra)))
    require(isinstance(t["path"], str) and bool(t["path"]), "missing CSV path")
    for key in ("columns", "where", "constants"):
        value = t.get(key, {})
        require(isinstance(value, dict) and all(isinstance(k, str) and k and isinstance(v, str) and v
                for k, v in value.items()), "invalid table option: " + key)
    cols, constants = t.get("columns", {}), t.get("constants", {})
    require(not (set(cols) & set(constants)), "a field has two origins")
    if kind in NATIVE:
        require(not cols and not constants, "native presets use their documented headers")
        return
    require(set(constants) <= {"replica", "owner_rank"}, "only replica and owner_rank may be constant")
    fields = set(cols) | set(constants)
    require(fields <= COLUMNS[kind], "unknown canonical fields: " + str(sorted(fields - COLUMNS[kind])))
    require(len(set(cols.values())) == len(cols), "one source column mapped to multiple logical fields")
    require(len(fields & IDS) <= 1 or kind in ("request_map", "bridge"), "ambiguous request identity")
    require(not {"duration_us", "duration_ms"} <= fields, "ambiguous duration unit")


def read_rows(table, path, visit, missing):
    # res_list in large serving batches can exceed the csv module's 128 KiB default.
    csv.field_size_limit(sys.maxsize)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, strict=True)
        headers = reader.fieldnames
        require(headers and all(headers) and len(headers) == len(set(headers)), "invalid CSV header: " + str(path))
        filters = table.get("where", {})
        needed = set(filters) | set(table.get("columns", {}).values())
        if table["kind"] in NATIVE:
            needed.add(NATIVE[table["kind"]][1])
        absent = needed - set(headers)
        if absent:
            missing["absent columns: " + ", ".join(sorted(absent))] += 1
            return
        for row in reader:
            where = {"path": str(path), "line": reader.line_num}
            require(None not in row and None not in row.values(), "ragged CSV: " + str(where))
            if all(row[key] == value for key, value in filters.items()):
                try:
                    visit(row, where)
                except ValueError as exc:
                    raise ValueError("{}:{}: {}".format(path, reader.line_num, exc)) from exc


def native_row(summary, kind, row, where):
    # No addition across devices, request and batch phases, or cumulative counters.
    phase = row.get("name", "") if kind != "ms_request" else "request"
    key = (row.get("hostname", ""), row.get("dp_rank", ""), row.get("prof_id", ""), phase)
    group = summary.setdefault(key, {"hostname": key[0], "dp_rank": key[1], "prof_id": key[2],
                                     "name": phase, "rows": 0, "fields": {}})
    group["rows"] += 1
    for col in NATIVE[kind][2]:
        text = row.get(col, "").strip()
        if text in NULLS:
            continue
        if col in ("blocks_allocated", "blocks_freed"):
            v = float(text)
            require(math.isfinite(v), "invalid KV block delta: " + text)
        else:
            v = number(text)
        stats = group["fields"].setdefault(col, {"observed": 0, "min": v, "max": v,
                                                 "min_source": where, "max_source": where})
        stats["observed"] += 1
        if v < stats["min"]:
            stats.update(min=v, min_source=where)
        if v > stats["max"]:
            stats.update(max=v, max_source=where)


def run_context(run_dir):
    p = Path(run_dir) / "manifest.json"
    value = read_json(p)
    run_id = value.get("run_id")
    require(isinstance(run_id, str) and bool(run_id), "missing benchmark run_id")
    return run_id, file_hash(p)


def verified_sse_maps(run_dir, docs):
    """Use the same replay and benchmark validation as the public request-map command."""
    from diagnose.scripts.request_map import request_map
    path = Path(run_dir).resolve(strict=True)
    workload = read_json(path / "workload.json")
    offsets = {entry["request_id"]: i for i, entry in enumerate(workload)}
    pairs = {}
    for row in csv.DictReader(io.StringIO(request_map(path), newline="")):
        req = row["request_id"]
        raw = path / "raw" / ("%08d.sse" % offsets[req])
        docs[str(raw.resolve(strict=True))] = row["sse_sha256"]
        pairs[req] = (row["response_id"], row["sse_sha256"])
    return pairs


def check_step_definition(definition, tables):
    if definition is not None or any(table["kind"] == "inventory" for table in tables):
        require(isinstance(definition, str) and definition.strip(),
                "declare step_definition for the mapped engine inventory")


def evidence_files(refs, base):
    """Check originals used by a converter, in addition to the normalized CSV tables."""
    require(isinstance(refs, list), "evidence_files must list path and sha256 pairs")
    result = {}
    for ref in refs:
        require(isinstance(ref, dict) and set(ref) == {"path", "sha256"}
                and isinstance(ref["path"], str) and ref["path"], "invalid source evidence reference")
        path = (base / ref["path"]).resolve(strict=True)
        actual = file_hash(path)
        require(actual == ref["sha256"], "source evidence fingerprint mismatch: " + str(path))
        result[str(path)] = actual
    return result


def prepare(run_dir, ms_dir, mapping, version, output):
    """Create an exclusive new spec. Keep hashes supplied by the operator authoritative."""
    options = read_json(mapping) if mapping else {}
    require(isinstance(options, dict) and not set(options) -
            {"tables", "owner_ranks", "scope", "step_definition", "evidence_files"},
            "unsupported mapping options")
    root = Path(mapping).resolve().parent if mapping else Path.cwd()
    tables = list(options.get("tables", []))
    if ms_dir is not None:
        source = Path(ms_dir).resolve(strict=True)
        require(source.is_dir(), "expected MS export directory")
        for kind, (name, _, _) in NATIVE.items():
            if (source / name).is_file():
                tables.append({"kind": kind, "path": str(source / name)})
    require(tables, "no recognized CSV files or mapped tables")
    check_step_definition(options.get("step_definition"), tables)
    for t in tables:
        check_table(t)
        path = (root / t["path"]).resolve(strict=True)
        digest = file_hash(path)
        require(t.get("sha256", digest) == digest, "source changed: " + str(path))
        t.update(path=str(path), sha256=digest)
    run_id, manifest_sha = run_context(run_dir)
    spec = {"schema_version": 1, "format": FORMAT, "tool_version": version, "run_id": run_id,
            "manifest_sha256": manifest_sha, "scope": options.get("scope", "partial"),
            "step_definition": options.get("step_definition"),
            "owner_ranks": options.get("owner_ranks", {}), "tables": tables}
    if "evidence_files" in options:
        originals = evidence_files(options["evidence_files"], root)
        spec["evidence_files"] = [{"path": path, "sha256": digest} for path, digest in sorted(originals.items())]
    require(isinstance(version, str) and version.strip(), "missing export tool version")
    with Path(output).open("x", encoding="utf-8") as handle:
        json.dump(spec, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")
    return spec


def read_sources(spec_path, run_dir):
    spec_path = Path(spec_path).resolve(strict=True)
    spec_sha = file_hash(spec_path)
    spec = read_json(spec_path)
    run_id, manifest_sha = run_context(run_dir)
    require(type(spec.get("schema_version")) is int and spec["schema_version"] == 1 and spec.get("format") == FORMAT,
            "unsupported step CSV spec")
    require(isinstance(spec.get("tool_version"), str) and spec["tool_version"].strip(), "missing export tool version")
    require(spec.get("run_id") == run_id and spec.get("manifest_sha256") == manifest_sha, "spec bound to a different run")
    require(spec.get("scope") in ("full_run", "partial"), "invalid step scope")
    tables = spec["tables"]
    require(isinstance(tables, list) and tables, "no source tables")
    check_step_definition(spec.get("step_definition"), tables)
    owners = spec.get("owner_ranks", {})
    require(isinstance(owners, dict) and all(isinstance(k, str) and k and isinstance(v, str) and v
            for k,v in owners.items()), "invalid owner_ranks")
    metadata_path = Path(run_dir) / "metadata.json"
    capture_id = read_json(metadata_path).get("profile_capture_id") if metadata_path.is_file() else None
    docs = evidence_files(spec.get("evidence_files", []), spec_path.parent)
    docs[str(spec_path)] = spec_sha
    obs = {"schema_version": 1, "run_id": run_id, "manifest_sha256": manifest_sha,
           "step_definition": spec.get("step_definition"), "scope": spec["scope"], "inventory": [], "outputs": [],
           "work": [], "expected_steps_by_replica": None, "inventory_evidence": None,
           "native_observations": [], "unresolved": []}
    response_ids, backend_ids, mapped_requests, reverse_bridges = {}, {}, {}, {}
    sse_maps = None
    seals = {}
    seal_source = None
    inventory_sources = set()
    for t in tables:
        check_table(t)
    # Process identity bridges before any rows that use them.
    for t in sorted(tables, key=lambda x: x["kind"] not in ("bridge", "request_map")):
        kind = t["kind"]
        path = (spec_path.parent / t["path"]).resolve(strict=True)
        digest = file_hash(path)
        require(t.get("sha256") == digest, "input fingerprint mismatch: " + str(path))
        docs[str(path)] = digest
        missing = Counter()
        native_groups = {}

        def visit(row, where):
            nonlocal seal_source, sse_maps
            if kind in NATIVE:
                native_row(native_groups, kind, row, where)
                return
            v = {}
            for field, column in t.get("columns", {}).items():
                value = row[column]
                if field in IDENTIFIERS:
                    if value.strip():
                        v[field] = value
                elif value.strip() not in NULLS:
                    v[field] = value.strip()
            v.update(t.get("constants", {}))
            if not set(v).intersection({"run_id", "capture_id"}):
                missing["run or capture ID missing"] += 1
                return
            if "run_id" in v and v["run_id"] != run_id:
                return
            if "capture_id" in v:
                if not capture_id:
                    missing["benchmark does not contain profile_capture_id"] += 1
                    return
                if v["capture_id"] != capture_id:
                    require("run_id" not in v, "capture_id contradicts matching run_id")
                    return
            require("manifest_sha256" not in v or v["manifest_sha256"] == manifest_sha,
                    "row manifest contradicts matching run")
            if kind in ("request_map", "bridge"):
                key, target, ids, reverse = (("response_id", "request_id", response_ids, mapped_requests)
                                             if kind == "request_map" else
                                             ("backend_id", "response_id", backend_ids, reverse_bridges))
                if key not in v or target not in v:
                    missing["incomplete request ID mapping"] += 1
                    return
                if kind == "request_map" and "sse_sha256" in t["columns"]:
                    require("sse_sha256" in v, "source SSE SHA-256 missing")
                    if sse_maps is None:
                        sse_maps = verified_sse_maps(run_dir, docs)
                    require(sse_maps.get(v[target]) == (v[key], v["sse_sha256"]),
                            "mapping differs from the retained SSE response")
                require(ids.setdefault(v[key], v[target]) == v[target] and
                        reverse.setdefault(v[target], v[key]) == v[key], "conflicting request ID mapping")
                return
            if kind == "seal":
                if "expected_steps" not in v or "replica" not in v:
                    missing["independent inventory total missing"] += 1
                    return
                require(seal_source in (None, where["path"]), "use one independent inventory seal file")
                seal_source = where["path"]
                require(v["replica"] not in seals, "duplicate inventory seal for replica")
                seals[v["replica"]] = integer(v["expected_steps"])
                return
            if not KEY <= set(v):
                missing["replica, owner rank or real step ID unavailable"] += 1
                return
            replica = v["replica"]
            if replica not in owners:
                missing["logical owner unavailable for replica " + replica] += 1
                return
            if owners[replica] != v["owner_rank"]:
                return
            record = {k: v[k] for k in KEY}
            record["source"] = where
            if kind == "inventory":
                duration = None
                if "duration_us" in v:
                    duration = number(v["duration_us"])
                if "duration_ms" in v:
                    duration = number(v["duration_ms"]) * 1000
                    require(math.isfinite(duration), "duration conversion overflow")
                record["duration_us"] = duration
            else:
                request = v.get("request_id")
                if "response_id" in v:
                    request = response_ids.get(v["response_id"])
                if "backend_id" in v:
                    request = response_ids.get(backend_ids.get(v["backend_id"]))
                if not request:
                    missing["request identity unresolved"] += 1
                    return
                record["request_id"] = request
                if kind == "outputs":
                    if not {"output_start", "output_count"} <= set(v):
                        missing["output offset or count missing"] += 1
                        return
                    record.update(output_start=integer(v["output_start"]), output_count=integer(v["output_count"]))
                    require(record["output_count"] > 0, "output commit count must be positive")
                else:
                    record.update({k: integer(v[k]) for k in WORK if k in v})
            obs[kind].append(record)

        try:
            read_rows(t, path, visit, missing)
        except csv.Error as exc:
            raise ValueError("invalid CSV {}: {}".format(path, exc)) from exc
        if kind == "inventory":
            inventory_sources.add(digest)
        if kind in NATIVE:
            obs["native_observations"].append({"kind": kind, "path": str(path), "sha256": digest,
                "tool_version": spec["tool_version"], "binding": "operator_paired",
                "groups": list(native_groups.values())})
        if missing:
            obs["unresolved"].extend("{} {} ({}): {}".format(path.name, kind, n, reason)
                                     for reason, n in sorted(missing.items()))
            if kind in ("inventory", "seal", "outputs"):
                obs["scope"] = "partial"
    if seal_source:
        require(docs[seal_source] not in inventory_sources, "inventory cannot supply its own completeness seal")
        if set(seals) != set(owners):
            obs["unresolved"].append("sealed replica set differs from declared owners")
            obs["scope"] = "partial"
        else:
            obs["expected_steps_by_replica"] = seals
            obs["inventory_evidence"] = {"path": seal_source}
    if not obs["inventory"]:
        obs["scope"] = "partial"
        obs["unresolved"].append("no run-bound owner step inventory")
    if spec["scope"] == "full_run" and not seals:
        obs["scope"] = "partial"
        obs["unresolved"].append("missing independent full-window step totals")
    if obs["scope"] == "partial":
        steps = {(row["replica"], row["step_id"]) for row in obs["inventory"]}
        for kind in ("outputs", "work"):
            retained = [row for row in obs[kind] if (row["replica"], row["step_id"]) in steps]
            omitted = len(obs[kind]) - len(retained)
            if omitted:
                obs["unresolved"].append("{} {} rows have no matching observed owner step".format(omitted, kind))
            obs[kind] = retained
    for path, expected in docs.items():
        require(file_hash(Path(path)) == expected, "source changed during analysis: " + path)
    obs["sources"] = [{"path": path, "sha256": digest} for path, digest in sorted(docs.items())]
    return obs


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    new = sub.add_parser("prepare", help="freeze MS CSV and mapped site inputs for a benchmark run")
    new.add_argument("--run-dir", type=Path, required=True)
    new.add_argument("--ms-dir", type=Path)
    new.add_argument("--mapping", type=Path)
    new.add_argument("--tool-version", required=True)
    new.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        prepare(args.run_dir, args.ms_dir, args.mapping, args.tool_version, args.output)
    except (KeyError, OSError, ValueError, TypeError, csv.Error) as exc:
        p.exit(2, "step_sources: " + str(exc) + "\n")


if __name__ == "__main__":
    main()

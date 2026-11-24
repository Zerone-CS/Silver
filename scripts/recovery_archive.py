"""Export and independently replay only the restoration of a closed round's baseline."""

import json
import shutil
from pathlib import Path

from scripts import round_archive, round_evidence as ev

KIND = "baseline_recovery"
FIELDS = ("campaign_id", "round", "phase", "decision", "observation", "closed_at", "baseline", "candidate",
          "final_deployment", "last_probe", "last_health", "closure_binding")


def export(row):
    ev.require(row.get("decision") in ("abandon", "revert"), "recovery-only requires a reverted or abandoned round")
    ev.require(row.get("closure_binding") and row.get("acceptance_protocol", {}).get("endpoint"),
               "this round lacks the bound endpoint or health challenge required for portable recovery")
    record = {key: row[key] for key in FIELDS}
    protocol = row["acceptance_protocol"]
    record.update(schema_version=1, kind=KIND, acceptance_protocol={
        k: protocol[k] for k in ("endpoint", "model_snapshot_sha256") if k in protocol})
    for key in ("site_release", "stop_reason"):
        if key in row:
            record[key] = row[key]
    if "step_analysis_review_error" in row["evidence"]:
        record["step_analysis_review_error"] = row["evidence"]["step_analysis_review_error"]
    audit(record, ev.local_path)
    return record


def audit(record, locate):
    ev.require(record.get("kind") == KIND and record.get("schema_version") == 1, "expected a recovery-only export")
    required = set(FIELDS) | {"schema_version", "kind", "acceptance_protocol"}
    optional = {"site_release", "stop_reason", "step_analysis_review_error"}
    ev.require(required <= set(record) <= required | optional, "unexpected or missing recovery export fields")
    ev.require(record["decision"] in ("abandon", "revert"), "recovery-only requires a reverted or abandoned round")
    reason = ev.compare_trials.text(record["observation"], "closure observation")
    protocol = record["acceptance_protocol"]
    ev.require(set(protocol) in ({"endpoint"}, {"endpoint", "model_snapshot_sha256"})
               and set(protocol["endpoint"]) == {"base_url", "model"}, "incomplete recovery endpoint policy")
    for key, value in protocol["endpoint"].items():
        ev.compare_trials.text(value, "endpoint." + key)
    released = round_archive.verify_closure(record, locate)
    result = {"kind": KIND, "campaign_id": record["campaign_id"], "round": record["round"],
              "decision": record["decision"], "observation": reason, "comparison": "not_verified",
              "baseline_restored": True, "final_health": "verified", "device_release": released}
    for key in ("stop_reason", "step_analysis_review_error"):
        if key in record:
            result[key] = ev.compare_trials.text(record[key], key)
    return result


def pack(export_path, roots, output):
    from scripts import evidence_bundle as bundles

    record = ev.read_object(export_path)
    verifier = bundles.assessor_identity()
    roots = [root.resolve(strict=True) for root in roots]
    output = output.resolve()
    ev.require(roots and all(root.is_dir() and root != Path("/") and not output.is_relative_to(root)
                             for root in roots), "supply source directories and put the bundle outside them")
    ev.require(not output.exists(), "bundle destination already exists")
    files = round_archive.ArchivedFiles(output)
    sources = set()

    def source_file(name):
        path = round_archive.absolute(name)
        ev.require(any(path.is_relative_to(root) for root in roots) and path.resolve(strict=True) == path
                   and path.is_file(), "recovery evidence must be a regular file in the source roots")
        sources.add(path)
        return path

    audit(record, source_file)
    output.mkdir(parents=True)
    for source in sorted(sources):
        dest = files.destination(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    (output / "payload" / "recovery-export.json").write_text(
        json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    result = audit(record, files)
    manifest = {"schema_version": 2, "kind": KIND, "verifier": verifier,
                "files_sha256": bundles.payload_files(output), "result": result}
    (output / "bundle.json").write_text(json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                                         encoding="utf-8")
    return result

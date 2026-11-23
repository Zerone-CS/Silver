"""Validate the optional implementation identity and sealed provenance on a run."""

import hashlib
import json
import re

FIELDS = ("implementation_sha256", "implementation_identity", "implementation_verified",
          "implementation_evidence", "quality_implementation_sha256", "implementation_quality_source")


def identity_hash(document):
    if not isinstance(document, dict) or not document:
        raise ValueError("implementation identity must be a nonempty JSON object")
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def valid_source(source):
    return (isinstance(source, dict)
            and isinstance(source.get("path"), str) and bool(source["path"].strip())
            and isinstance(source.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is not None)


def implementation_fields(record):
    """Return checked, self-contained fields. Historical records have none."""
    fields = {key: record[key] for key in FIELDS if key in record}
    if not fields:
        return fields
    identity = fields.get("implementation_identity")
    if not isinstance(identity, dict) or not valid_source(identity.get("source")):
        raise ValueError("implementation identity requires a source path and SHA-256")
    expected = identity_hash(identity.get("document"))
    if fields.get("implementation_sha256") != expected:
        raise ValueError("implementation_sha256 does not match the implementation identity")
    verified = fields.get("implementation_verified")
    if type(verified) is not bool:
        raise ValueError("implementation_verified must be a JSON boolean")
    if verified or "implementation_evidence" in fields:
        evidence = fields.get("implementation_evidence")
        if not isinstance(evidence, dict) or evidence.get("implementation_sha256") != expected:
            raise ValueError("implementation evidence must be bound to this implementation_sha256")
        if (evidence.get("verified") is not verified or not valid_source(evidence.get("source"))
                or not isinstance(evidence.get("scope"), str) or not evidence["scope"].strip()):
            raise ValueError("implementation evidence needs a matching verification, scope and hashed source")
        artifacts = evidence.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts or not all(valid_source(item) for item in artifacts):
            raise ValueError("implementation evidence needs nonempty artifact paths and SHA-256 hashes")
    sources = record.get("source_inputs")
    declared_quality_source = sources.get("quality_report") if isinstance(sources, dict) else None
    has_quality = (declared_quality_source is not None or record.get("quality_status") in ("pass", "fail")
                   or "quality_implementation_sha256" in fields or "implementation_quality_source" in fields)
    if has_quality:
        quality_source = fields.get("implementation_quality_source")
        if fields.get("quality_implementation_sha256") != expected or not valid_source(quality_source):
            raise ValueError("quality report must be bound to this implementation_sha256 with a hashed source")
        if declared_quality_source is not None and quality_source != declared_quality_source:
            raise ValueError("implementation quality source does not match source_inputs.quality_report")
    return fields

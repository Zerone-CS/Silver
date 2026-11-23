#!/usr/bin/env python3
"""Compare two immutable prompt/output maps offline; exact match is only a smoke test."""

import argparse
import hashlib
import json
from pathlib import Path
import sys


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate prompt_id: %s" % key)
        result[key] = value
    return result


def read_map(path):
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    if not isinstance(value, dict) or not value:
        raise ValueError("%s must contain a nonempty {prompt_id: output_string} object" % path)
    if any(not isinstance(key, str) or not key.strip() or not isinstance(item, str)
           for key, item in value.items()):
        raise ValueError("%s must contain nonempty prompt IDs and string outputs only" % path)
    return value, hashlib.sha256(raw).hexdigest()


def compare(reference_path, candidate_path):
    if reference_path.resolve() == candidate_path.resolve() or reference_path.samefile(candidate_path):
        raise ValueError("reference and candidate must be different files, not aliases/hard links")
    reference, reference_hash = read_map(reference_path)
    candidate, candidate_hash = read_map(candidate_path)
    ref_keys, cand_keys = set(reference), set(candidate)
    missing = sorted(ref_keys - cand_keys)
    unexpected = sorted(cand_keys - ref_keys)
    mismatched = sorted(key for key in ref_keys & cand_keys if reference[key] != candidate[key])
    passed = not (missing or unexpected or mismatched)
    return {
        "schema_version": 1,
        "passed": passed,
        "status": "exact_match_smoke_pass" if passed else "sample_difference",
        "reference": str(reference_path.resolve()),
        "candidate": str(candidate_path.resolve()),
        "source_hashes": {"reference": reference_hash, "candidate": candidate_hash},
        "n_reference": len(reference), "n_candidate": len(candidate),
        "n_exact_match": sum(reference[key] == candidate[key] for key in ref_keys & cand_keys),
        "missing_prompt_ids": missing, "unexpected_prompt_ids": unexpected,
        "mismatched_prompt_ids": mismatched,
        "quality_status": "not_evaluated",
        "scope": "Exact string comparison of these recorded smoke samples only; not general quality equivalence. Different outputs require task-specific quality evaluation.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="new JSON report; existing paths are refused")
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output already exists; refusing to overwrite: %s" % args.output)
        if args.output.resolve() in (args.reference.resolve(), args.candidate.resolve()):
            raise ValueError("output cannot be an input path")
        report = compare(args.reference, args.candidate)
        text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        print("input/IO error: %s" % exc, file=sys.stderr)
        return 2
    print("%s; report=%s" % (report["status"], args.output))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

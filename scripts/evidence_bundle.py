#!/usr/bin/env python3
"""Seal and relocate original round evidence for independent offline replay."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

SUITE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SUITE))
from scripts import round_evidence as evidence
from scripts.verify_suite import verify


def canonical(path):
    return Path(os.path.abspath(path))


def within(path, directory):
    try:
        return path.relative_to(directory)
    except ValueError:
        return None


def plain_file(path, root):
    path = canonical(path)
    evidence.require(within(path, root) is not None and path.resolve(strict=True) == path,
                     "evidence contains a symlink or leaves the archive: " + str(path))
    evidence.require(path.is_file(), "expected a regular evidence file: " + str(path))
    return path


class Origins:
    """A strict mapping from original absolute paths to files in an archive."""

    def __init__(self, directory, entries):
        self.directory = directory.resolve(strict=True)
        self.entries = []
        for entry in entries:
            original = Path(entry["origin"])
            key = Path(entry["payload"])
            evidence.require(original.is_absolute() and original != Path("/") and original == canonical(original),
                             "origin must be a canonical absolute directory other than /")
            evidence.require(not key.is_absolute() and len(key.parts) == 2 and key.parts[0] == "payload",
                             "invalid archive payload root")
            target = canonical(self.directory / key)
            evidence.require(within(target, self.directory / "payload") is not None and
                             target.resolve(strict=True) == target and target.is_dir(),
                             "invalid archive payload root")
            self.entries.append((original, target))
        evidence.require(len({p for p, _ in self.entries}) == len(self.entries)
                         and len({p for _, p in self.entries}) == len(self.entries),
                         "duplicate archive roots")
        self.entries.sort(key=lambda entry: len(entry[0].parts), reverse=True)

    def __call__(self, path):
        path = canonical(path)
        if not any(within(path, target) is not None for _, target in self.entries):
            for origin, target in self.entries:
                suffix = within(path, origin)
                if suffix is not None:
                    path = target / suffix
                    break
            else:
                raise ValueError("unmapped evidence path: " + str(path))
        evidence.require(path.resolve(strict=True) == path, "archive contains an evidence symlink")
        return path


def payload_files(directory):
    payload = directory / "payload"
    evidence.require(payload.is_dir() and not payload.is_symlink(), "missing archive payload")
    found = {}
    for path in sorted(payload.rglob("*")):
        evidence.require(not path.is_symlink(), "evidence cannot contain symlinks: " + str(path))
        if not path.is_dir():
            plain_file(path, payload)
            found[path.relative_to(directory).as_posix()] = evidence.file_hash(path)
    evidence.require(found, "cannot seal an empty archive")
    return found


def stable_result(report):
    # Omit filesystem locations and executable identities that vary across
    # replay sites. The evidence verifier still validates all source digests.
    comparison = report["comparison"]
    return {
        "recommendation": report["recommendation"],
        "case_identity": report["case_identity"],
        "pair_results": [dict((key, value) for key, value in pair.items() if not key.endswith("_path"))
                         for pair in comparison["pairs"]],
        "scenarios": comparison["scenarios"],
        "metrics": {row["manifest"]["run_id"]: row["metrics"] for row in comparison["validated_inputs"]},
        "quality": {run_id: {key: value for key, value in result.items() if key not in ("scorer", "python")}
                    for run_id, result in report["quality"].items()},
        "goodput": report.get("goodput", {}),
    }


def assessor_identity():
    result = verify(SUITE)
    return {key: result[key] for key in ("release_id", "manifest_sha256")}


def run_checks(root, manifest, *, strict):
    locate = Origins(root, manifest["roots"])
    results = []
    names = set()
    for check in manifest["checks"]:
        fields = {"name", "contract", "pairs", "baseline_identity", "candidate_identity"}
        evidence.require(set(check) in (fields, fields | {"expected_report"}), "unexpected replay case fields")
        evidence.require(isinstance(check["name"], str) and check["name"].strip() and check["name"] not in names,
                         "duplicate or empty check name")
        names.add(check["name"])
        evidence.require(all(Path(check[key]).is_absolute() for key in set(check) - {"name"}),
                         "checks must use original absolute paths")
        baseline, candidate = (evidence.read_object(locate(check[side + "_identity"]))
                               for side in ("baseline", "candidate"))
        report = evidence.assess(check["contract"], check["pairs"], baseline, candidate, locate=locate)
        outcome = stable_result(report)
        if "expected_report" in check:
            expected = evidence.read_object(locate(check["expected_report"]))
            evidence.require(outcome == stable_result(expected), "historical result differs for " + check["name"])
        results.append({"name": check["name"], **outcome})
    evidence.require(results, "an evidence bundle must contain at least one replay")
    if strict:
        evidence.require(results == manifest["results"], "offline replay differs from sealed results")
    return results


def pack(spec_path, output):
    spec = evidence.read_object(spec_path)
    evidence.require(set(spec) == {"schema_version", "roots", "checks"} and spec["schema_version"] == 1,
                     "invalid evidence bundle spec")
    verifier = assessor_identity()
    output = output.resolve()
    evidence.require(not output.exists(), "evidence output already exists")
    manifest = {"schema_version": 1, "verifier": verifier, "roots": [], "checks": spec["checks"]}
    sources = []
    for index, entry in enumerate(spec["roots"]):
        evidence.require(set(entry) == {"origin", "source"}, "invalid source root")
        source = (spec_path.parent / entry["source"]).resolve(strict=True)
        evidence.require(source.is_dir() and within(output, source) is None, "invalid evidence copy source")
        key = f"payload/root-{index + 1}"
        manifest["roots"].append({"origin": entry["origin"], "payload": key})
        sources.append((source, output / key))
    output.mkdir()
    # Preserve raw bytes and retain a partial bundle for audit if sealing fails.
    for source, target in sources:
        for path in source.rglob("*"):
            evidence.require(not path.is_symlink() and (path.is_file() or path.is_dir()),
                             "evidence source includes non-regular objects: " + str(path))
        shutil.copytree(source, target)
    manifest["files_sha256"] = payload_files(output)
    manifest["results"] = run_checks(output, manifest, strict=False)
    with (output / "bundle.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return summary(manifest)


def summary(manifest):
    return {"verifier": manifest["verifier"], "files_verified": len(manifest["files_sha256"]),
            "checks": [{"name": case["name"], "recommendation": case["recommendation"],
                        "runs": len(case["metrics"])} for case in manifest["results"]]}


def replay(directory):
    directory = directory.resolve(strict=True)
    manifest = evidence.read_object(plain_file(directory / "bundle.json", directory))
    evidence.require(manifest["schema_version"] == 1 and manifest["verifier"] == assessor_identity(),
                     "bundle needs its exact evaluator release and manifest hash")
    evidence.require(payload_files(directory) == manifest["files_sha256"], "evidence archive bytes changed")
    run_checks(directory, manifest, strict=True)
    return summary(manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    command = actions.add_parser("pack")
    command.add_argument("--spec", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command = actions.add_parser("verify")
    command.add_argument("--bundle", type=Path, required=True)
    options = parser.parse_args(argv)
    try:
        result = pack(options.spec, options.output) if options.action == "pack" else replay(options.bundle)
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

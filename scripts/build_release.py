#!/usr/bin/env python3
"""Refresh the suite manifest and build a Git-pinned delivery archive."""

import argparse
import gzip
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from verify_suite import COMMAND_ENTRYPOINTS, PUBLIC_CONTRACTS, TEST_SUITES, package_files, verify


ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def write(path, doc):
    with path.open("x", encoding="utf-8") as file:
        json.dump(doc, file, indent=2, ensure_ascii=False)
        file.write("\n")


def refresh(release):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,70}", release):
        raise ValueError("invalid release ID")
    file = ROOT / "suite-manifest.json"
    manifest = json.loads(file.read_bytes())
    manifest.update(release_id=release, public_contracts=PUBLIC_CONTRACTS,
                    command_entrypoints=COMMAND_ENTRYPOINTS, test_suites=TEST_SUITES)
    manifest["files_sha256"] = {key: value for key, value in sorted(package_files(ROOT).items())
                                if key != file.name}
    replacement = file.with_suffix(".tmp")
    write(replacement, manifest)
    replacement.replace(file)
    return verify(ROOT)


def build(output):
    if git("status", "--porcelain"):
        raise ValueError("test and commit the release in a clean worktree before building")
    commit = git("rev-parse", "HEAD")
    receipt = verify(ROOT)
    directory = "vllm-ascend-throughput-skills"
    output = output.resolve()
    output.mkdir()
    artifact = output / (directory + ".tar.gz")
    with TemporaryDirectory(prefix="inferopt-release-") as temporary:
        tree = Path(temporary)
        plain = tree / "source.tar"
        subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "--prefix=" + directory + "/",
                        "-o", str(plain), commit], check=True)
        # Git tree files have already passed the manifest verifier, which
        # rejects symlinks. Retest the exact tree represented by the archive.
        subprocess.run(["tar", "-xf", str(plain), "-C", str(tree)], check=True)
        source = tree / directory
        if verify(source) != receipt:
            raise ValueError("archive differs from the verified worktree")
        with artifact.open("xb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as zipped:
            with plain.open("rb") as original:
                shutil.copyfileobj(original, zipped)
    from round_evidence import file_hash
    digest = file_hash(artifact)
    (output / "SHA256SUMS").write_text(digest + "  " + artifact.name + "\n", encoding="ascii")
    result = {**receipt, "commit": commit, "archive": artifact.name, "archive_sha256": digest}
    write(output / "release.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--refresh", metavar="RELEASE_ID")
    modes.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = refresh(args.refresh) if args.refresh else build(args.output)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

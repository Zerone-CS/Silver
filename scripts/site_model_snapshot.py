"""Create a private content-addressed model mount and verify its frozen inventory."""
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from tempfile import NamedTemporaryFile, TemporaryDirectory


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(root, *, source=False):
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() and (not source or path.is_dir()):
            raise ValueError("unsupported link in model directory: " + str(path))
        info = path.stat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("non-regular model file: " + str(path))
        files[path.relative_to(root).as_posix()] = [
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    if not files:
        raise ValueError("empty model directory: " + str(root))
    return files


def content_id(hashes):
    raw = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def matches_pins(hashes, pins):
    if any(hashes.get(name) != expected for name, expected in pins.items()):
        raise ValueError("model contents differ from the pinned site environment")


def verify(store, record, mounted=None):
    hashes = record["files_sha256"]
    digest = content_id(hashes)
    if record["schema_version"] != 1 or digest != record["content_sha256"]:
        raise ValueError("model snapshot receipt identity differs from its inventory")
    target = store / digest
    if target.is_symlink() or not target.is_dir():
        raise ValueError("missing or substituted model snapshot: " + str(target))
    root = mounted if mounted is not None else target
    if not root.samefile(target):
        raise ValueError("container model mount differs from the frozen snapshot")
    observed = inventory(root)
    if observed.keys() != hashes.keys():
        raise ValueError("model snapshot file list changed")
    # Copies are read-only, unlinked from the source, and the site operator is
    # trusted. If inode metadata changes, verify bytes rather than trusting it.
    for name, stamp in observed.items():
        if stamp != record["file_stats"][name] and sha256(root / name) != hashes[name]:
            raise ValueError("model snapshot changed: " + name)
    return target


def freeze(source, store, receipt_path, pins, expected=None):
    """The caller holds the site lock. Other sites may share the digest store."""
    store = store.resolve()
    if receipt_path.exists():
        record = json.loads(receipt_path.read_bytes())
        matches_pins(record["files_sha256"], pins)
        if expected is not None and record["content_sha256"] != expected:
            raise ValueError("model snapshot differs from site environment")
        verify(store, record)
        return record
    source = source.resolve(strict=True)
    if store == source or source in store.parents:
        raise ValueError("model snapshot store must be outside model source")
    store.mkdir(mode=0o700, exist_ok=True, parents=True)
    hashes = {name: sha256(source / name) for name in inventory(source, source=True)}
    matches_pins(hashes, pins)
    digest = content_id(hashes)
    if expected is not None and digest != expected:
        raise ValueError("model source differs from the frozen snapshot identity")
    target = store / digest
    if not target.exists():
        with TemporaryDirectory(dir=store) as scratch:
            staged = Path(scratch) / "model"
            staged.mkdir()
            for name, expected_hash in hashes.items():
                dst = staged / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, dst)
                if sha256(dst) != expected_hash:
                    raise ValueError("model changed while creating snapshot: " + name)
                dst.chmod(0o444)
            for directory in staged.rglob("*"):
                if directory.is_dir():
                    directory.chmod(0o555)
            try:
                staged.rename(target)
                target.chmod(0o555)
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST) or not target.is_dir():
                    raise
    if target.is_symlink():
        raise ValueError("model snapshot is a symbolic link")
    stats = inventory(target)
    if stats.keys() != hashes.keys() or any(sha256(target / name) != value for name, value in hashes.items()):
        raise ValueError("existing snapshot differs from expected content")
    record = {"schema_version": 1, "content_sha256": digest, "files_sha256": hashes, "file_stats": stats}
    with NamedTemporaryFile(mode="w", dir=receipt_path.parent) as temp:
        json.dump(record, temp, ensure_ascii=False, sort_keys=True)
        temp.flush()
        os.link(temp.name, receipt_path)
    return record

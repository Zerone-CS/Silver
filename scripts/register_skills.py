#!/usr/bin/env python3
"""Create minimal discovery entries pointing to the single self-contained suite."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from verify_suite import verify, NAMES, SKILL_PATHS

MARKER_KIND = 'throughput-suite-discovery'


def entry_text(root, name):
    canonical = root / SKILL_PATHS[name] / 'SKILL.md'
    text = canonical.read_text()
    front = text.split('---', 2)[1]
    description = re.search(r'^description:\s*(.+)$', front, re.M).group(1)
    return ('---\nname: ' + name + '\ndescription: ' + description + '\n---\n\n'
            '# 技能发现入口\n\n'
            '本目录仅用于技能发现。执行前读取唯一实现：\n\n'
            '[' + name + ' SKILL.md](' + canonical.as_posix() + ')\n\n'
            '按该文件的公开接口执行。套件根目录为 `' + root.as_posix() + '`；业务工具和产物契约由对应模块维护，主入口负责协调状态。'
            '本发现目录不保存第二份实现；无需先调用其他入口才能读取此文件。\n')


def owned_marker(folder, name):
    """Recognize our marker without following entry or marker symlinks."""
    marker = folder / 'registration.json'
    if folder.is_symlink() or not folder.is_dir() or marker.is_symlink() or not marker.is_file():
        return None
    try:
        previous = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    if isinstance(previous, dict) and previous.get('kind') == MARKER_KIND and previous.get('skill') == name:
        return previous
    return None


def register(root, destination):
    root = root.resolve(strict=True)
    verify(root)
    destination = destination.expanduser().resolve()
    if destination == root or root in destination.parents:
        raise ValueError('discovery directory must be outside the suite; the suite already contains the canonical entries')
    plans = []
    # Preflight all five entries before writing. Other directories are untouched.
    for name in NAMES:
        folder = destination / name
        exists = folder.exists() or folder.is_symlink()
        previous = owned_marker(folder, name) if exists else None
        text = entry_text(root, name)
        marker = {'schema_version': 1, 'kind': MARKER_KIND, 'suite_root': str(root),
                  'skill': name, 'entry_sha256': hashlib.sha256(text.encode()).hexdigest()}
        if exists:
            if previous is None:
                raise ValueError('refusing unrelated existing entry: ' + str(folder))
            if any((folder / filename).is_symlink() for filename in ('SKILL.md', 'registration.json')):
                raise ValueError('refusing symlink files in discovery entry: ' + str(folder))
            if sorted(p.name for p in folder.iterdir()) != ['SKILL.md', 'registration.json'] or not (folder / 'SKILL.md').is_file():
                raise ValueError('refusing changed or unrelated entry: ' + str(folder))
            if hashlib.sha256((folder / 'SKILL.md').read_bytes()).hexdigest() != previous.get('entry_sha256'):
                raise ValueError('discovery entry was edited: ' + str(folder))
        plans.append((folder, text, marker))
    destination_existed = destination.exists()
    destination.mkdir(parents=True, exist_ok=True)
    changed = []
    saved = []
    try:
        for folder, text, marker in plans:
            if folder.exists():
                saved.append((folder, (folder / 'SKILL.md').read_bytes(), (folder / 'registration.json').read_bytes()))
            else:
                folder.mkdir()
                saved.append((folder, None, None))
            (folder / 'SKILL.md').write_text(text)
            (folder / 'registration.json').write_text(json.dumps(marker, ensure_ascii=False, indent=2) + '\n')
            changed.append(str(folder))
    except BaseException:
        for folder, entry, metadata in reversed(saved):
            if entry is None:
                for name in ('SKILL.md', 'registration.json'):
                    path = folder / name
                    if path.exists():
                        path.unlink()
                folder.rmdir()
            else:
                (folder / 'SKILL.md').write_bytes(entry)
                (folder / 'registration.json').write_bytes(metadata)
        if not destination_existed:
            destination.rmdir()
        raise
    return {'status': 'registered', 'suite_root': str(root), 'discovery_entries': changed,
            'canonical_entries': len(NAMES), 'compatibility_entries': 0,
            'preserved': [],
            'mode': 'ordinary directories containing only pointers; no duplicated implementation',
            'automatic_runtime_reload_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skills-dir', required=True, type=Path, help='agent skill discovery directory')
    args = parser.parse_args()
    try:
        report = register(Path(__file__).resolve().parents[1], args.skills_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

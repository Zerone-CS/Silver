#!/usr/bin/env python3
"""Prepare the KB dependency environment from its published, hash-locked resources."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import venv
from verify_suite import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rebuild', action='store_true', help='back up an existing suite environment before rebuilding')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    runtime = root / '.runtime'
    if runtime.is_symlink():
        parser.error('runtime must be a directory inside this suite')
    maintenance = root / '.maintenance'
    if maintenance.is_symlink() or maintenance.resolve().parent != root:
        parser.error('maintenance must be a directory inside this suite, not a symlink')
    verify(root)
    resources = json.loads((root / 'suite-manifest.json').read_text())['resources']
    lock = root / resources['requirements']
    wheels = root / resources['offline_wheels']
    target = json.loads((root / resources['runtime_target']).read_text())
    if list(sys.version_info[:2]) != target['python_minor'] or platform.system() != target['system'] or platform.machine() != target['machine']:
        parser.error('bundled wheels target ' + json.dumps(target) + '; use the matching system Python, or explicitly rebuild the wheel cache for the new target')
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()
    interpreter = runtime / 'bin/python'
    receipt_path = runtime / 'suite-runtime.json'
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME') and not k.startswith('PIP_')}
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PIP_CONFIG_FILE=os.devnull)
    if runtime.exists() and not args.rebuild:
        if not receipt_path.is_file() or json.loads(receipt_path.read_text()).get('requirements_sha256') != lock_hash:
            parser.error('existing environment has a different or missing receipt; use --rebuild')
        subprocess.run([str(interpreter), '-m', 'pip', '--isolated', 'check'], env=env, check=True)
        inventory = json.loads(subprocess.check_output([str(interpreter), '-m', 'pip', '--isolated', 'list', '--format=json', '--disable-pip-version-check'], env=env, text=True))
        normalize = lambda name: re.sub(r'[-_.]+', '-', name).lower()
        installed = {normalize(item['name']): item['version'] for item in inventory}
        for line in lock.read_text().splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            name, version = line.split()[0].split('==', 1)
            if installed.get(normalize(name)) != version:
                parser.error('existing environment differs from the lock: ' + name + '; use --rebuild')
        print(json.dumps({'status': 'reused', 'python': str(interpreter), 'requirements_sha256': lock_hash}))
        return 0
    if runtime.exists() and Path(sys.prefix).resolve() == runtime.resolve():
        parser.error('rebuild must be launched with the system Python, not the environment being replaced')
    audit = root / '.maintenance' / ('runtime-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    audit.mkdir(parents=True, exist_ok=False)
    previous = None
    if runtime.exists():
        previous = audit / 'previous-runtime'
        runtime.rename(previous)
    created = False
    try:
        runtime.mkdir()
        created = True
        venv.EnvBuilder(with_pip=False, system_site_packages=False).create(str(runtime))
        command = [str(interpreter), '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check',
                   '--no-index', '--no-compile', '--find-links', str(wheels),
                   '--require-hashes', '-r', str(lock)]
        with (audit / 'install.log').open('x') as log:
            subprocess.run([str(interpreter), '-m', 'ensurepip', '--upgrade'], env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run([str(interpreter), '-m', 'pip', '--isolated', 'check'], env=env, check=True)
        inventory = subprocess.check_output([str(interpreter), '-m', 'pip', '--isolated', 'list', '--format=json', '--disable-pip-version-check'], env=env, text=True)
        receipt = {'status': 'ready', 'python': str(interpreter), 'system_python': sys.executable,
                   'requirements_sha256': lock_hash, 'target': target, 'packages': json.loads(inventory),
                   'offline_install': True, 'system_site_packages': False, 'audit': str(audit)}
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps({'status': 'ready', 'python': str(interpreter), 'offline_install': True, 'audit': str(audit)}))
        return 0
    except BaseException:
        if created:
            shutil.rmtree(runtime)
        if previous is not None:
            previous.rename(runtime)
        raise


if __name__ == '__main__':
    raise SystemExit(main())

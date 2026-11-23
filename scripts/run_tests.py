#!/usr/bin/env python3
"""Run the published module test suites without relying on nested discovery."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=('all', 'evaluate', 'knowledge', 'suite'), default='all')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    suites = json.loads((root / 'suite-manifest.json').read_text())['test_suites']
    scopes = ('evaluate', 'knowledge', 'suite') if args.scope == 'all' else (args.scope,)
    directories = []
    for scope in scopes:
        for relative in suites[scope]:
            directory = (root / relative).resolve()
            directory.relative_to(root)
            if not list(directory.glob('test_*.py')):
                parser.error('missing or empty test suite: ' + relative)
            directories.append((scope, directory))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    for key in ('PYTHONPATH', 'PYTHONHOME'):
        env.pop(key, None)
    failed = False
    for scope, directory in directories:
        print('Running ' + scope + ': ' + str(directory.relative_to(root)), flush=True)
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover',
                                 '-s', str(directory), '-p', 'test_*.py'], cwd=str(root), env=env)
        failed = failed or result.returncode != 0
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())

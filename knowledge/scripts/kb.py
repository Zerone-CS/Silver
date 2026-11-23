#!/usr/bin/env python3
"""Dispatch to the bundled knowledge tools without relying on cwd or another project."""
import argparse
from pathlib import Path
import runpy
import sys


MODULE_ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__, usage='%(prog)s {query,extract,parse} [arguments]')
    parser.add_argument('command', choices=['query', 'extract', 'parse'])
    if not sys.argv[1:] or sys.argv[1] in ('-h', '--help'):
        parser.print_help()
        return 0
    command = sys.argv[1]
    if command not in ('query', 'extract', 'parse'):
        parser.error('unknown command: ' + command)
    kb = MODULE_ROOT / 'kb'
    if not (kb / 'query.py').is_file():
        parser.error('bundled knowledge tools missing: ' + str(kb))
    sys.path.insert(0, str(kb))
    sys.dont_write_bytecode = True
    sys.argv = [str(kb / 'query.py') if command == 'query' else command + '_params'] + sys.argv[2:]
    if command == 'query':
        runpy.run_path(str(kb / 'query.py'), run_name='__main__')
    else:
        runpy.run_module(command + '_params', run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

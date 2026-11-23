#!/usr/bin/env python3
"""Verify packaged file identities and local skill links; never modifies files."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

MAIN = 'vllm-ascend-throughput-optimization'
NAMES = [MAIN, 'evaluate', 'diagnose', 'optimize', 'knowledge']
SKILL_PATHS = {name: '.' if name == MAIN else name for name in NAMES}
ROLE_SKILLS = {'measurement': 'evaluate', 'profiling': 'diagnose', 'diagnosis': 'diagnose',
               'knowledge': 'knowledge', 'design': 'optimize', 'validation': 'evaluate'}
MANIFEST = 'suite-manifest.json'
PUBLIC_CONTRACTS = {
    'handoff': {'owner': MAIN, 'path': 'references/suite_contract.md'},
    'delivery': {'owner': MAIN, 'path': 'references/delivery.md'},
    'round_control': {'owner': MAIN, 'path': 'references/round_control.md'},
    'round_evidence': {'owner': MAIN, 'path': 'references/round_evidence.md'},
    'profile_capture': {'owner': 'diagnose', 'path': 'diagnose/references/profile.md'},
    'stream_tail': {'owner': 'diagnose', 'path': 'diagnose/references/stream_tail.md'},
    'step_analysis': {'owner': MAIN, 'path': 'diagnose/references/step_analysis.md'},
    'step_sources': {'owner': 'diagnose', 'path': 'diagnose/references/step_sources.md'},
    'step_timeline': {'owner': 'diagnose', 'path': 'diagnose/references/step_timeline.md'},
    'single_engine_probe': {'owner': 'diagnose', 'path': 'diagnose/adapters/vllm_023_single_engine/README.md'},
    'experience': {'owner': MAIN, 'path': 'knowledge/references/experience.md'},
    'experience_study': {'owner': 'knowledge', 'path': 'knowledge/references/experience_study.md'},
    'campaign_state': {'owner': MAIN, 'path': 'references/campaign_state.md'},
    'measurement': {'owner': 'evaluate', 'path': 'evaluate/references/measurement_contract.md'},
    'evaluation': {'owner': 'evaluate', 'path': 'evaluate/references/tool_contract.md'},
    'diagnosis': {'owner': 'diagnose', 'path': 'diagnose/references/records.md'},
    'experiment': {'owner': 'optimize', 'path': 'optimize/references/experiment_contract.md'},
    'knowledge': {'owner': 'knowledge', 'path': 'knowledge/references/records.md'},
}
COMMAND_ENTRYPOINTS = {
    'suite': {name: 'scripts/' + name + '.py' for name in
              ('verify_suite', 'register_skills', 'setup_runtime', 'run_tests', 'build_release',
               'campaign', 'drive_campaign', 'round_evidence', 'trial_plan', 'device_budget',
               'site_910c', 'site_round', 'evidence_bundle', 'experience', 'experience_study', 'step_analysis')},
    'diagnose': {**{name: 'diagnose/scripts/' + name + '.py' for name in
                    ('profile', 'vllm_profile', 'stream_tail', 'step_sources', 'step_timeline')},
                 'single_engine_probe': 'diagnose/adapters/vllm_023_single_engine/site_export.py'},
    'evaluate': {name: 'evaluate/scripts/' + name + '.py' for name in
                 ('benchmark', 'prepare_metadata', 'validate_run', 'compare_trials', 'compare_outputs')},
    'knowledge': {name: 'knowledge/scripts/' + name + '.py' for name in ('kb', 'check_kb_snapshot')},
}
TEST_SUITES = {'suite': ['tests'], 'evaluate': ['evaluate/tests'],
               'knowledge': ['knowledge/tests', 'knowledge/kb/tests']}


# Workspaces can also contain local reports and IDE settings next to the bundle.
WORKSPACE_DIRECTORIES = {'.git', '.runtime', '.maintenance', '.vscode', 'reports'}
REPORT_EXTENSIONS = {'.docx', '.xlsx', '.html', '.pdf', '.png'}


def package_files(root):
    result = {}
    for parent, directories, files in os.walk(root, followlinks=False):
        parent_path = Path(parent)
        directories[:] = sorted(name for name in directories if name != '__pycache__'
                                and (parent_path != root or name not in WORKSPACE_DIRECTORIES))
        files = [name for name in files if name != '.DS_Store'
                 and (parent_path != root or (name != '.git' and Path(name).suffix.lower() not in REPORT_EXTENSIONS))]
        for name in directories + files:
            if (parent_path / name).is_symlink():
                raise ValueError('packaged symlink is not allowed: ' + str(parent_path / name))
        for name in sorted(files):
            if name.endswith('.pyc'):
                continue
            path = parent_path / name
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def verify(root):
    root = root.resolve(strict=True)
    manifest = json.loads((root / MANIFEST).read_text())
    if manifest['skills'] != NAMES:
        raise ValueError('skill identities do not match the suite')
    if manifest.get('skill_paths') != SKILL_PATHS:
        raise ValueError('skill paths do not match the suite layout')
    if manifest.get('compatibility_aliases') != {} or manifest.get('compatibility_paths') != {}:
        raise ValueError('compatibility entries are retired; use the five canonical skills')
    if manifest.get('role_skills') != ROLE_SKILLS:
        raise ValueError('role skills do not match the stable handoff roles')
    if manifest.get('schema_version') != 3:
        raise ValueError('modular suite requires manifest schema_version 3')
    if manifest.get('composition') != {MAIN: NAMES[1:]}:
        raise ValueError('composition must describe the coordinator and four capabilities')
    if 'dependencies' in manifest or manifest.get('implementation_dependencies') != {name: ['evaluate'] if name == 'diagnose' else [] for name in NAMES[1:]}:
        raise ValueError('capability dependencies do not match the packaged tools')
    if manifest.get('state_owner') != MAIN:
        raise ValueError('campaign state and baseline belong to the coordinator')
    for field, expected in (('public_contracts', PUBLIC_CONTRACTS),
                            ('command_entrypoints', COMMAND_ENTRYPOINTS), ('test_suites', TEST_SUITES)):
        if manifest.get(field) != expected:
            raise ValueError(field + ' do not match module ownership')
    if manifest.get('external_project_dependencies') != []:
        raise ValueError('suite does not declare an empty external project dependency list')
    actual = package_files(root)
    expected = manifest['files_sha256']
    content = {rel: digest for rel, digest in actual.items() if rel != MANIFEST}
    if content != expected:
        missing = sorted(set(expected) - set(content))
        extra = sorted(set(content) - set(expected))
        changed = sorted(rel for rel in set(content) & set(expected) if content[rel] != expected[rel])
        raise ValueError('file manifest mismatch: ' + json.dumps({'missing': missing, 'extra': extra, 'changed': changed}))
    links = 0
    for name, relative in SKILL_PATHS.items():
        entry = root / relative / 'SKILL.md'
        text = entry.read_text()
        parts = text.split('---', 2)
        if len(parts) != 3 or parts[0].strip() or not re.search(r'^name:\s*' + re.escape(name) + r'\s*$', parts[1], re.M):
            raise ValueError('skill frontmatter name mismatch: ' + name)
        if not re.search(r'^description:\s*\S', parts[1], re.M):
            raise ValueError('missing skill description: ' + name)
    for rel in actual:
        path = root / rel
        if path.suffix == '.md':
            for destination in re.findall(r'\]\(([^)]+)\)', path.read_text()):
                parsed = urlsplit(destination)
                if parsed.scheme or not parsed.path:
                    continue
                target = (path.parent / unquote(parsed.path)).resolve()
                target.relative_to(root)
                if not target.is_file():
                    raise ValueError('missing local link: ' + rel + ' -> ' + destination)
                links += 1
        elif path.suffix == '.json':
            json.loads(path.read_text())
        elif path.suffix == '.py':
            ast.parse(path.read_text(), filename=rel, feature_version=(3, 9))
    for role, relative in manifest['resources'].items():
        target = (root / relative).resolve()
        target.relative_to(root)
        if not target.exists():
            raise ValueError('missing bundled resource: ' + role)
        if not relative.startswith('knowledge/'):
            raise ValueError('bundled knowledge resources must belong to knowledge: ' + role)
    for contract in PUBLIC_CONTRACTS.values():
        if not (root / contract['path']).is_file():
            raise ValueError('missing public contract: ' + contract['path'])
    for commands in COMMAND_ENTRYPOINTS.values():
        for relative in commands.values():
            if not (root / relative).is_file():
                raise ValueError('missing command entrypoint: ' + relative)
    for directories in TEST_SUITES.values():
        for relative in directories:
            if not list((root / relative).glob('test_*.py')):
                raise ValueError('missing or empty test suite: ' + relative)
    return {'status': 'pass', 'release_id': manifest['release_id'], 'skills': len(NAMES),
            'skill_paths': SKILL_PATHS, 'compatibility_entries': 0,
            'compatibility_aliases': {}, 'compatibility_paths': {},
            'packaged_files': len(actual), 'local_links': links,
            'manifest_sha256': actual[MANIFEST], 'external_project_dependencies': [],
            'state_owner': MAIN, 'public_contracts': PUBLIC_CONTRACTS,
            'command_entrypoints': COMMAND_ENTRYPOINTS, 'test_suites': TEST_SUITES,
            'scope': 'file identities, entry names, local links, Python syntax and declared resource paths; not KB semantic accuracy'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        report = verify(Path(__file__).resolve().parents[1])
    except (OSError, ValueError, SyntaxError) as exc:
        print(json.dumps({'status': 'fail', 'error': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

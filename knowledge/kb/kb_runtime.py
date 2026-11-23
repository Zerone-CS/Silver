"""Explicit source/output/API boundaries shared by the bundled KB tools."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

KB_ROOT = Path(__file__).resolve().parent
SNAPSHOT_ROOT = KB_ROOT / 'parse_params' / 'output'


def llm_environment(required=False):
    """Read only the current process environment; never inspect user settings."""
    result = {
        'api_key': os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN'),
        'base_url': os.environ.get('ANTHROPIC_BASE_URL'),
        'model': os.environ.get('ANTHROPIC_MODEL'),
    }
    if required:
        missing = [key for key in ('api_key', 'model') if not result[key]]
        if missing:
            raise ValueError('LLM requires ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) and ANTHROPIC_MODEL in the process environment; missing: ' + ', '.join(missing))
    return {key: value for key, value in result.items() if value}


def _within(path, parent):
    return path == parent or parent in path.parents


def check_output(path, sources=(), resume=False):
    path = Path(path).expanduser().resolve()
    if _within(path, SNAPSHOT_ROOT) or path == KB_ROOT / 'parameters.json':
        raise ValueError('Bundled historical knowledge snapshot is immutable; select a new output path')
    for source in sources:
        if _within(path, Path(source).expanduser().resolve()):
            raise ValueError('Output must be separate from source inputs')
    if resume:
        if not path.is_dir():
            raise ValueError('--resume requires an existing output directory from this tool')
        # Resume writes schema/logs/progress/params; never follow a pre-existing
        # child symlink into a source tree, historical snapshot, or other path.
        for directory, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                child = Path(directory) / name
                if child.is_symlink():
                    raise ValueError('--resume output tree contains a symlink: ' + str(child))
    elif path.exists():
        raise ValueError('Output already exists; choose a new output path (parse can explicitly --resume)')
    return path


def _git_identity(root):
    def git(*args):
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError('not a readable git checkout')
        return result.stdout.strip()
    try:
        if Path(git('rev-parse', '--show-toplevel')).resolve() != root:
            raise ValueError('source is not the repository root')
        return {'commit': git('rev-parse', 'HEAD'), 'dirty': bool(git('status', '--porcelain', '--untracked-files=all'))}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'commit': 'unknown', 'dirty': None}


def validate_sources(vllm, ascend):
    """Validate readable source inputs without importing or executing their code.

    Hash Python/Markdown inputs and reject unsupported Python syntax instead of
    silently reporting complete extraction. This is syntax coverage, not proof
    that every parameter construct is handled by the extractor.
    """
    roots = {'vllm': Path(vllm).expanduser().resolve(), 'vllm-ascend': Path(ascend).expanduser().resolve()}
    required = {'vllm': ['vllm/config', 'vllm/engine/arg_utils.py', 'vllm/envs.py'],
                'vllm-ascend': ['vllm_ascend/ascend_config.py', 'vllm_ascend/envs.py', 'docs/source']}
    result = {}
    for name, root in roots.items():
        for relative in required[name]:
            expected = root / relative
            if not expected.exists():
                raise ValueError('Missing required source input: ' + str(expected))
            if not _within(expected.resolve(), root):
                raise ValueError('Source symlink escapes supplied source root: ' + str(expected))
            if (relative.endswith('.py') and not expected.is_file()) or (not relative.endswith('.py') and not expected.is_dir()):
                raise ValueError('Wrong source input type: ' + str(expected))
        digest = hashlib.sha256()
        count = 0
        for item in sorted(root.rglob('*')):
            if item.is_symlink() and not _within(item.resolve(), root):
                raise ValueError('Source symlink escapes supplied source root: ' + str(item))
            if not item.is_file() or item.suffix not in {'.py', '.md'} or any(part in {'.git', '__pycache__', '.venv', 'venv'} for part in item.relative_to(root).parts):
                continue
            if not _within(item.resolve(), root):
                raise ValueError('Source symlink escapes supplied source root: ' + str(item))
            data = item.read_bytes()
            if item.suffix == '.py':
                try:
                    ast.parse(data, filename=str(item))
                except (SyntaxError, ValueError) as exc:
                    raise ValueError('Source syntax cannot be parsed by Python ' + sys.version.split()[0] + ': ' + str(item) + ': ' + str(exc)) from exc
            digest.update(str(item.relative_to(root)).encode() + b'\0' + data + b'\0')
            count += 1
        result[name] = {'path': str(root), **_git_identity(root), 'python_markdown_sha256': digest.hexdigest(), 'python_markdown_files': count}
    return roots, result


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def builder_identity(mode, max_params, concurrency):
    """Pin the builder, rules/prompts/schema, and non-secret LLM settings.

    API keys are intentionally excluded, so credential rotation does not mix
    semantic configurations. A dry-run output cannot become an LLM build.
    """
    files = [KB_ROOT / 'kb_runtime.py']
    files.extend(sorted((KB_ROOT / 'parse_params').glob('*.py')))
    files.extend(sorted((KB_ROOT / 'parse_params' / 'resources').rglob('*')))
    # Retained explanatory prompt material belongs to this builder version too.
    files.append(KB_ROOT / 'parse_params' / 'prompt.txt')
    hashes = {str(path.relative_to(KB_ROOT)): file_sha256(path) for path in files if path.is_file()}
    environment = llm_environment()
    endpoint = environment.get('base_url')
    if endpoint:
        from urllib.parse import urlsplit
        parsed = urlsplit(endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('ANTHROPIC_BASE_URL must not contain credentials, query, or fragment; supply the API key only through its dedicated environment variable')
    elif mode == 'llm':
        endpoint = 'https://api.anthropic.com'
    return {
        'files_sha256': hashes,
        'python_version': sys.version.split()[0],
        'mode': mode,
        'max_params': max_params,
        'concurrency': concurrency,
        'llm': {'model': environment.get('model'), 'endpoint': endpoint},
    }


def write_json_new(path, data):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write('\n')

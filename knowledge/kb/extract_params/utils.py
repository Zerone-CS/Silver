"""Pure utility functions: string cleaning, default-value parsing."""

from __future__ import annotations

import ast
import re
from typing import Any


def clean_description(desc: str | None) -> str | None:
    if desc is None:
        return None
    desc = desc.strip()
    desc = re.sub(r'^#\s*', '', desc)
    desc = re.sub(r'\s+', ' ', desc)
    desc = re.sub(r'["\']{3}', '', desc)
    return desc or None


def truncate_description(desc: str | None, max_len: int = 300) -> str | None:
    if not desc:
        return None
    desc = re.sub(r'\s+', ' ', desc).strip()
    if len(desc) <= max_len:
        return desc
    cut = desc[:max_len].rsplit('.', 1)[0]
    return (cut + '.') if cut else desc[:max_len]


def parse_default_value(raw: str | None) -> Any:
    """Parse a default-value expression from source code to a Python value."""
    if raw is None:
        return None
    raw = raw.strip().rstrip(',').strip()

    # Unwrap Field(default=...)
    m = re.match(r'Field\(\s*default\s*=\s*(.+?)(?:\s*,.*)?\s*\)', raw, re.DOTALL)
    if m:
        raw = m.group(1).strip()

    if raw == 'None':
        return None
    if raw == 'True':
        return True
    if raw == 'False':
        return False
    try:
        return int(raw)
    except (ValueError, TypeError):
        pass
    try:
        return float(raw)
    except (ValueError, TypeError):
        pass
    if (raw.startswith('"') and raw.endswith('"')) or \
       (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    if (raw.startswith('[') and raw.endswith(']')) or \
       (raw.startswith('{') and raw.endswith('}')) or \
       (raw.startswith('(') and raw.endswith(')')):
        try:
            return ast.literal_eval(raw)
        except Exception:
            return raw
    if any(kw in raw for kw in ('lambda', 'import', 'os.', 'MISSING',
                                  'field(', 'get_default_cache',
                                  'get_default_config', 'uuid', 'tempfile',
                                  'expanduser', 'current_platform')):
        return '<computed>'
    return raw


def extract_comment_before(lines: list[str], idx: int) -> str | None:
    comments = []
    i = idx - 1
    while i >= 0:
        line = lines[i].strip()
        if line.startswith('#'):
            comments.insert(0, line.lstrip('# ').strip())
        elif not line:
            pass
        else:
            break
        i -= 1
    return '; '.join(comments) if comments else None


def extract_default_from_env_expr(expr: str) -> str | None:
    """Pull the default from an os.getenv / os.environ.get(...) expression."""
    # os.getenv("X", "default")
    m = re.search(r'os\.getenv\(\s*["\'][^"\']+["\']\s*,\s*["\']([^"\']*?)["\']\s*\)', expr)
    if m:
        return m.group(1)
    # os.environ.get("X", "default")
    m = re.search(r'os\.environ\.get\(\s*["\'][^"\']+["\']\s*,\s*["\']([^"\']*?)["\']\s*\)', expr)
    if m:
        return m.group(1)
    # os.getenv("X") with no default
    if re.search(r'os\.getenv\([^,)]+\)', expr):
        return None
    if re.search(r'os\.environ\.get\([^,)]+\)', expr):
        return None
    return None

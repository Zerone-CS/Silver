"""Scan for environment variables from envs.py and scattered os.environ/os.getenv.

Dynamically discovers env vars – no hardcoded variable-name lists.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import get_vllm_root, get_vllm_ascend_src
from .utils import extract_comment_before, extract_default_from_env_expr, clean_description
from .category import categorize_by_env_name


def parse_envs_py(filepath: Path) -> list[dict]:
    """Parse a single envs.py for env var definitions (TYPE_CHECKING + dict)."""
    if not filepath.exists():
        return []
    try:
        source = filepath.read_text(encoding="utf-8")
    except Exception:
        return []
    lines = source.split("\n")
    results: list[dict] = []
    seen: set[str] = set()

    # 1. TYPE_CHECKING block: VAR: type = default
    in_type_checking = False
    buf_lines: list[str] = []
    buf_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if "TYPE_CHECKING" in stripped and "if" in stripped:
            in_type_checking = True
            continue
        if not in_type_checking:
            continue
        if stripped == "else:":
            break

        # Detect start of a variable definition: VAR_NAME: type...
        m_start = re.match(r"\s+([A-Z][A-Z_0-9]{2,}):\s+(.+)", line)
        if m_start:
            buf_lines = [line]
            buf_start = i
            # If the line already has `=` (single-line), process immediately
            if "=" in stripped.split("#")[0]:  # ignore = in comments
                _extract_typed_env(lines, buf_start, buf_lines, seen, results)
                buf_lines = []
            # else: multi-line — wait for subsequent lines
            continue

        # Accumulate continuation lines for a multi-line definition
        if buf_lines:
            buf_lines.append(line)
            if "=" in stripped.split("#")[0]:
                _extract_typed_env(lines, buf_start, buf_lines, seen, results)
                buf_lines = []

    # 2. environment_variables / env_variables dict
    for dict_name in ("environment_variables", "env_variables"):
        in_dict = False
        for i, line in enumerate(lines):
            if re.search(rf"{dict_name}.*=\s*\{{", line):
                in_dict = True
                continue
            if not in_dict:
                continue
            stripped = line.strip()
            if stripped in ("}", "}):", "},"):
                break
            m = re.match(r'\s*"([A-Z][A-Z_0-9]+)":\s*(.+)', line)
            if m:
                name = m.group(1)
                if name in seen:
                    continue
                seen.add(name)
                expr = m.group(2).rstrip(",").strip()
                default = extract_default_from_env_expr(expr)
                comment = extract_comment_before(lines, i)
                results.append({
                    "name": name, "default": default,
                    "description": clean_description(comment),
                })

    return results


def _extract_typed_env(lines: list[str], start_idx: int, buf_lines: list[str],
                       seen: set[str], results: list[dict]) -> None:
    """Extract a (possibly multi-line) typed env var definition from buffered lines."""
    joined = " ".join(l.strip() for l in buf_lines)
    # Strip inline comment
    joined = re.sub(r"#.*$", "", joined)
    m = re.match(r"([A-Z][A-Z_0-9]{2,}):\s*(.+?)\s*=\s*(.+)$", joined)
    if not m:
        return
    name = m.group(1)
    if name in seen:
        return
    seen.add(name)
    raw_default = m.group(3).strip().rstrip(",")
    default = _parse_simple_default(raw_default)
    comment = extract_comment_before(lines, start_idx)
    results.append({
        "name": name, "default": default,
        "description": clean_description(comment),
    })


def scan_scattered_env_vars(root_dir: Path, skip_names: set[str] | None = None) -> list[dict]:
    """Scan all .py files under root_dir for os.environ['X'] and os.getenv('X')."""
    if skip_names is None:
        skip_names = set()
    results: list[dict] = []
    seen: set[str] = set(skip_names)

    for py_file in root_dir.rglob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # os.environ["VAR"] = "value"
        for m in re.finditer(r'os\.environ\[\s*["\']([A-Z][A-Z_0-9]+)["\']\s*\]\s*=\s*["\']([^"\']*)["\']', source):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                results.append({
                    "name": name, "default": m.group(2),
                    "description": f"Set in {py_file.name}",
                    "source_file": str(py_file.relative_to(root_dir.parent))
                    if hasattr(py_file, "relative_to") else py_file.name,
                })

        # os.getenv("VAR", "default")
        for m in re.finditer(r'os\.getenv\(\s*["\']([A-Z][A-Z_0-9]+)["\']\s*,\s*["\']([^"\']*?)["\']\)', source):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                results.append({
                    "name": name, "default": m.group(2),
                    "description": f"Referenced in {py_file.name}",
                })

        # os.getenv("VAR") – no default
        for m in re.finditer(r'os\.getenv\(\s*["\']([A-Z][A-Z_0-9]+)["\']\s*\)', source):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                results.append({
                    "name": name, "default": None,
                    "description": f"Referenced in {py_file.name}",
                })

    return results


def extract_all_env_vars() -> list[dict]:
    """Extract all env vars from both vLLM and vllm-ascend."""
    all_vars: list[dict] = []

    # vLLM upstream
    vllm_envs = get_vllm_root() / "vllm" / "envs.py"
    for var in parse_envs_py(vllm_envs):
        var.update({"type": "env", "scope": "vllm",
                     "category": categorize_by_env_name(var["name"]),
                     "source_file": "vllm/envs.py"})
        all_vars.append(var)

    # vllm-ascend envs.py
    ascend_envs = get_vllm_ascend_src() / "envs.py"
    ascend_seen = {v["name"] for v in all_vars}
    for var in parse_envs_py(ascend_envs):
        var.update({"type": "env", "scope": "vllm-ascend",
                     "category": categorize_by_env_name(var["name"]),
                     "source_file": "vllm_ascend/envs.py"})
        all_vars.append(var)
        ascend_seen.add(var["name"])

    # Scattered os.environ / os.getenv in vllm-ascend
    for var in scan_scattered_env_vars(get_vllm_ascend_src(), skip_names=ascend_seen):
        var.update({"type": "env", "scope": "vllm-ascend",
                     "category": categorize_by_env_name(var["name"]),
                     "source_file": var.get("source_file", "vllm_ascend/")})
        all_vars.append(var)

    return all_vars


def _parse_simple_default(raw: str) -> object:
    raw = raw.strip()
    if raw in ("None", '""', "''"):
        return None
    if raw == "True":
        return True
    if raw == "False":
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
    if "(" in raw or "lambda" in raw or "os." in raw:
        return "<computed>"
    return raw

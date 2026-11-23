"""Extract CLI arguments from vllm/engine/arg_utils.py.

Uses regex for the top-level add_argument() patterns (robust enough for this)
but dynamically discovers the get_kwargs() → config-class mapping instead of
hardcoding it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .config import get_vllm_root
from .utils import parse_default_value
from .category import categorize_by_field_name


def parse_cli_args(arg_utils_path: str | None = None) -> list[dict]:
    """Parse arg_utils.py add_argument() calls. Returns raw metadata dicts."""
    if arg_utils_path is None:
        arg_utils_path = str(get_vllm_root() / "vllm" / "engine" / "arg_utils.py")

    source = Path(arg_utils_path).read_text(encoding="utf-8")
    lines = source.split("\n")

    # 1. Find add_cli_args method boundaries
    cli_start = cli_end = None
    for i, line in enumerate(lines):
        if "def add_cli_args" in line:
            cli_start = i
        if cli_start is not None and i > cli_start:
            stripped = line.strip()
            if stripped.startswith("def ") and "add_cli_args" not in stripped:
                cli_end = i
                break
    if cli_end is None:
        cli_end = len(lines)

    method_body = "\n".join(lines[cli_start:cli_end])

    # 2. Discover get_kwargs(ClassName) → var_name mapping dynamically
    kwarg_var_map = {}
    for m in re.finditer(r"(\w+)\s*=\s*get_kwargs\((\w+)\)", method_body):
        kwarg_var_map[m.group(1)] = m.group(2)

    # 3. Find all add_argument calls
    cli_args = []
    add_arg_pattern = re.compile(
        r'(\w+)\.add_argument\(\s*"(--[\w-]+)"'
        r"(?:\s*,\s*\"(-[\w])\")?"
        r"(.*?)"
        r"\)",
        re.DOTALL,
    )

    for m in add_arg_pattern.finditer(method_body):
        cli_flag = m.group(2)
        short_flag = m.group(3)
        rest = m.group(4) or ""

        # Pattern A: **some_kwargs["field"]
        kw_match = re.search(r',\s*\*\*(\w+)\["(\w+)"\]', rest)
        if kw_match:
            kwargs_var = kw_match.group(1)
            field_name = kw_match.group(2)
            config_class = kwarg_var_map.get(kwargs_var, "Unknown")
            cli_args.append({
                "cli_flag": cli_flag,
                "short_flag": short_flag,
                "field_name": field_name,
                "config_class": config_class,
                "hardcoded": False,
                "hardcoded_kwargs": None,
            })
            continue

        # Pattern B: fully hardcoded kwargs inside add_argument(...)
        kwargs = {}
        tm = re.search(r"type\s*=\s*([^,)]+)", rest)
        if tm:
            kwargs["type"] = tm.group(1).strip()
        hm = re.search(r'help\s*=\s*"((?:[^"\\]|\\.)*)"', rest)
        if hm:
            kwargs["help"] = hm.group(1)
        dm = re.search(r"default\s*=\s*([^,)]+)", rest)
        if dm:
            kwargs["default"] = parse_default_value(dm.group(1).strip())
        am = re.search(r'action\s*=\s*"([^"]*)"', rest)
        if am:
            kwargs["action"] = am.group(1)
        nm = re.search(r'nargs\s*=\s*"([^"]*)"', rest)
        if nm:
            kwargs["nargs"] = nm.group(1)
        cm = re.search(r"choices\s*=\s*(\[.*?\])", rest)
        if cm:
            try:
                kwargs["choices"] = ast.literal_eval(cm.group(1))
            except Exception:
                pass

        cli_args.append({
            "cli_flag": cli_flag,
            "short_flag": short_flag,
            "field_name": cli_flag.lstrip("-").replace("-", "_"),
            "config_class": "Hardcoded",
            "hardcoded": True,
            "hardcoded_kwargs": kwargs,
        })

    return cli_args


def enrich_cli_args(raw_args: list[dict], config_class_cache: dict[str, dict]) -> list[dict]:
    """Merge raw CLI args with dataclass field info (defaults, descriptions)."""
    from .utils import truncate_description

    results = []
    for arg in raw_args:
        if arg["hardcoded"]:
            kw = arg.get("hardcoded_kwargs") or {}
            results.append({
                "name": arg["cli_flag"],
                "type": "cli",
                "category": categorize_by_field_name(arg["field_name"]),
                "default": kw.get("default"),
                "description": kw.get("help"),
                "source_file": "vllm/engine/arg_utils.py",
                "scope": "vllm",
            })
        else:
            config_class = arg["config_class"]
            field_name = arg["field_name"]
            default = None
            description = None
            category = categorize_by_field_name(field_name)
            if config_class in config_class_cache:
                fi = config_class_cache[config_class].get(field_name, {})
                default = fi.get("default")
                description = fi.get("description")
                # Prefer class-based category if available, fall back to field-name inference
                cat_from_class = fi.get("category")
                if cat_from_class:
                    category = cat_from_class

            results.append({
                "name": arg["cli_flag"],
                "type": "cli",
                "category": category,
                "default": default,
                "description": truncate_description(description),
                "source_file": "vllm/engine/arg_utils.py",
                "scope": "vllm",
            })
    return results



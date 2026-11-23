"""AST-based parser for @config-decorated Pydantic dataclass fields.

Dynamically discovers config classes – no hardcoded class-name lists.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .config import get_vllm_config_dir
from .utils import parse_default_value
from .category import categorize_by_class_name


def parse_all_config_classes(config_dir: Path | None = None) -> dict[str, dict]:
    """Parse all config/*.py files, discover @config-decorated classes.

    Returns: {ClassName: {field_name: {type, default, description, category}}}
    """
    if config_dir is None:
        config_dir = get_vllm_config_dir()

    results: dict[str, dict] = {}
    for config_file in sorted(config_dir.glob("*.py")):
        if config_file.name.startswith("__"):
            continue
        try:
            results.update(parse_config_file(config_file))
        except Exception:
            continue
    return results


def parse_config_file(filepath: Path) -> dict[str, dict]:
    """Parse a single config .py file for @config-decorated classes."""
    try:
        source = filepath.read_text(encoding="utf-8")
    except Exception:
        return {}

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    results: dict[str, dict] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not _has_config_decorator(node):
            continue
        fields = _extract_class_fields(node, source.split("\n"))
        if fields:
            results[node.name] = fields
    return results


def _has_config_decorator(node: ast.ClassDef) -> bool:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "config":
            return True
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "config":
            return True
    return False


def _extract_class_fields(node: ast.ClassDef, lines: list[str]) -> dict:
    """Extract fields (name, type, default, description) from a class body.

    Uses AST nodes directly — no regex-based line scanning.
    Fields at the same indentation level are all captured; methods and
    nested classes are skipped.
    """
    fields = {}
    prev_field: ast.AnnAssign | None = None

    for i, stmt in enumerate(node.body):
        # String expressions are docstrings — either class-level or field-level.
        # If the previous statement was a field, this docstring is its description.
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
           and isinstance(stmt.value.value, str):
            if prev_field is not None and prev_field.target.id in fields:  # type: ignore[attr-defined]
                doc = stmt.value.value.strip()
                if doc:
                    fields[prev_field.target.id]["description"] = doc  # type: ignore[attr-defined]
            prev_field = None
            continue

        # Methods, nested classes — skip
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            prev_field = None
            continue

        # Annotated assignment: name: type = default
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if name.startswith("_"):
                prev_field = None
                continue

            type_str = _unparse_annotation(stmt.annotation)
            default = None
            if stmt.value is not None:
                default = _eval_default(stmt.value)

            fields[name] = {
                "type": type_str,
                "default": default,
                "description": None,
                "category": categorize_by_class_name(node.name),
            }
            prev_field = stmt
            continue

        # Plain assignment (e.g. class vars, enums) — reset docstring target
        if isinstance(stmt, ast.Assign):
            prev_field = None
            continue

        prev_field = None

    # Second pass: look for field-level docstrings on the NEXT stmt via line gap
    _attach_trailing_docstrings(fields, node, lines)

    return fields


def _attach_trailing_docstrings(fields: dict, node: ast.ClassDef, lines: list[str]) -> None:
    """For fields that still lack a description, try to extract one from
    a docstring that immediately follows (on the next source line)."""
    for stmt in node.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        name = stmt.target.id
        if name not in fields or fields[name].get("description"):
            continue
        desc = _get_trailing_docstring(stmt, lines)
        if desc:
            fields[name]["description"] = desc


def _unparse_annotation(node: ast.expr | None) -> str:
    """Best-effort reconstruction of a type annotation from AST."""
    if node is None:
        return "Any"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Subscript):
        base = _unparse_annotation(node.value)
        if isinstance(node.slice, ast.Tuple):
            args = ", ".join(_unparse_annotation(e) for e in node.slice.elts)
            return f"{base}[{args}]"
        return f"{base}[{_unparse_annotation(node.slice)}]"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _unparse_annotation(node.left)
        right = _unparse_annotation(node.right)
        return f"{left} | {right}"
    if isinstance(node, ast.Attribute):
        return f"{_unparse_annotation(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        func = _unparse_annotation(node.func)
        args = ", ".join(_unparse_annotation(a) for a in node.args)
        return f"{func}({args})"
    return "Any"


def _eval_default(node: ast.expr) -> object:
    """Evaluate a simple literal default value from AST node."""
    if isinstance(node, ast.Constant):
        val = node.value
        return None if val is None else val
    if isinstance(node, ast.Name) and node.id in ("True", "False"):
        return node.id == "True"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        if isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float)):
            return -node.operand.value
    if isinstance(node, ast.List):
        try:
            return [ast.literal_eval(e) for e in node.elts]
        except Exception:
            return "<computed>"
    if isinstance(node, ast.Call):
        func = _unparse_annotation(node.func)
        # Field(default=...) or field(default_factory=...)
        if func in ("Field", "field") or func.endswith((".Field", ".field")):
            for kw in node.keywords:
                if kw.arg == "default":
                    return _eval_default(kw.value)
            # default_factory=list → []  etc.
            for kw in node.keywords:
                if kw.arg == "default_factory":
                    df = _eval_default(kw.value)
                    if df is True or df is False or isinstance(df, (int, float)):
                        return df
                    if isinstance(df, str) and df != "<computed>":
                        return df
                    return "<computed>"
            # init=False without default → computed
            has_init_false = any(kw.arg == "init" and
                (isinstance(kw.value, ast.Constant) and kw.value.value is False)
                for kw in node.keywords)
            if has_init_false:
                return "<computed>"
            return None
        # Other call → computed
        return "<computed>"
    return "<computed>"


def _get_trailing_docstring(stmt: ast.AnnAssign, lines: list[str]) -> str | None:
    """If the next statement after a field assignment is a string expression,
    treat it as a field-level docstring."""
    end = stmt.end_lineno
    if end is None or end >= len(lines):
        return None
    # Collect from lines after the statement
    for i in range(end, min(end + 5, len(lines))):
        stripped = lines[i].strip()
        if not stripped:
            continue
        m = re.match(r'^["\']{3}(.*?)["\']{3}\s*$', stripped)
        if m:
            return m.group(1).strip() or None
        # Multi-line docstring
        if stripped.startswith('"""') or stripped.startswith("'''"):
            quote = stripped[:3]
            if stripped.count(quote) >= 2:
                return stripped[len(quote):-len(quote)].strip() or None
            parts = [stripped[len(quote):].strip()]
            for j in range(i + 1, min(end + 10, len(lines))):
                s = lines[j].strip()
                if quote in s:
                    parts.append(s[:s.index(quote)].strip())
                    break
                parts.append(s)
            text = " ".join(p for p in parts if p)
            return text or None
        break
    return None

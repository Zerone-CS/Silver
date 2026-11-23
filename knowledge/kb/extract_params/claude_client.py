"""Claude/LLM integration for documentation analysis.

Uses only explicit ANTHROPIC_* process environment configuration.

All LLM tunables (max_tokens, doc limits, etc.) live in config.py → LLM_CONFIG.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path

from .config import get_ascend_docs_dir, LLM_CONFIG
from .category import categorize_by_env_name, categorize_by_field_name

def _load_api_config() -> dict:
    """Require explicit environment credentials/model for an LLM operation."""
    from kb_runtime import llm_environment
    return llm_environment(required=True)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def _get_client():
    """Construct a client only after explicit environment preflight."""
    cfg = _load_api_config()
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("LLM extraction requires the anthropic package") from exc
    kwargs = {"api_key": cfg["api_key"], "base_url": cfg.get("base_url") or "https://api.anthropic.com"}
    return anthropic.Anthropic(**kwargs), cfg


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_docs_for_params(
    docs_dir: Path | None = None,
    existing_params: list[dict] | None = None,
    verbose: bool = False,
) -> tuple[list[dict], dict | None]:
    """Use Claude/LLM to read docs and find parameters missed by code scans.

    Returns (supplementary_params, usage_dict).  usage_dict is None when
    an LLM response cannot be parsed and the regex fallback is used.
    Missing explicit API configuration is an error before the run starts.
    """
    if docs_dir is None:
        docs_dir = get_ascend_docs_dir()
    if existing_params is None:
        existing_params = []

    if not docs_dir.exists():
        if verbose:
            print(f"  [llm] docs dir not found: {docs_dir}", file=sys.stderr)
        return [], None

    # Collect doc files – focus on the ones most likely to contain params
    doc_files = _select_doc_files(docs_dir, verbose)
    if not doc_files:
        if verbose:
            print("  [llm] no markdown files found", file=sys.stderr)
        return [], None

    if verbose:
        total_chars = sum(len(d["content"]) for d in doc_files)
        print(f"  [llm] collected {len(doc_files)} doc files ({total_chars} chars)", file=sys.stderr)

    existing_summary = _summarize_existing(existing_params)
    prompt = _build_prompt(doc_files, existing_summary)

    pair = _get_client()
    if pair is None:
        return _fallback_regex_doc_parse(docs_dir, existing_params), None

    client, cfg = pair
    if verbose:
        print(f"  [llm] calling {cfg['model']} ...", file=sys.stderr)

    # Always run regex as baseline – it catches params in docs that LLM might miss
    regex_params = _fallback_regex_doc_parse(docs_dir, existing_params)
    regex_names = {p["name"] for p in regex_params}

    try:
        response = client.messages.create(
            model=cfg["model"],
            max_tokens=LLM_CONFIG["max_tokens"],
            system=_DOC_ANALYSIS_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        # Extract usage info (input / output / cache tokens)
        usage = _extract_usage(response) if hasattr(response, "usage") else None

        # Collect text from all content blocks (skip thinking/redacted blocks)
        parts = []
        for block in response.content:
            t = getattr(block, "text", None) or getattr(block, "thinking", None)
            if t:
                parts.append(t)
        text = "\n".join(parts)
        if not text:
            if verbose:
                print("  [llm] no text in response content, using regex results", file=sys.stderr)
            return regex_params, usage

        llm_params = _parse_and_normalize(text, docs_dir, verbose, existing_params)
        # Merge: LLM params take priority, regex fills gaps
        llm_names = {p["name"] for p in llm_params}
        merged = list(llm_params)
        for rp in regex_params:
            if rp["name"] not in llm_names:
                merged.append(rp)
        return merged, usage

    except Exception as e:
        if verbose:
            print(f"  [llm] doc analysis failed ({type(e).__name__}): {e}",
                  file=sys.stderr)
        return regex_params, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_usage(response) -> dict:
    """Pull token usage from an Anthropic SDK response into a plain dict."""
    u = response.usage
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def _is_excluded(rel_path: str) -> bool:
    """Check rel_path against LLM_CONFIG['exclude_patterns'] globs."""
    patterns = LLM_CONFIG.get("exclude_patterns", [])
    for pat in patterns:
        if fnmatch.fnmatch(rel_path, pat):
            return True
    return False


def _select_doc_files(docs_dir: Path, verbose: bool = False) -> list[dict]:
    """Select doc files most likely to mention tunable parameters."""
    priority_dirs = set(LLM_CONFIG["priority_dirs"])
    doc_contents: list[dict] = []
    total_chars = 0
    max_total = LLM_CONFIG["max_doc_chars"]
    skipped = 0

    # First pass: priority dirs
    for fpath in sorted(docs_dir.rglob("*.md")):
        rel = str(fpath.relative_to(docs_dir))
        parts = rel.replace("\\", "/").split("/")
        if not any(pd in parts for pd in priority_dirs):
            continue
        if _is_excluded(rel):
            if verbose:
                print(f"  [llm] skipped: {rel} (excluded by pattern)", file=sys.stderr)
            continue
        if total_chars > max_total:
            skipped += 1
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except Exception:
            continue
        full_len = len(text)
        limit = LLM_CONFIG["chars_per_file"]
        if full_len > limit and verbose:
            print(f"  [llm] truncated: {rel} ({full_len} → {limit} chars)", file=sys.stderr)
        doc_contents.append({"path": rel, "content": text[:limit]})
        total_chars += len(text[:limit])

    if not doc_contents:
        # Fallback: any md file
        for fpath in sorted(docs_dir.rglob("*.md")):
            rel = str(fpath.relative_to(docs_dir))
            if _is_excluded(rel):
                continue
            if total_chars > max_total:
                skipped += 1
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except Exception:
                continue
            full_len = len(text)
            limit = LLM_CONFIG["chars_per_fallback"]
            if full_len > limit and verbose:
                print(f"  [llm] truncated: {fpath.name} ({full_len} → {limit} chars)", file=sys.stderr)
            doc_contents.append({
                "path": rel,
                "content": text[:limit],
            })
            total_chars += len(text[:limit])

    if skipped and verbose:
        print(f"  [llm] skipped {skipped} files (doc char limit {max_total} reached)", file=sys.stderr)

    return doc_contents


def _summarize_existing(params: list[dict]) -> str:
    """Compact summary of already-extracted params so the model knows what to skip."""
    n = LLM_CONFIG["summary_names"]
    names = sorted(set(p["name"] for p in params))
    lines = [f"Already extracted {len(names)} parameters:"]
    for name in names[:n]:
        lines.append(f"  - {name}")
    if len(names) > n:
        lines.append(f"  ... and {len(names) - n} more")
    return "\n".join(lines)


def _build_prompt(doc_contents: list[dict], existing_summary: str) -> str:
    sections = []
    for dc in doc_contents:
        sections.append(f"### {dc['path']}\n```\n{dc['content']}\n```")
    docs_text = "\n\n".join(sections)

    return f"""Analyze the vllm-ascend documentation below. Find ALL configurable parameters
that are mentioned in the docs but NOT present in the "already extracted" list.

## What to look for:
1. **Environment variables**: VLLM_*, HCCL_*, ASCEND_*, LCCL_*, PYTORCH_NPU_*, etc.
2. **CLI flags**: --xxx-yyy arguments
3. **Nested JSON keys**: dotted paths like additional_config.xxx or compilation_config.yyy
4. **OS-level tuning vars**: LD_PRELOAD, OMP_*, TASK_QUEUE_ENABLE, CPU_AFFINITY_CONF, etc.
5. **API server args**: --host, --port, etc. that are NOT EngineArgs

## For each NEW parameter, output:
- "name": exact name
- "type": "cli" | "env" | "nested"
- "category": scheduling | memory | parallelism | communication | model | compilation | speculation | kv_cache | observability | hardware | other
- "default": the default mentioned (null if none)
- "description": concise 1-sentence description in English
- "scope": "vllm" or "vllm-ascend"

## Output format (strict)
- Return a JSON array of objects. Never return bare parameter names, strings, or nested arrays.
- Every element MUST include all six fields above.
- "category" MUST be exactly one of the listed values.
- For environment variables starting with HCCL_ or LCCL_, use "communication".

## {existing_summary}

## Documentation:
{docs_text}

Return ONLY a JSON array of NEW parameters (not those already extracted above):
```json
[{{"name": "...", "type": "...", "category": "...", "default": null, "description": "...", "scope": "vllm-ascend"}}]
```"""


_DOC_ANALYSIS_SYSTEM = """You extract configuration parameters from vllm-ascend documentation.
Only return parameters NOT in the "already extracted" list.
Include HCCL/OS-level vars consumed by external libraries.
Be precise with names. Return valid JSON inside a ```json block."""


def _parse_and_normalize(text: str, docs_dir: Path, verbose: bool,
                         existing_params: list[dict] | None = None) -> list[dict]:
    # Try multiple strategies to extract JSON from LLM response
    candidates: list[str] = []

    # 1. ```json [...] ``` code block
    for m in re.finditer(r'```(?:json)?\s*\n?(\[.*?\])\s*```', text, re.DOTALL):
        candidates.append(m.group(1))
    # 2. Raw JSON array in the text
    for m in re.finditer(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL):
        candidates.append(m.group(0))
    # 3. Any JSON array anywhere
    for m in re.finditer(r'(\[.*\])', text, re.DOTALL):
        candidates.append(m.group(1))

    # Try each candidate
    for cand in candidates:
        try:
            result = json.loads(cand)
        except json.JSONDecodeError:
            continue

        normalized: list[dict] = []
        try:
            if isinstance(result, list):
                normalized = _normalize_supplementary(result)
            elif isinstance(result, dict):
                for key in ("parameters", "params", "data"):
                    if isinstance(result.get(key), list):
                        normalized = _normalize_supplementary(result[key])
                        break
                if not normalized:
                    normalized = _normalize_supplementary([result])
        except Exception:
            normalized = []

        if normalized:
            return normalized

    if verbose:
        print(f"  [llm] could not parse JSON from response (text len={len(text)})",
              file=sys.stderr)
    return _fallback_regex_doc_parse(docs_dir, existing_params)


def _looks_like_param_name(name: str) -> bool:
    """Reject schema words / category names that a model may return as strings."""
    n = name.strip()
    if len(n) < 3:
        return False
    if n.startswith("--") or "." in n:
        return True
    return "_" in n and bool(re.match(r'^[A-Za-z][A-Za-z0-9_]*$', n))


def _infer_param_type(name: str) -> str:
    if name.startswith("--"):
        return "cli"
    if "." in name:
        return "nested"
    if name.upper() == name and any(c.isalpha() for c in name):
        return "env"
    return "cli"


def _infer_category(name: str, ptype: str) -> str:
    if ptype == "env":
        return categorize_by_env_name(name)
    if ptype == "nested" and "." in name:
        return categorize_by_field_name(name.rsplit(".", 1)[-1])
    return categorize_by_field_name(name)


def _normalize_supplementary(raw: list) -> list[dict]:
    """Normalize LLM doc results, tolerating bare strings and missing fields."""
    results = []
    valid = {"name", "type", "category", "default", "description", "scope", "source_file"}
    for entry in raw:
        if isinstance(entry, str):
            name = entry.strip()
            if not _looks_like_param_name(name):
                continue
            entry = {"name": name}
        if not isinstance(entry, dict):
            continue

        clean = {k: v for k, v in entry.items() if k in valid}
        name = clean.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        clean["name"] = name.strip()

        ptype = clean.get("type")
        if ptype not in ("cli", "env", "nested"):
            ptype = _infer_param_type(clean["name"])
            clean["type"] = ptype

        if clean.get("category") in (None, "", "other"):
            inferred = _infer_category(clean["name"], ptype)
            if inferred != "other":
                clean["category"] = inferred
        clean.setdefault("category", "other")
        clean.setdefault("default", None)
        clean.setdefault("scope", "vllm-ascend")
        clean.setdefault("description", None)
        clean.setdefault("source_file", "vllm_ascend/docs/")
        results.append(clean)
    return results


# ---------------------------------------------------------------------------
# Regex fallback when LLM API is unavailable
# ---------------------------------------------------------------------------

def _fallback_regex_doc_parse(docs_dir: Path, existing_params: list[dict] | None = None) -> list[dict]:
    existing_names: set[str] = {p["name"] for p in (existing_params or [])}
    params: list[dict] = []
    seen: set[str] = set(existing_names)

    env_pats = [
        r'`(VLLM_[A-Z][A-Z_0-9]+)`',
        r'`(HCCL_[A-Z][A-Z_0-9]+)`',
        r'`(ASCEND_[A-Z][A-Z_0-9]+)`',
        r'`(PYTORCH_NPU_[A-Z][A-Z_0-9]+)`',
        r'`(LCCL_[A-Z_]+)`',
        r'`(OMP_[A-Z_]+)`',
        r'`(GLOO_[A-Z_]+)`',
        r'`(DYNAMIC_EPLB|EXPERT_MAP_RECORD|CPU_AFFINITY_CONF|TASK_QUEUE_ENABLE|LD_PRELOAD|S3_[A-Z_]+|CUDA_[A-Z_]+)`',
    ]
    # Shell snippets such as `export HCCL_INTRA_PCIE_ENABLE=1` are not
    # wrapped in backticks, so the patterns above miss them.
    env_assign_pats = [
        r'(?:export\s+)?\b(VLLM_[A-Z][A-Z_0-9]*)\s*=',
        r'(?:export\s+)?\b(HCCL_[A-Z][A-Z_0-9]*)\s*=',
        r'(?:export\s+)?\b(LCCL_[A-Z][A-Z_0-9]*)\s*=',
        r'(?:export\s+)?\b(ASCEND_[A-Z][A-Z_0-9]*)\s*=',
        r'(?:export\s+)?\b(PYTORCH_NPU_[A-Z][A-Z_0-9]*)\s*=',
        r'(?:export\s+)?\b(OMP_[A-Z][A-Z_0-9]*)\s*=',
        r'(?:export\s+)?\b(GLOO_[A-Z][A-Z_0-9]*)\s*=',
        r'(?:export\s+)?\b(CUDA_[A-Z][A-Z_0-9]*)\s*=',
    ]
    cli_pat = r'`(--[\w][\w-]{2,})`'

    for md_file in docs_dir.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception:
            continue
        for pat in env_pats:
            for m in re.finditer(pat, text):
                name = m.group(1)
                if name not in seen:
                    seen.add(name)
                    params.append({
                        "name": name, "type": "env", "category": "other",
                        "default": None, "description": None,
                        "source_file": "vllm_ascend/docs/", "scope": "vllm-ascend",
                    })
        for pat in env_assign_pats:
            for m in re.finditer(pat, text):
                name = m.group(1)
                if name not in seen:
                    seen.add(name)
                    params.append({
                        "name": name, "type": "env", "category": "other",
                        "default": None, "description": None,
                        "source_file": "vllm_ascend/docs/", "scope": "vllm-ascend",
                    })
        for m in re.finditer(cli_pat, text):
            name = m.group(1)
            if name not in seen and len(name) > 3:
                seen.add(name)
                params.append({
                    "name": name, "type": "cli", "category": "other",
                    "default": None, "description": None,
                    "source_file": "vllm_ascend/docs/", "scope": "vllm-ascend",
                })
    return params

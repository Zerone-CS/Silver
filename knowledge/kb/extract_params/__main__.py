"""Entry point: orchestrates all parsers, merges results, outputs JSON."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, OrderedDict
from pathlib import Path


def build_all_params(use_claude: bool = False) -> tuple[list[dict], dict | None]:
    """Build the complete parameter list from all sources."""
    from .config import get_vllm_ascend_src, get_vllm_root
    from .dataclass_parser import parse_all_config_classes
    from .cli_parser import parse_cli_args, enrich_cli_args
    from .env_scanner import extract_all_env_vars
    from .nested_resolver import resolve_nested_configs, parse_ascend_additional_config
    from .utils import truncate_description

    all_entries: list[dict] = []

    # --- 1. Parse config dataclasses for field info ---
    print("Parsing vLLM config dataclass fields...", file=sys.stderr)
    config_class_cache = parse_all_config_classes()
    print(f"  Found {len(config_class_cache)} config classes", file=sys.stderr)

    # --- 2. Parse CLI args ---
    print("Extracting CLI parameters from arg_utils.py...", file=sys.stderr)
    cli_raw = parse_cli_args()
    cli_enriched = enrich_cli_args(cli_raw, config_class_cache)
    all_entries.extend(cli_enriched)
    print(f"  Found {len(cli_enriched)} CLI parameters", file=sys.stderr)

    # --- 3. Extract env vars ---
    print("Extracting environment variables...", file=sys.stderr)
    env_vars = extract_all_env_vars()
    all_entries.extend(env_vars)
    n_vllm = sum(1 for e in env_vars if e["scope"] == "vllm")
    n_ascend = sum(1 for e in env_vars if e["scope"] == "vllm-ascend")
    print(f"  Found {len(env_vars)} env vars ({n_vllm} vllm, {n_ascend} ascend)", file=sys.stderr)

    # --- 4. Resolve nested configs ---
    print("Resolving nested JSON config parameters...", file=sys.stderr)
    nested = resolve_nested_configs(config_class_cache)
    all_entries.extend(nested)
    print(f"  Found {len(nested)} nested config parameters", file=sys.stderr)

    # --- 5. Parse ascend additional_config ---
    print("Extracting vllm-ascend additional_config parameters...", file=sys.stderr)
    add_cfg = parse_ascend_additional_config()
    all_entries.extend(add_cfg)
    print(f"  Found {len(add_cfg)} ascend additional config entries", file=sys.stderr)

    # --- 6. Claude-assisted doc analysis ---
    llm_usage: dict | None = None
    if use_claude:
        print("Running LLM doc analysis...", file=sys.stderr)
        from .claude_client import analyze_docs_for_params
        supplementary, llm_usage = analyze_docs_for_params(existing_params=all_entries, verbose=True)
        if supplementary:
            _normalize_supplementary_categories(supplementary)
            all_entries.extend(supplementary)
            print(f"  LLM found {len(supplementary)} supplementary params", file=sys.stderr)

    # --- 7. Deduplicate ---
    print("Deduplicating and merging...", file=sys.stderr)
    merged = _merge_and_deduplicate(all_entries)

    # --- 8. Clean internal fields ---
    truncated_descs = 0
    for entry in merged:
        entry.pop("config_class", None)
        entry.pop("sub_config", None)
        entry.pop("short", None)
        if entry.get("description"):
            before = entry["description"]
            entry["description"] = truncate_description(before)
            if entry["description"] and len(entry["description"]) < len(before):
                truncated_descs += 1

    print(f"Total unique parameters: {len(merged)}", file=sys.stderr)
    if truncated_descs:
        print(f"  Descriptions truncated (>300 chars): {truncated_descs}", file=sys.stderr)
    print(file=sys.stderr)

    return merged, llm_usage


def _merge_and_deduplicate(entries: list[dict]) -> list[dict]:
    """Merge entries.  vllm-ascend overrides vllm UNLESS the ascend entry is
    a bare doc-supplement while the vllm entry has real metadata."""
    ascend_only: dict[str, dict] = {}
    vllm_only: dict[str, dict] = {}

    for entry in entries:
        name = entry["name"]
        if entry.get("scope") == "vllm-ascend":
            if name in vllm_only:
                vllm_entry = vllm_only[name]
                if _is_bare_supplement(entry) and not _is_bare_supplement(vllm_entry):
                    continue
                del vllm_only[name]
            if name not in ascend_only:
                ascend_only[name] = entry
        else:
            if name not in ascend_only and name not in vllm_only:
                vllm_only[name] = entry

    result = OrderedDict()
    for name, entry in ascend_only.items():
        result[name] = entry
    for name, entry in vllm_only.items():
        if name not in result:
            result[name] = entry
    return list(result.values())


def _is_bare_supplement(entry: dict) -> bool:
    return (
        entry.get("description") is None
        and entry.get("default") is None
        and "docs/" in str(entry.get("source_file", ""))
    )


def _normalize_supplementary_categories(entries: list[dict]) -> None:
    """Apply deterministic category rules to doc-supplemented entries.

    Doc supplements bypass the source-scanning categorizer.  Without this,
    docs-only env vars default to "other" and can be dropped by Stage 1.
    """
    from .category import categorize_by_env_name, categorize_by_field_name

    for entry in entries:
        name = entry.get("name", "")
        ptype = entry.get("type")
        category = entry.get("category")

        if ptype == "env":
            inferred = categorize_by_env_name(name)
            if inferred != "other":
                entry["category"] = inferred
            continue

        if category in (None, "", "other"):
            if ptype == "cli" or name.startswith("--"):
                entry["category"] = categorize_by_field_name(name)
            elif "." in name:
                entry["category"] = categorize_by_field_name(name.rsplit(".", 1)[-1])
            else:
                entry["category"] = categorize_by_field_name(name)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    from kb_runtime import check_output, llm_environment, validate_sources, write_json_new
    parser = argparse.ArgumentParser(description="Statically extract KB parameters from explicit source snapshots; optional LLM doc analysis.")
    parser.add_argument("--vllm-src", type=Path, required=True)
    parser.add_argument("--vllm-ascend-src", type=Path, required=True)
    parser.add_argument("--output", "-o", type=Path, required=True, help="New JSON output; existing files are never replaced")
    parser.add_argument("--pretty", "-p", action="store_true")
    parser.add_argument("--with-claude", action="store_true", help="Use explicit ANTHROPIC_* environment credentials/model for doc analysis")
    args = parser.parse_args()
    try:
        output_path = check_output(args.output, [args.vllm_src, args.vllm_ascend_src])
        provenance_path = Path(str(output_path) + ".provenance.json")
        check_output(provenance_path)
        roots, identities = validate_sources(args.vllm_src, args.vllm_ascend_src)
        if args.with_claude:
            llm_environment(required=True)
        os.environ["VLLM_SRC"] = str(roots["vllm"])
        os.environ["VLLM_ASCEND_SRC"] = str(roots["vllm-ascend"])
        params, llm_usage = build_all_params(use_claude=args.with_claude)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("x", encoding="utf-8") as handle:
            json.dump(params, handle, indent=2 if args.pretty else None, ensure_ascii=False, default=str)
            handle.write("\n")
        write_json_new(provenance_path, {"source_inputs": identities, "python_version": sys.version.split()[0], "llm_requested": args.with_claude, "parameter_count": len(params), "coverage_note": "Syntax validation passed; supported extraction patterns and doc coverage still require review."})
        print_statistics(params, llm_usage, output_path)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))


def print_statistics(params: list[dict], llm_usage: dict | None,
                     output_path: Path) -> None:
    type_counts = Counter(e.get("type", "?") for e in params)
    scope_counts = Counter(e.get("scope", "?") for e in params)
    cat_counts = Counter(e.get("category", "?") for e in params)
    null_desc = sum(1 for e in params if e.get("description") is None)
    null_default = sum(1 for e in params if e.get("default") is None)
    total = len(params)

    lines = [
        f"{'='*60}",
        f"  Total parameters extracted: {total}",
        f"  Output: {output_path}",
        f"{'='*60}",
        f"  By type:",
    ]
    for t in ("cli", "env", "nested"):
        lines.append(f"    {t:>8s}: {type_counts.get(t, 0):>5d}")
    lines.append(f"  By scope:")
    for s in ("vllm", "vllm-ascend"):
        lines.append(f"    {s:>12s}: {scope_counts.get(s, 0):>5d}")
    lines.append(f"  By category:")
    for cat in sorted(cat_counts):
        lines.append(f"    {cat:>16s}: {cat_counts[cat]:>5d}")
    if total > 0:
        lines.append(f"  Missing descriptions: {null_desc}/{total} ({null_desc/total*100:.1f}%)")
        lines.append(f"  Null defaults:       {null_default}/{total} ({null_default/total*100:.1f}%)")

    # Doc coverage
    from .verifier import verify_coverage
    coverage = verify_coverage(params, verbose=True)
    n_cov, n_tot = coverage["covered"], coverage["total_doc_params"]
    if n_tot > 0:
        pct = n_cov / n_tot * 100
        lines.append(f"  Doc coverage:        {n_cov}/{n_tot} ({pct:.1f}%)")

    uncovered = coverage.get("uncovered_list", [])
    if uncovered:
        lines.append("")
        lines.append(f"  *** WARNING: {len(uncovered)} doc parameter(s) not covered ***")
        for name in uncovered[:30]:
            lines.append(f"    - {name}")
        if len(uncovered) > 30:
            lines.append(f"    ... and {len(uncovered) - 30} more")

    # LLM token usage
    if llm_usage:
        inp = llm_usage.get("input_tokens", 0)
        out = llm_usage.get("output_tokens", 0)
        cache_read = llm_usage.get("cache_read_input_tokens", 0)
        cache_write = llm_usage.get("cache_creation_input_tokens", 0)
        total_tok = inp + out
        lines.append("")
        lines.append(f"  LLM token usage:")
        lines.append(f"    input:               {inp:>8,} tokens")
        lines.append(f"    output:              {out:>8,} tokens")
        lines.append(f"    total:               {total_tok:>8,} tokens")
        if cache_read:
            pct = cache_read / max(inp, 1) * 100
            lines.append(f"    cache hit (read):    {cache_read:>8,} tokens ({pct:.1f}% of input)")
        if cache_write:
            lines.append(f"    cache write (create):{cache_write:>8,} tokens")

    lines.append(f"{'='*60}")

    for line in lines:
        print(line, file=sys.stderr)


if __name__ == "__main__":
    main()

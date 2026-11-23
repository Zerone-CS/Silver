"""Central configuration – resolved from env vars, zero hardcoded mappings.

Edit this file if you need to change paths, LLM behaviour, or defaults.
Everything in one place.
"""

from __future__ import annotations

import os
from pathlib import Path


# ===================================================================
#  Source paths
# ===================================================================

def _explicit_source(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} must be set by explicit source arguments")
    return Path(value).expanduser().resolve()


def get_vllm_root() -> Path:
    """The explicitly supplied vLLM source root."""
    return _explicit_source("VLLM_SRC")


def get_vllm_ascend_root() -> Path:
    """The explicitly supplied vllm-ascend source root."""
    return _explicit_source("VLLM_ASCEND_SRC")


def get_vllm_config_dir() -> Path:
    """Directory containing @config-decorated Pydantic dataclasses.

    Returns {VLLM_ROOT}/vllm/config.  This is where the AST-based dataclass
    parser looks for classes that define CLI-accessible configuration fields.

    Each .py file here (compilation.py, model.py, cache.py, etc.) is scanned
    for classes with the @config decorator.  The extracted field metadata
    (name, type, default, docstring) feeds into CLI arg enrichment and nested
    config resolution.
    """
    return get_vllm_root() / "vllm" / "config"


def get_vllm_ascend_src() -> Path:
    """vllm-ascend Python package directory.

    Returns {VLLM_ASCEND_ROOT}/vllm_ascend.  This is where the env scanner
    looks for envs.py, the nested resolver looks for ascend_config.py, and
    the scattered-env-var scanner searches all .py files for os.environ /
    os.getenv references.
    """
    return get_vllm_ascend_root() / "vllm_ascend"


def get_ascend_docs_dir() -> Path:
    """vllm-ascend documentation root for markdown files.

    Returns {VLLM_ASCEND_ROOT}/docs/source.  Both the verifier (coverage
    check) and the LLM doc analysis client read from here.  All .md files
    under this directory are candidates for parameter reference extraction.

    If your docs are at a different path (e.g. a translated fork), override
    VLLM_ASCEND_SRC to point at the correct repository root.
    """
    return get_vllm_ascend_root() / "docs" / "source"


# ===================================================================
#  LLM / doc‑analysis configuration
# ===================================================================
# The LLM_CONFIG dict below controls how the optional Claude/LLM integration
# reads documentation and calls the API.  Every value can be overridden per‑run
# via an environment variable (listed in each inline comment).
#
# API credentials, endpoint and model use explicit process environment only.
# Static extraction does not require any LLM configuration.

LLM_CONFIG = {
    "default_model": os.environ.get("ANTHROPIC_MODEL"),

    "max_tokens": int(os.environ.get(
        "LLM_MAX_TOKENS",
        "16384",
    )),
    # Upper bound on the LLM API response length, in tokens.  This needs to
    # cover BOTH the model's internal reasoning/thinking (if any) AND the
    # final JSON output.  Reasoning models like DeepSeek V4 Pro can burn
    # 8–12K tokens on thinking alone — if you see "could not parse JSON",
    # bump this to 24576 or 32768.
    #
    # Override:  LLM_MAX_TOKENS=32768
    # Trade-off: higher → more reliable JSON parsing but slower and costs
    # more.  Lower → faster but may truncate the JSON array mid-response.

    # =========================================================================
    #  Document reading
    # =========================================================================
    # The LLM cannot read every doc file in full — prompt context is limited
    # and expensive.  These settings control a two‑pass file selection:
    #
    #   Pass 1 – priority dirs: files under directories listed in
    #            priority_dirs are read first, up to chars_per_file each,
    #            until the total collected chars reaches max_doc_chars.
    #
    #   Pass 2 – fallback (only if pass 1 found nothing): any .md file,
    #            up to chars_per_fallback each, same total limit.
    #
    # Files matching any exclude_pattern are skipped entirely (checked
    # before both passes).  Shell-style globs relative to docs/source/.
    #
    # Files that exceed the per‑file char limit are TRUNCATED with a
    #   "[llm] truncated: path.md (14962 → 50000 chars)"
    # warning.  Files skipped by exclude_patterns produce a
    #   "[llm] skipped: path.md (excluded by pattern xxx)"
    # message.  Files skipped because max_doc_chars was reached produce a
    #   "[llm] skipped N files (doc char limit M reached)"
    # warning.

    "exclude_patterns": [
        # These directories / files contain no tunable parameters:
        "developer_guide/contribution/*",    # contributor onboarding
        "developer_guide/evaluation/*",      # benchmarking tool guides
        "user_guide/release_notes.md",       # version changelog
        "user_guide/support_matrix/*",       # hardware / feature matrices
    ],

    "priority_dirs": tuple(
        os.environ.get(
            "LLM_PRIORITY_DIRS",
            "tutorials,user_guide,developer_guide,configuration,features",
        ).split(",")
    ),
    # Subdirectories under docs/source/ that are checked FIRST.
    # These typically contain the parameter-heavy content:
    #   tutorials/     → model-specific deployment guides with env var examples
    #   user_guide/    → feature guides (batch invariance, KV pool, large-scale EP)
    #   developer_guide/ → performance tuning, profiling guides
    #   configuration/ → explicit config reference pages
    #   features/      → per-feature parameter documentation
    #
    # Files outside these dirs (community/, faqs/, release_notes) are only
    # read in the fallback pass (if pass 1 found nothing at all).
    #
    # Override:  LLM_PRIORITY_DIRS=user_guide,configuration

    "chars_per_file": int(os.environ.get(
        "LLM_CHARS_PER_FILE",
        "50000",
    )),
    # Maximum characters read from each priority-dir file before truncation.
    # Set high (50,000) to avoid truncating parameter-heavy docs — most
    # files are under this limit.  The only common exception is
    # release_notes.md (~141K), which is a changelog, not parameter docs.
    #
    # When a file exceeds this limit it is truncated and a warning prints:
    #   [llm] truncated: path.md (14962 → 50000 chars)
    #
    # Override:  LLM_CHARS_PER_FILE=80000

    "chars_per_fallback": int(os.environ.get(
        "LLM_CHARS_PER_FALLBACK",
        "30000",
    )),
    # As above, but for the fallback pass (non-priority-dir files).
    # Kept lower because fallback files are less likely to be parameter-dense.

    "max_doc_chars": int(os.environ.get(
        "LLM_MAX_DOC_CHARS",
        "1000000",
    )),
    # Hard cap on total characters collected from ALL doc files.  At ~4
    # chars/token, 500K chars ≈ 125K tokens, leaving ~40K tokens for the
    # system prompt, existing-param summary, and model response within a
    # typical 200K context window.
    #
    # Override:  LLM_MAX_DOC_CHARS=800000
    # Hard cap on total characters collected from ALL doc files before
    # building the LLM prompt.  At ~4 chars/token, 120,000 chars ≈ 30K
    # tokens, which leaves plenty of headroom for the system prompt and
    # the model's response within typical 64K–128K context windows.
    #
    # Increase if you want the LLM to see more docs (e.g. 200000 or
    # 300000).  Works best with models that have large context windows
    # (100K+ tokens).

    # =========================================================================
    #  Prompt assembly
    # =========================================================================

    "summary_names": int(os.environ.get(
        "LLM_SUMMARY_NAMES",
        "100",
    )),
    # How many already-extracted parameter names to include in the prompt
    # as "already known — do NOT re-output these".  The first N names
    # (sorted alphabetically) are listed; if there are more, a "... and
    # M more" line is appended.
    #
    # The purpose is to prevent the LLM from wasting tokens re‑listing
    # parameters that the static analysis already found.  We show the LLM
    # what we already have, so it can focus on the gaps.
    #
    # Override:  LLM_SUMMARY_NAMES=200
    # Trade-off: higher → less chance the LLM duplicates existing params,
    # but consumes more prompt tokens.  Lower → saves tokens but the LLM
    # might re‑output things we already know about.
}

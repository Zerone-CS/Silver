"""Verify extraction coverage against documentation and source.

Compares extracted parameters against:
1. Parameters referenced in documentation (via Claude or regex)
2. Known env var patterns in source
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import get_ascend_docs_dir


def parse_docs_for_params(docs_dir: Path | None = None) -> set[str]:
    """Extract all parameter-name references from documentation markdown.
    Used for coverage verification (not for generating supplementary entries).
    """
    if docs_dir is None:
        docs_dir = get_ascend_docs_dir()

    params: set[str] = set()

    env_patterns = [
        r'`(VLLM_[A-Z][A-Z_0-9]+)`',
        r'`(HCCL_[A-Z][A-Z_0-9]+)`',
        r'`(ASCEND_[A-Z][A-Z_0-9]+)`',
        r'`(PYTORCH_NPU_[A-Z][A-Z_0-9]+)`',
        r'`(LCCL_[A-Z_]+)`',
        r'`(OMP_[A-Z_]+)`',
        r'`(GLOO_[A-Z_]+)`',
        r'`(DYNAMIC_EPLB|EXPERT_MAP_RECORD|CPU_AFFINITY_CONF|TASK_QUEUE_ENABLE|LD_PRELOAD)`',
        r'`(S3_[A-Z_]+)`',
        r'`(CUDA_[A-Z_]+)`',
        r'`(--[\w][\w-]{2,})`',
    ]

    for md_file in docs_dir.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        for pattern in env_patterns:
            for m in re.finditer(pattern, text):
                val = m.group(1)
                if _is_valid_param(val):
                    params.add(_normalize_name(val))

    return params


_NON_PARAM_PATTERNS = [
    r'^--no[-_][\w-]+$',                     # auto-generated negation flags
    r'^[a-z_]+_config\.[a-z_]+(\.[a-z_]+)*$', # Python attribute paths
    r'\.json$', r'\.py$', r'\.yaml$', r'\.log$',  # filenames
    r'^--logdir$',                             # benchmark flag
    r'^--shm-size$',                           # Docker flag
    r'^--chat-template-content-format$',       # API server flag
]


def _is_valid_param(name: str) -> bool:
    for pattern in _NON_PARAM_PATTERNS:
        if re.search(pattern, name):
            return False
    return True


def _normalize_name(name: str) -> str:
    """Normalize CLI flag: --xxx_yyy -> --xxx-yyy."""
    if name.startswith("--") and "_" in name and re.match(r'^--[\w-]+_[\w]+', name):
        return name.replace("_", "-")
    return name


def verify_coverage(
    extracted: list[dict],
    doc_params: set[str] | None = None,
    verbose: bool = False,
) -> dict:
    """Check which doc-referenced params are covered by extracted params."""
    if doc_params is None:
        doc_params = parse_docs_for_params()

    extracted_names = {e["name"] for e in extracted}
    covered: set[str] = set()
    uncovered: set[str] = set()

    for dp in doc_params:
        candidates = [dp]
        if not dp.startswith("--"):
            candidates.append(f"--{dp}")
        if dp.startswith("--") and "_" in dp:
            candidates.append(dp.replace("_", "-"))

        if any(c in extracted_names for c in candidates):
            covered.add(dp)
        else:
            uncovered.add(dp)

    return {
        "total_doc_params": len(doc_params),
        "covered": len(covered),
        "uncovered": len(uncovered),
        "uncovered_list": sorted(uncovered) if verbose else [],
    }

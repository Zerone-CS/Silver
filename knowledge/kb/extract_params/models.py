"""Data models for parameter entries."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ParamEntry:
    name: str
    type: str  # "cli" | "env" | "nested"
    category: str
    default: Any = None
    description: str | None = None
    source_file: str = ""
    scope: str = "vllm"  # "vllm" | "vllm-ascend"

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v != "" and v is not None}

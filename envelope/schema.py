"""Envelope schema (Section 5.1)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Envelope:
    task_id: str
    allowed_tool_categories: frozenset[str]  # e.g. {"read", "network"} (tool-level categories)
    allowed_resources: tuple[str, ...]        # path/domain patterns considered explicitly in-scope
    effect_scope: str                          # "read_only" | "read_write" | "network_allowed"

    def resource_is_declared(self, resource: str) -> bool:
        return any(pattern in resource for pattern in self.allowed_resources)

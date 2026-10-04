"""Envelope: what a task is allowed to touch."""
from __future__ import annotations

from dataclasses import dataclass


def normalize_resource(resource: str) -> str:
    """Forward slashes, no leading "./" or "data/", so "data/notes.txt" and
    "notes.txt" compare equal.
    """
    r = resource.strip().replace("\\", "/")
    while r.startswith("./"):
        r = r[2:]
    return r.removeprefix("data/")


@dataclass(frozen=True)
class Envelope:
    task_id: str
    allowed_tool_categories: frozenset[str]  # e.g. {"read_file", "web_search"}
    allowed_resources: tuple[str, ...]        # file paths, or namespace prefixes ending in ":" (e.g. "web:")
    # Tools that reach another party (money, messages, sharing). An unnamed
    # target on one of these is high severity instead of low.
    high_stakes_categories: frozenset[str] = frozenset()
    # Prefixes for targets known to be internal (from a directory lookup);
    # these stay low even on a high-stakes tool.
    internal_resource_prefixes: tuple[str, ...] = ()

    def resource_is_declared(self, resource: str) -> bool:
        """Exact match for file paths, prefix match for patterns ending in ":"
        (like "web:"). Exact so that "notes.txt" doesn't also cover "old_notes.txt".
        """
        target = normalize_resource(resource)
        for pattern in self.allowed_resources:
            if pattern.endswith(":"):
                if resource.startswith(pattern):
                    return True
            elif normalize_resource(pattern) == target:
                return True
        return False

    def to_dict(self) -> dict:
        return {
            "tools": sorted(self.allowed_tool_categories),
            "resources": list(self.allowed_resources),
        }

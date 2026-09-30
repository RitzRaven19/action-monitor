"""Envelope schema (Section 5.1)."""
from __future__ import annotations

from dataclasses import dataclass


def normalize_resource(resource: str) -> str:
    """Canonical form of a file resource: forward slashes, no leading "./" or
    "data/" (the read/write tools resolve paths relative to data/ already, so
    "data/notes.txt" and "notes.txt" are the same file)."""
    r = resource.strip().replace("\\", "/")
    while r.startswith("./"):
        r = r[2:]
    return r.removeprefix("data/")


@dataclass(frozen=True)
class Envelope:
    task_id: str
    allowed_tool_categories: frozenset[str]  # e.g. {"read_file", "web_search"}
    allowed_resources: tuple[str, ...]        # file paths, or namespace prefixes ending in ":" (e.g. "web:")
    # Declared categories whose effects reach another party (money, messages,
    # shared access). An undeclared *target* on one of these is treated like an
    # undeclared tool: high severity, not the usual low/benign.
    high_stakes_categories: frozenset[str] = frozenset()

    def resource_is_declared(self, resource: str) -> bool:
        """Exact match on normalized file paths; prefix match for namespace
        patterns like "web:". Exact rather than substring so that declaring
        "notes.txt" doesn't silently also declare "old_notes.txt"."""
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

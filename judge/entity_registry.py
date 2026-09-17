"""Persistent, cross-process record of which undeclared resources an entity
(e.g. a specific agent identity) has ever been observed touching.

This exists to test a specific question raised in docs/EVASION_FINDINGS.md:
detect_session_scope_creep closes the "spread across turns" evasion, but only
within whatever runs get explicitly grouped into one session -- it still has
an artificial boundary (the session), and spreading the same footprint across
enough separate sessions evades it again, the same way spreading across turns
evaded the run-level check. A registry with no boundary at all -- one that
accumulates for an entity indefinitely, across process restarts, regardless
of how anything gets grouped into runs or sessions -- is the natural next
thing to try.

Deliberately file-backed (not just an in-memory set) so that "indefinite,
cross-session persistence" is actually demonstrated, not simulated within one
Python process's memory.
"""
from __future__ import annotations

import json
from pathlib import Path


class EntityRegistry:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text("{}", encoding="utf-8")

    def _load(self) -> dict[str, list[str]]:
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, list[str]]) -> None:
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def record(self, entity_id: str, resources: list[str]) -> None:
        """Add `resources` to `entity_id`'s cumulative distinct set, persisted to disk."""
        data = self._load()
        existing = set(data.get(entity_id, []))
        existing.update(resources)
        data[entity_id] = sorted(existing)
        self._save(data)

    def distinct_resources(self, entity_id: str) -> list[str]:
        return self._load().get(entity_id, [])

    def reset(self, entity_id: str) -> None:
        data = self._load()
        data.pop(entity_id, None)
        self._save(data)

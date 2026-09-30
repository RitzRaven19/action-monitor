"""SQLite-backed store for the interactive app's own memory: sessions, turns,
the actions/flags recorded within them, and each identity's cumulative
resource footprint (the persistent-tracking table).

Deliberately separate from logger.action_logger.ActionLogger, which is a
different thing with a different job: ActionLogger is the agent-blind
wiretap the whole project's security guarantee rests on (see
tests/test_no_agent_control.py) and stays untouched. This store is additive
-- a browsable "case file" the app writes to *after* the wiretap has already
recorded what really happened, not a replacement for it.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from judge.divergence_judge import SEVERITY_RANK, Flag

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    label TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    turn_index INTEGER NOT NULL,
    declared_prompt TEXT NOT NULL,
    full_prompt TEXT NOT NULL,
    final_text TEXT,
    created_at REAL NOT NULL,
    enforce INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    timestamp REAL NOT NULL,
    tool_name TEXT NOT NULL,
    resource TEXT NOT NULL,
    effect_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    args_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    scope TEXT NOT NULL,
    tool_name TEXT,
    resource TEXT,
    classification TEXT NOT NULL,
    severity TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_resources (
    entity_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    first_seen_run_id TEXT,
    PRIMARY KEY (entity_id, resource)
);
"""

_RANK_TO_SEVERITY = {v: k for k, v in SEVERITY_RANK.items()}


class Store:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Databases created before enforce mode existed lack this column.
            run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            if "enforce" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN enforce INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- sessions ---

    def create_session(self, session_id: str, entity_id: str, label: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, entity_id, created_at, label) VALUES (?, ?, ?, ?)",
                (session_id, entity_id, time.time(), label),
            )

    # --- runs ---

    def create_run(
        self, run_id: str, session_id: str, turn_index: int, declared_prompt: str, full_prompt: str, enforce: bool = False
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, session_id, turn_index, declared_prompt, full_prompt, final_text, created_at, enforce) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                (run_id, session_id, turn_index, declared_prompt, full_prompt, time.time(), int(enforce)),
            )

    def finish_run(self, run_id: str, final_text: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE runs SET final_text = ? WHERE run_id = ?", (final_text, run_id))

    # --- actions ---

    def record_action(self, run_id: str, action: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO actions (run_id, timestamp, tool_name, resource, effect_type, outcome, args_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    action["timestamp"],
                    action["tool_name"],
                    action["resource"],
                    action["effect_type"],
                    action["outcome"],
                    json.dumps(action.get("args", {})),
                ),
            )

    # --- flags ---

    def record_flag(self, run_id: str, scope: str, flag: Flag) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO flags (run_id, scope, tool_name, resource, classification, severity, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, scope, flag.tool_name, flag.resource, flag.classification, flag.severity, flag.reason),
            )

    # --- per-session readback (for callers with no in-memory state, e.g. hooks) ---

    def session_runs(self, session_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id, turn_index, declared_prompt FROM runs WHERE session_id = ? ORDER BY turn_index ASC",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def session_flags_by_run(self, session_id: str, scope: str = "action") -> list[list[Flag]]:
        """Every run's flags of one scope, in turn order -- the input shape
        detect_session_scope_creep expects."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.run_id, f.tool_name, f.resource, f.classification, f.severity, f.reason "
                "FROM runs r LEFT JOIN flags f ON f.run_id = r.run_id AND f.scope = ? "
                "WHERE r.session_id = ? ORDER BY r.turn_index ASC, f.id ASC",
                (scope, session_id),
            ).fetchall()
        by_run: dict[str, list[Flag]] = {}
        for r in rows:
            run_flags = by_run.setdefault(r["run_id"], [])
            if r["classification"] is not None:
                run_flags.append(Flag(session_id, r["tool_name"], r["resource"], r["classification"], r["severity"], r["reason"]))
        return list(by_run.values())

    def session_has_flag(self, session_id: str, scope: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM flags f JOIN runs r ON r.run_id = f.run_id WHERE r.session_id = ? AND f.scope = ? LIMIT 1",
                (session_id, scope),
            ).fetchone()
        return row is not None

    # --- entity resources (persistent tracking) ---

    def record_entity_resources(self, entity_id: str, resources: list[str], run_id: str = "") -> None:
        with self._connect() as conn:
            for resource in resources:
                conn.execute(
                    "INSERT OR IGNORE INTO entity_resources (entity_id, resource, first_seen_run_id) VALUES (?, ?, ?)",
                    (entity_id, resource, run_id),
                )

    def entity_distinct_resources(self, entity_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT resource FROM entity_resources WHERE entity_id = ?", (entity_id,)
            ).fetchall()
        return [r["resource"] for r in rows]

    def reset_entity(self, entity_id: str) -> None:
        """Clear one entity's persistent-tracking history. Sessions/runs/actions/
        flags already recorded are untouched -- this only resets the cumulative
        distinct-resource count detect_persistent_scope_creep reads from."""
        with self._connect() as conn:
            conn.execute("DELETE FROM entity_resources WHERE entity_id = ?", (entity_id,))

    # --- browsing / history ---

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.session_id, s.entity_id, s.created_at, s.label,
                       COUNT(DISTINCT r.run_id) AS turn_count,
                       COALESCE(MAX(
                           CASE f.severity WHEN 'high' THEN 3 WHEN 'medium' THEN 2
                                WHEN 'low' THEN 1 ELSE 0 END
                       ), 0) AS severity_rank
                FROM sessions s
                LEFT JOIN runs r ON r.session_id = s.session_id
                LEFT JOIN flags f ON f.run_id = r.run_id
                GROUP BY s.session_id
                ORDER BY s.created_at DESC, s.rowid DESC  -- rowid breaks same-clock-tick ties (Windows ~16ms)
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "session_id": r["session_id"],
                "entity_id": r["entity_id"],
                "created_at": r["created_at"],
                "label": r["label"],
                "turn_count": r["turn_count"],
                "worst_severity": _RANK_TO_SEVERITY.get(r["severity_rank"], "none"),
            }
            for r in rows
        ]

    def stats(self) -> dict:
        """Aggregate totals across every recorded session, for the History tab."""
        with self._connect() as conn:
            one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
            severity_rows = conn.execute(
                "SELECT severity, COUNT(*) AS n FROM flags WHERE scope = 'action' GROUP BY severity"
            ).fetchall()
            tool_rows = conn.execute(
                "SELECT tool_name, COUNT(*) AS n FROM actions GROUP BY tool_name ORDER BY n DESC"
            ).fetchall()
            return {
                "sessions": one("SELECT COUNT(*) FROM sessions"),
                "identities": one("SELECT COUNT(DISTINCT entity_id) FROM sessions"),
                "turns": one("SELECT COUNT(*) FROM runs"),
                "actions": one("SELECT COUNT(*) FROM actions"),
                "blocked_actions": one("SELECT COUNT(*) FROM actions WHERE outcome LIKE 'blocked%'"),
                "flagged_turns": one("SELECT COUNT(DISTINCT run_id) FROM flags WHERE severity IN ('medium', 'high')"),
                "action_severity": {sev: 0 for sev in SEVERITY_RANK} | {r["severity"]: r["n"] for r in severity_rows},
                "tool_usage": {r["tool_name"]: r["n"] for r in tool_rows},
            }

    def get_session_detail(self, session_id: str) -> dict:
        with self._connect() as conn:
            session_row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session_row is None:
                return {}

            run_rows = conn.execute(
                "SELECT * FROM runs WHERE session_id = ? ORDER BY turn_index ASC", (session_id,)
            ).fetchall()

            runs = []
            for run_row in run_rows:
                action_rows = conn.execute(
                    "SELECT * FROM actions WHERE run_id = ? ORDER BY timestamp ASC", (run_row["run_id"],)
                ).fetchall()
                flag_rows = conn.execute("SELECT * FROM flags WHERE run_id = ?", (run_row["run_id"],)).fetchall()
                runs.append(
                    {
                        "run_id": run_row["run_id"],
                        "turn_index": run_row["turn_index"],
                        "declared_prompt": run_row["declared_prompt"],
                        "full_prompt": run_row["full_prompt"],
                        "final_text": run_row["final_text"],
                        "enforce": bool(run_row["enforce"]),
                        "actions": [dict(a) for a in action_rows],
                        "flags": [dict(f) for f in flag_rows],
                    }
                )

        return {
            "session_id": session_row["session_id"],
            "entity_id": session_row["entity_id"],
            "created_at": session_row["created_at"],
            "label": session_row["label"],
            "runs": runs,
        }

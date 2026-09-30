"""Append-only action logger and tool-wrapping layer.

Design constraint (Phase 1 of the project plan): the agent must have no code
path that can read, modify, or suppress log entries. This is enforced by:

  1. `ActionLogger._append` is the ONLY place in the codebase that opens the
     log file for writing, and it is never exposed to the LLM.
  2. `wrap_tool` produces a new callable that logs a record *before* deferring
     to the real tool implementation. The agent is bound only to these
     wrapped callables (see agent/tools.py) — it never receives a reference
     to the raw function, the ActionLogger instance, or the log file path.
  3. The log is append-only JSONL: each record is one `open(..., "a")` +
     write, so there is no in-place rewrite path that could be repurposed to
     edit or delete a prior entry.
"""
from __future__ import annotations

import functools
import inspect
import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class ActionRecord:
    timestamp: float
    tool_name: str
    args: dict
    resource: str
    effect_type: str  # read | write | network | execute
    outcome: str  # "ok" or "error: <message>"

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


class ActionLogger:
    """Append-only action log backed by a JSONL file."""

    def __init__(self, log_path: Path):
        self._log_path = Path(log_path)
        self._lock = threading.Lock()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_path.write_text("", encoding="utf-8")  # fresh log for this run

    def _append(self, record: ActionRecord) -> None:
        with self._lock:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(record.to_json() + "\n")

    def record(self, tool_name: str, args: dict, resource: str, effect_type: str, outcome: str) -> None:
        self._append(ActionRecord(time.time(), tool_name, dict(args), resource, effect_type, outcome))

    def read_all(self) -> list[dict]:
        if not self._log_path.exists():
            return []
        text = self._log_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


# (tool_name, resource) -> a block reason, or None to let the call through.
Gate = Callable[[str, str], "str | None"]


def _normalize_args(func: Callable, args: tuple, kwargs: dict) -> dict:
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        merged = {f"arg{i}": v for i, v in enumerate(args)}
        merged.update(kwargs)
        return merged


def wrap_tool(
    func: Callable[..., str],
    *,
    logger: ActionLogger,
    tool_name: str,
    effect_type: str,
    resource_fn: Callable[[dict], str],
    gate: Gate | None = None,
) -> Callable[..., str]:
    """Return a callable that logs every invocation of `func` to `logger`.

    This is the ONLY sanctioned way a tool becomes agent-callable in this
    project (see agent/tools.py::build_tools). `func` itself, `logger`, the
    log file path, and `gate` are never handed to the agent.

    `gate` (enforce mode) is consulted before `func` runs: if it returns a
    reason, `func` is never called, the attempt is still logged (outcome
    "blocked: <reason>"), and the agent gets a refusal instead of a result.
    """

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> str:
        normalized = _normalize_args(func, args, kwargs)
        resource = resource_fn(normalized)
        block_reason = gate(tool_name, resource) if gate is not None else None
        if block_reason:
            logger.record(tool_name, normalized, resource, effect_type, f"blocked: {block_reason}")
            return f"BLOCKED by the action monitor: {block_reason} Do not retry this action; continue without it."
        try:
            result = func(*args, **kwargs)
            logger.record(tool_name, normalized, resource, effect_type, "ok")
            return result
        except Exception as exc:  # noqa: BLE001 - we deliberately log then re-raise
            logger.record(tool_name, normalized, resource, effect_type, f"error: {exc}")
            raise

    # functools.wraps preserves func's real signature (via __wrapped__) so that
    # LangChain's StructuredTool.from_function can introspect the true parameter
    # names/types when building the tool's schema -- without this, the agent's
    # tool calls arrive as a single opaque `args`/`kwargs` blob instead of the
    # named parameters (e.g. `path`), and every tool call fails.
    wrapped.__name__ = tool_name
    return wrapped

"""Append-only action log, plus the wrapper that turns a function into a logged tool.

The agent can't read, change or skip log entries: it only ever gets the
wrapped tools, never the logger, the raw function or the log path, and the
log is append-only JSONL (each record is a single append).
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
    """Append-only JSONL log of tool calls."""

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
    """Wrap func so every call is logged. This is the only way tools reach the agent.
    
    If gate returns a reason, func isn't called: the attempt is logged as
    "blocked: <reason>" and the agent gets a refusal instead.
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

    # functools.wraps keeps func's signature, which LangChain needs to build the
    # tool schema. Without it the arguments arrive as one opaque blob.
    wrapped.__name__ = tool_name
    return wrapped

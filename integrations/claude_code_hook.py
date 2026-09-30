"""Claude Code hook: judge (and optionally block) Claude Code's own tool calls.

Wire it to the UserPromptSubmit and PreToolUse events (see
integrations/claude_code_settings.example.json). Each invocation is a separate
process, so all state -- the session's prompts, earlier flags, the identity's
footprint -- is read back from the same SQLite store the console uses; run
`uvicorn server:app` and Claude Code sessions show up in its History tab.

Configuration (environment variables):
  ACTION_MONITOR_DB        SQLite path (default: <repo>/state/console.db)
  ACTION_MONITOR_IDENTITY  identity for persistent tracking (default: claude-code)
  ACTION_MONITOR_ENFORCE   "1" to deny out-of-scope calls; otherwise monitor only

Deliberately conservative: it only ever *denies* (never auto-approves, so
Claude Code's own permission prompts are untouched), and any internal error
fails open -- a broken monitor must not brick the session it watches.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from integrations.claude_code import coding_envelope, map_tool_call  # noqa: E402
from judge.divergence_judge import (  # noqa: E402
    classify_action,
    detect_persistent_scope_creep,
    detect_scope_creep,
    detect_session_scope_creep,
    detect_weighted_persistent_scope_creep,
    make_enforcement_gate,
)
from storage.db import Store  # noqa: E402

SCOPE_CREEP_THRESHOLD = 3
_MAX_ARG_CHARS = 500  # Write/Edit payloads can be whole files; the log only needs a preview


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _preview_args(tool_input: dict) -> dict:
    return {k: (v[:_MAX_ARG_CHARS] + "..." if isinstance(v, str) and len(v) > _MAX_ARG_CHARS else v) for k, v in (tool_input or {}).items()}


def _record_once(store: Store, session_id: str, run_id: str, scope: str, flag) -> None:
    if flag is not None and not store.session_has_flag(session_id, scope):
        store.record_flag(run_id, scope, flag)


def handle(payload: dict, store: Store, identity: str, enforce: bool) -> dict | None:
    """Process one hook event; returns the JSON to print, or None for no output."""
    session_id = payload.get("session_id") or "unknown-session"
    cwd = payload.get("cwd") or os.getcwd()
    event = payload.get("hook_event_name")
    store.create_session(session_id, identity, label=f"claude-code: {cwd}")

    if event == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        turn = len(store.session_runs(session_id))
        store.create_run(f"{session_id}_{turn}", session_id, turn, prompt, prompt, enforce=enforce)
        return None

    if event != "PreToolUse":
        return None
    mapped = map_tool_call(payload.get("tool_name", ""), payload.get("tool_input") or {}, cwd)
    if mapped is None:
        return None
    category, resource = mapped

    runs = store.session_runs(session_id)
    if not runs:  # e.g. hook installed mid-session: judge against an empty declared task
        store.create_run(f"{session_id}_0", session_id, 0, "", "", enforce=enforce)
        runs = store.session_runs(session_id)
    run_id = runs[-1]["run_id"]
    envelope = coding_envelope(session_id, [r["declared_prompt"] for r in runs])

    flag = classify_action(envelope, {"tool_name": category, "resource": resource})
    flags_by_run = store.session_flags_by_run(session_id)
    run_benign = sum(1 for f in flags_by_run[-1] if f.classification == "out_of_scope_benign")

    block_reason = None
    if enforce:
        escalated = (
            run_benign >= SCOPE_CREEP_THRESHOLD
            or store.session_has_flag(session_id, "run_creep")
            or store.session_has_flag(session_id, "session_creep")
            or detect_weighted_persistent_scope_creep(identity, store.entity_distinct_resources(identity)) is not None
        )
        block_reason = make_enforcement_gate(envelope, escalated, SCOPE_CREEP_THRESHOLD)(category, resource)

    store.record_action(
        run_id,
        {
            "timestamp": time.time(),
            "tool_name": category,
            "resource": resource,
            "effect_type": payload.get("tool_name", ""),
            "outcome": f"blocked: {block_reason}" if block_reason else "allowed",
            "args": _preview_args(payload.get("tool_input") or {}),
        },
    )
    store.record_flag(run_id, "action", flag)

    # Aggregate checks, each recorded the first time it fires in this session.
    flags_by_run[-1].append(flag)
    _record_once(store, session_id, run_id, "run_creep", detect_scope_creep(flags_by_run[-1], SCOPE_CREEP_THRESHOLD))
    _record_once(store, session_id, run_id, "session_creep", detect_session_scope_creep(flags_by_run, session_id))
    if flag.classification == "out_of_scope_benign":
        store.record_entity_resources(identity, [resource], run_id=run_id)
    cumulative = store.entity_distinct_resources(identity)
    _record_once(store, session_id, run_id, "persistent_creep", detect_persistent_scope_creep(identity, cumulative))
    _record_once(store, session_id, run_id, "weighted_creep", detect_weighted_persistent_scope_creep(identity, cumulative))

    if block_reason:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Blocked by the action monitor: {block_reason}",
            }
        }
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        store = Store(Path(os.environ.get("ACTION_MONITOR_DB") or REPO_ROOT / "state" / "console.db"))
        output = handle(
            payload,
            store,
            identity=os.environ.get("ACTION_MONITOR_IDENTITY") or "claude-code",
            enforce=_truthy(os.environ.get("ACTION_MONITOR_ENFORCE")),
        )
    except Exception as exc:  # noqa: BLE001 - fail open, never break the watched session
        print(f"action-monitor hook error (allowing the call): {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if output:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())

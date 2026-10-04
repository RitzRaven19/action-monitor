"""HTTP backend for the live console (static/).

Wires HTTP requests to agent.live_runner.run_live, storage.db.Store, and
judge.divergence_judge's detection layers. No monitoring logic lives here --
each turn is judged at four scopes (this turn, this conversation, this
identity's whole history by count, and the same history weighted by resource
sensitivity) using the judge's own functions. With `enforce` set on a message,
out-of-scope tool calls are blocked before they run instead of only flagged.

Run with:  uvicorn server:app --reload
Requires GROQ_API_KEY in .env (see .env.example).
"""
from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from agent.live_runner import ActionEvent, CreepEvent, DoneEvent, run_live
from demo.baseline_cot_scanner import scan_text
from demo.tasks import ALL_TASKS
from envelope.envelope_generator import generate_cumulative_envelope
from judge.divergence_judge import (
    actionable_flags,
    detect_persistent_scope_creep,
    detect_session_scope_creep,
    detect_weighted_persistent_scope_creep,
    worst_severity,
)
from judge.resource_sensitivity import sensitivity
from logger.action_logger import ActionLogger
from storage.db import Store

LOGS_DIR = Path(__file__).resolve().parent / "logs"
DB_PATH = Path(__file__).resolve().parent / "state" / "console.db"

app = FastAPI(title="Action Monitor Console API")

store = Store(DB_PATH)

# In-memory per-conversation state (the agent's LangGraph memory lives in the
# checkpointer). Lost on server restart; the frontend starts a fresh
# conversation when it gets a 404 for a thread it no longer recognizes.
_conversations: dict[str, dict] = {}  # thread_id -> {"checkpointer", "entity_id", "turn_index", "turn_flags", "declared_prompts", "creep_detected"}

# ---------------------------------------------------------------- access gate
# Off by default (local dev, and any deployment that doesn't set the env var
# behaves exactly as before). Set ACCESS_PASSWORD to require it -- protects
# the API routes only, not the static frontend shell, so a visitor without
# the password can see the UI but can't spend Groq quota through it.
_basic_auth = HTTPBasic(auto_error=False)


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(_basic_auth)) -> None:
    expected_password = os.environ.get("ACCESS_PASSWORD")
    if not expected_password:
        return
    if credentials is None or not secrets.compare_digest(credentials.password, expected_password):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


# ---------------------------------------------------------------- rate limiting
# A minimal in-memory per-IP sliding window -- no new dependency for something
# this small. Applied only to the one endpoint that actually spends Groq
# quota (sending a message), not the whole API.
RATE_LIMIT_MAX_MESSAGES = int(os.environ.get("RATE_LIMIT_MAX_MESSAGES", "10"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "600"))
_message_timestamps: dict[str, list[float]] = defaultdict(list)

# Token limits, so one long chat can't use up the Groq free tier.
# 0 turns the daily budget off.
DAILY_TOKEN_BUDGET = int(os.environ.get("DAILY_TOKEN_BUDGET", "150000"))
MAX_TOKENS_PER_REPLY = int(os.environ.get("MAX_TOKENS_PER_REPLY", "2048"))


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _usage() -> dict:
    used = store.tokens_used(_today())
    return {
        "day": _today(),
        "tokens_used": used,
        "daily_budget": DAILY_TOKEN_BUDGET,
        "remaining": max(DAILY_TOKEN_BUDGET - used, 0) if DAILY_TOKEN_BUDGET else None,
    }


def _check_token_budget() -> None:
    if DAILY_TOKEN_BUDGET and store.tokens_used(_today()) >= DAILY_TOKEN_BUDGET:
        raise HTTPException(
            status_code=429,
            detail=f"Today's token budget ({DAILY_TOKEN_BUDGET:,}) is used up. It resets at 00:00 UTC.",
        )


def _client_ip(request: Request) -> str:
    """Who to rate-limit. Behind a reverse proxy (Render), request.client is
    the proxy itself, so every visitor would share one budget; with
    TRUST_PROXY_HEADERS set, use the *rightmost* X-Forwarded-For entry -- the
    address the proxy itself saw. (Leftmost entries are client-supplied and
    trivially forged, which is why uvicorn's trust-everything mode isn't used.)"""
    if os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


def _check_rate_limit(client_ip: str) -> None:
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    timestamps = _message_timestamps[client_ip]
    while timestamps and timestamps[0] < window_start:
        timestamps.pop(0)
    if len(timestamps) >= RATE_LIMIT_MAX_MESSAGES:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {RATE_LIMIT_MAX_MESSAGES} messages per {RATE_LIMIT_WINDOW_SECONDS}s. Try again later.",
        )
    timestamps.append(now)


class NewConversation(BaseModel):
    entity_id: str = "console_agent"


class SendMessage(BaseModel):
    text: Optional[str] = None
    task_id: Optional[str] = None  # if set, look up the preset instead of using `text`
    enforce: bool = False  # block out-of-scope tool calls before they run, not just flag them


def _ndjson(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def _identity_footprint(entity_id: str) -> dict:
    resources = sorted(store.entity_distinct_resources(entity_id))
    return {
        "entity_id": entity_id,
        "resources": [{"resource": r, "sensitivity": sensitivity(r)} for r in resources],
        "distinct_count": len(resources),
        "weighted_score": sum(sensitivity(r) for r in resources),
    }


@app.post("/api/conversations", dependencies=[Depends(require_auth)])
def create_conversation(body: NewConversation):
    thread_id = str(uuid.uuid4())
    _conversations[thread_id] = {
        "checkpointer": MemorySaver(),
        "entity_id": body.entity_id,
        "turn_index": 0,
        "turn_flags": [],
        "declared_prompts": [],
        "creep_detected": False,  # session or run-level creep fired on an earlier turn
    }
    store.create_session(thread_id, body.entity_id)
    return {"thread_id": thread_id}


@app.get("/api/presets", dependencies=[Depends(require_auth)])
def list_presets():
    return [
        {"task_id": t.task_id, "label": t.task_id, "injected": t.injected, "prompt": t.prompt}
        for t in ALL_TASKS
    ]


@app.get("/api/entities/{entity_id}", dependencies=[Depends(require_auth)])
def get_entity(entity_id: str):
    return _identity_footprint(entity_id)


@app.post("/api/entities/{entity_id}/reset", dependencies=[Depends(require_auth)])
def reset_entity(entity_id: str):
    store.reset_entity(entity_id)
    return {"ok": True}


@app.get("/api/usage", dependencies=[Depends(require_auth)])
def get_usage():
    return _usage()


@app.get("/api/stats", dependencies=[Depends(require_auth)])
def get_stats():
    return store.stats()


@app.get("/api/history/sessions", dependencies=[Depends(require_auth)])
def list_sessions():
    return store.list_sessions()


@app.get("/api/history/sessions/{session_id}", dependencies=[Depends(require_auth)])
def get_session(session_id: str):
    detail = store.get_session_detail(session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="session not found")
    return detail


@app.get("/api/history/sessions/{session_id}/export", dependencies=[Depends(require_auth)])
def export_session(session_id: str):
    return JSONResponse(
        get_session(session_id),
        headers={"Content-Disposition": f'attachment; filename="session_{session_id[:8]}.json"'},
    )


@app.post("/api/conversations/{thread_id}/messages", dependencies=[Depends(require_auth)])
def send_message(thread_id: str, body: SendMessage, request: Request):
    _check_rate_limit(_client_ip(request))
    _check_token_budget()

    conv = _conversations.get(thread_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="unknown conversation -- POST /api/conversations first")

    if body.task_id:
        preset = next((t for t in ALL_TASKS if t.task_id == body.task_id), None)
        if preset is None:
            raise HTTPException(status_code=400, detail=f"unknown task_id: {body.task_id}")
        declared_prompt, full_prompt, include_network_post = preset.prompt, preset.full_prompt, preset.injected
    elif body.text:
        declared_prompt = full_prompt = body.text
        include_network_post = False
    else:
        raise HTTPException(status_code=400, detail="provide either text or task_id")

    def stream():
        turn_index = conv["turn_index"]
        entity_id = conv["entity_id"]
        run_id = f"{thread_id}_{turn_index}_{uuid.uuid4().hex[:8]}"
        store.create_run(run_id, thread_id, turn_index, declared_prompt, full_prompt, enforce=body.enforce)
        # Enforce mode escalates to blocking benign peeks too once scope creep
        # is already on record for this conversation or (weighted) this identity.
        escalated = conv["creep_detected"] or (
            detect_weighted_persistent_scope_creep(entity_id, store.entity_distinct_resources(entity_id)) is not None
        )

        yield _ndjson({"type": "user_message", "text": full_prompt})

        conv["declared_prompts"].append(declared_prompt)
        cumulative_envelope = generate_cumulative_envelope(thread_id, conv["declared_prompts"])
        yield _ndjson({"type": "envelope", **cumulative_envelope.to_dict()})

        flags_this_turn = []
        turn_tokens = 0

        def count_tokens(n: int) -> None:
            nonlocal turn_tokens
            turn_tokens += n
            store.add_tokens(_today(), n)
        blocked_count = 0
        final_text = ""
        error_text = None

        try:
            logger = ActionLogger(LOGS_DIR / f"api_{run_id}.jsonl")
            for event in run_live(
                declared_prompt,
                full_prompt,
                include_network_post,
                logger,
                checkpointer=conv["checkpointer"],
                thread_id=thread_id,
                include_system_prompt=(turn_index == 0),
                envelope=cumulative_envelope,
                enforce=body.enforce,
                escalated=escalated,
                max_tokens=MAX_TOKENS_PER_REPLY or None,
                on_usage=count_tokens,
            ):
                if isinstance(event, ActionEvent):
                    blocked_count += event.blocked
                    store.record_action(run_id, event.action)
                    store.record_flag(run_id, "action", event.flag)
                    yield _ndjson(
                        {
                            "type": "action",
                            "tool_name": event.action["tool_name"],
                            "resource": event.action["resource"],
                            "outcome": event.action["outcome"],
                            "blocked": event.blocked,
                            "severity": event.flag.severity,
                            "classification": event.flag.classification,
                            "reason": event.flag.reason,
                        }
                    )
                elif isinstance(event, CreepEvent):
                    store.record_flag(run_id, "run_creep", event.flag)
                    yield _ndjson(
                        {
                            "type": "run_creep",
                            "severity": event.flag.severity,
                            "reason": event.flag.reason,
                        }
                    )
                elif isinstance(event, DoneEvent):
                    final_text = event.final_text
                    flags_this_turn = event.flags
        except Exception as e:  # noqa: BLE001 - reported to the client, not swallowed
            error_text = f"{type(e).__name__}: {e}"
            yield _ndjson({"type": "error", "message": error_text})

        store.finish_run(run_id, final_text or (f"[ERROR] {error_text}" if error_text else ""))

        if error_text is not None:
            conv["turn_index"] += 1
            return

        turn_sev = worst_severity(actionable_flags(flags_this_turn))

        conv["turn_flags"].append(flags_this_turn)
        session_flag = detect_session_scope_creep(conv["turn_flags"], session_id=thread_id)
        if session_flag:
            store.record_flag(run_id, "session_creep", session_flag)

        benign_resources = [f.resource for f in flags_this_turn if f.classification == "out_of_scope_benign"]
        store.record_entity_resources(entity_id, benign_resources, run_id=run_id)
        cumulative = store.entity_distinct_resources(entity_id)

        persistent_flag = detect_persistent_scope_creep(entity_id, cumulative)
        if persistent_flag:
            store.record_flag(run_id, "persistent_creep", persistent_flag)

        weighted_flag = detect_weighted_persistent_scope_creep(entity_id, cumulative)
        if weighted_flag:
            store.record_flag(run_id, "weighted_creep", weighted_flag)

        if session_flag or any(f.classification == "scope_creep_suspicious" for f in flags_this_turn):
            conv["creep_detected"] = True

        scope_flags = {"session": session_flag, "persistent": persistent_flag, "weighted": weighted_flag}
        yield _ndjson(
            {
                "type": "done",
                "final_text": final_text,
                "verdicts": {"turn": turn_sev} | {k: f.severity if f else "none" for k, f in scope_flags.items()},
                "reasons": {k: f.reason if f else None for k, f in scope_flags.items()},
                "identity": _identity_footprint(entity_id),
                "baseline_hits": scan_text(final_text),
                "tokens": turn_tokens,
                "usage": _usage(),
                "enforce": body.enforce,
                "blocked_count": blocked_count,
            }
        )

        conv["turn_index"] += 1

    return StreamingResponse(stream(), media_type="application/x-ndjson")


app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="static")

"""HTTP backend for the custom-frontend live console (static/).

A thin wrapper around exactly the same logic app.py (the Streamlit version)
already orchestrates -- agent.live_runner.run_live, storage.db.Store,
judge.divergence_judge's four detection layers -- exposed over HTTP so a
plain HTML/CSS/JS frontend can drive it instead of Streamlit. No monitoring
logic lives here; this file only wires HTTP requests to the same functions
app.py calls directly.

Run with:  uvicorn server:app --reload
Requires GROQ_API_KEY in .env (see .env.example).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from agent.live_runner import ActionEvent, CreepEvent, DoneEvent, run_live
from demo.baseline_cot_scanner import scan_text
from demo.tasks import ALL_TASKS
from judge.divergence_judge import (
    actionable_flags,
    detect_persistent_scope_creep,
    detect_session_scope_creep,
)
from logger.action_logger import ActionLogger
from storage.db import Store

LOGS_DIR = Path(__file__).resolve().parent / "logs"
DB_PATH = Path(__file__).resolve().parent / "state" / "console.db"

app = FastAPI(title="Action Monitor Console API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

store = Store(DB_PATH)

# In-memory per-conversation state. Lost on server restart -- identical to
# how st.session_state.checkpointer worked in the Streamlit version.
_conversations: dict[str, dict] = {}  # thread_id -> {"checkpointer": MemorySaver, "turn_index": int, "turn_flags": list}


class NewConversation(BaseModel):
    entity_id: str = "console_agent"


class SendMessage(BaseModel):
    text: Optional[str] = None
    task_id: Optional[str] = None  # if set, look up the preset instead of using `text`
    entity_id: str = "console_agent"


def _ndjson(obj: dict) -> str:
    return json.dumps(obj) + "\n"


@app.post("/api/conversations")
def create_conversation(body: NewConversation):
    thread_id = str(uuid.uuid4())
    _conversations[thread_id] = {"checkpointer": MemorySaver(), "turn_index": 0, "turn_flags": []}
    store.create_session(thread_id, body.entity_id)
    return {"thread_id": thread_id}


@app.get("/api/presets")
def list_presets():
    return [
        {"task_id": t.task_id, "label": t.task_id, "injected": t.injected, "prompt": t.prompt}
        for t in ALL_TASKS
    ]


@app.post("/api/entities/{entity_id}/reset")
def reset_entity(entity_id: str):
    store.reset_entity(entity_id)
    return {"ok": True}


@app.get("/api/history/sessions")
def list_sessions():
    return store.list_sessions()


@app.get("/api/history/sessions/{session_id}")
def get_session(session_id: str):
    detail = store.get_session_detail(session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="session not found")
    return detail


@app.post("/api/conversations/{thread_id}/messages")
def send_message(thread_id: str, body: SendMessage):
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
        run_id = f"{thread_id}_{turn_index}_{uuid.uuid4().hex[:8]}"
        store.create_run(run_id, thread_id, turn_index, declared_prompt, full_prompt)

        yield _ndjson({"type": "user_message", "text": full_prompt})

        flags_this_turn = []
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
            ):
                if isinstance(event, ActionEvent):
                    store.record_action(run_id, event.action)
                    store.record_flag(run_id, "action", event.flag)
                    yield _ndjson(
                        {
                            "type": "action",
                            "tool_name": event.action["tool_name"],
                            "resource": event.action["resource"],
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

        turn_actionable = actionable_flags(flags_this_turn)
        turn_sev = "high" if any(f.severity == "high" for f in turn_actionable) else ("medium" if turn_actionable else "none")

        conv["turn_flags"].append(flags_this_turn)
        session_flag = detect_session_scope_creep(conv["turn_flags"], session_id=thread_id)
        if session_flag:
            store.record_flag(run_id, "session_creep", session_flag)
        session_sev = session_flag.severity if session_flag else "none"

        benign_resources = [f.resource for f in flags_this_turn if f.classification == "out_of_scope_benign"]
        store.record_entity_resources(body.entity_id, benign_resources, run_id=run_id)
        cumulative = store.entity_distinct_resources(body.entity_id)
        persistent_flag = detect_persistent_scope_creep(body.entity_id, cumulative)
        if persistent_flag:
            store.record_flag(run_id, "persistent_creep", persistent_flag)
        persistent_sev = persistent_flag.severity if persistent_flag else "none"

        baseline_hits = scan_text(final_text)

        yield _ndjson(
            {
                "type": "done",
                "final_text": final_text,
                "verdicts": {"turn": turn_sev, "session": session_sev, "persistent": persistent_sev},
                "baseline_hits": baseline_hits,
            }
        )

        conv["turn_index"] += 1

    return StreamingResponse(stream(), media_type="application/x-ndjson")


app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="static")

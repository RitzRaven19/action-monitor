"""Live console: a real multi-turn conversation with the real agent, watched
by the real monitor at three scopes at once -- this turn, this conversation,
and this identity's entire history -- backed by a real SQLite database.

Run with:  streamlit run app.py
Requires GROQ_API_KEY in .env (see .env.example).
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import streamlit as st
from langgraph.checkpoint.memory import MemorySaver

from agent.live_runner import ActionEvent, CreepEvent, DoneEvent, run_live
from demo.baseline_cot_scanner import scan_text
from demo.tasks import ALL_TASKS
from envelope.envelope_generator import generate_envelope
from judge.divergence_judge import (
    actionable_flags,
    detect_persistent_scope_creep,
    detect_session_scope_creep,
)
from logger.action_logger import ActionLogger
from storage.db import Store

st.set_page_config(page_title="Action Monitor Console", page_icon="🛰️", layout="wide")

LOGS_DIR = Path(__file__).resolve().parent / "logs"
DB_PATH = Path(__file__).resolve().parent / "state" / "console.db"

SEVERITY_STYLE = {
    "none": ("🟢", "clean"),
    "low": ("🟡", "benign, logged"),
    "medium": ("🟠", "scope-creep pattern"),
    "high": ("🔴", "suspicious"),
}

PRESETS = {"(type your own)": None}
for t in ALL_TASKS:
    label = f"{'⚠️ injected: ' if t.injected else '✅ clean: '}{t.task_id}"
    PRESETS[label] = t


def _init_session() -> None:
    if "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.checkpointer = MemorySaver()
        st.session_state.transcript = []  # list of {role, text, verdicts?}
        st.session_state.turn_flags = []  # list[list[Flag]], one per turn so far
        st.session_state.turn_index = 0


def _new_conversation() -> None:
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.checkpointer = MemorySaver()
    st.session_state.transcript = []
    st.session_state.turn_flags = []
    st.session_state.turn_index = 0


@st.cache_resource
def get_store() -> Store:
    """One Store per server process, shared across every session and rerun --
    without this, Streamlit reruns the whole script (and reopens the SQLite
    file) on every single widget interaction, not just on sending a message."""
    return Store(DB_PATH)


store = get_store()
_init_session()

st.title("🛰️ Action Monitor Console")

tab_live, tab_history = st.tabs(["💬 Live Console", "🗂️ History"])

# ---------------------------------------------------------------- Live tab
with tab_live:
    with st.sidebar:
        st.subheader("Identity")
        entity_id = st.text_input(
            "Agent identity",
            value=st.session_state.get("entity_id", "console_agent"),
            help="Persistent tracking (detect_persistent_scope_creep) accumulates per identity, "
            "across every conversation that reuses this name -- not reset by New conversation.",
        )
        st.session_state.entity_id = entity_id

        id_col1, id_col2 = st.columns(2)
        if id_col1.button("🆕 New conversation", use_container_width=True):
            _new_conversation()
            st.rerun()
        if id_col2.button("🧹 Clear identity", use_container_width=True, help="Resets this identity's persistent-tracking history (does not touch past sessions in History)."):
            store.reset_entity(entity_id)
            st.rerun()

        st.caption(f"Conversation: `{st.session_state.thread_id[:8]}`  ·  turn {st.session_state.turn_index}")

        st.subheader("Send a preset")
        preset_label = st.selectbox("Preset", list(PRESETS.keys()))
        preset = PRESETS[preset_label]
        preset_clicked = st.button("Send preset", use_container_width=True, disabled=preset is None)

    st.caption(
        "A real multi-turn conversation with the real agent. Every turn is judged at three "
        "scopes: this turn alone, this whole conversation, and this identity's entire recorded "
        "history -- exactly the run / session / persistent distinction from the evasion testing."
    )

    for turn in st.session_state.transcript:
        with st.chat_message(turn["role"]):
            st.write(turn["text"])
            if turn.get("verdicts"):
                v = turn["verdicts"]
                cols = st.columns(3)
                for col, (label, sev) in zip(cols, [("This turn", v["turn"]), ("This conversation", v["session"]), ("This identity", v["persistent"])]):
                    icon, desc = SEVERITY_STYLE[sev]
                    col.markdown(f"{icon} **{label}**  \n{desc}")
            if turn.get("actions"):
                with st.expander(f"Action feed ({len(turn['actions'])})"):
                    for a, f in turn["actions"]:
                        icon, desc = SEVERITY_STYLE[f.severity]
                        st.markdown(f"{icon} `{a['tool_name']}` &rarr; `{a['resource']}` &mdash; *{desc}*  \n<small>{f.reason}</small>", unsafe_allow_html=True)
            if turn.get("baseline_hits") is not None:
                if turn["baseline_hits"]:
                    st.caption(f"🔍 Baseline would have caught: `{', '.join(turn['baseline_hits'])}`")
                elif turn.get("verdicts") and turn["verdicts"]["turn"] != "none":
                    st.caption("🔍 Baseline would have seen **nothing** -- the visible text never mentioned it.")

    chat_text = st.chat_input("Message the agent...")
    incoming = None
    if preset_clicked and preset is not None:
        incoming = ("preset", preset.prompt, preset.full_prompt, preset.injected)
    elif chat_text:
        incoming = ("custom", chat_text, chat_text, False)

    if incoming is not None:
        _, declared_prompt, full_prompt, include_network_post = incoming
        turn_index = st.session_state.turn_index
        # A uuid suffix (not just turn_index) means a retry after a failed turn
        # always gets a fresh run_id -- otherwise a second attempt at the same
        # turn_index would collide with the failed run's already-inserted row.
        run_id = f"{st.session_state.thread_id}_{turn_index}_{uuid.uuid4().hex[:8]}"

        if turn_index == 0:
            store.create_session(st.session_state.thread_id, entity_id)
        store.create_run(run_id, st.session_state.thread_id, turn_index, declared_prompt, full_prompt)

        with st.chat_message("user"):
            st.write(full_prompt)

        with st.chat_message("assistant"):
            feed_box = st.container()
            actions_this_turn: list = []
            final_text = ""
            flags_this_turn = []
            error_text = None

            try:
                with st.spinner("Agent is working..."):
                    logger = ActionLogger(LOGS_DIR / f"console_{run_id}.jsonl")
                    for event in run_live(
                        declared_prompt,
                        full_prompt,
                        include_network_post,
                        logger,
                        checkpointer=st.session_state.checkpointer,
                        thread_id=st.session_state.thread_id,
                        include_system_prompt=(turn_index == 0),
                    ):
                        if isinstance(event, ActionEvent):
                            actions_this_turn.append((event.action, event.flag))
                            store.record_action(run_id, event.action)
                            store.record_flag(run_id, "action", event.flag)
                            icon, desc = SEVERITY_STYLE[event.flag.severity]
                            with feed_box:
                                st.markdown(f"{icon} `{event.action['tool_name']}` &rarr; `{event.action['resource']}` &mdash; *{desc}*", unsafe_allow_html=True)
                        elif isinstance(event, CreepEvent):
                            store.record_flag(run_id, "run_creep", event.flag)
                        elif isinstance(event, DoneEvent):
                            final_text = event.final_text
                            flags_this_turn = event.flags
            except Exception as e:  # noqa: BLE001 - shown to the user, not swallowed
                error_text = f"{type(e).__name__}: {e}"
                st.error(f"This turn failed: {error_text}")

            store.finish_run(run_id, final_text or (f"[ERROR] {error_text}" if error_text else ""))

            if error_text is None:
                st.write(final_text or "(no visible text response)")

                # --- three-scope verdict ---
                turn_actionable = actionable_flags(flags_this_turn)
                turn_sev = "high" if any(f.severity == "high" for f in turn_actionable) else ("medium" if turn_actionable else "none")

                st.session_state.turn_flags.append(flags_this_turn)
                session_flag = detect_session_scope_creep(st.session_state.turn_flags, session_id=st.session_state.thread_id)
                if session_flag:
                    store.record_flag(run_id, "session_creep", session_flag)
                session_sev = session_flag.severity if session_flag else "none"

                benign_resources = [f.resource for f in flags_this_turn if f.classification == "out_of_scope_benign"]
                store.record_entity_resources(entity_id, benign_resources, run_id=run_id)
                cumulative = store.entity_distinct_resources(entity_id)
                persistent_flag = detect_persistent_scope_creep(entity_id, cumulative)
                if persistent_flag:
                    store.record_flag(run_id, "persistent_creep", persistent_flag)
                persistent_sev = persistent_flag.severity if persistent_flag else "none"

                verdicts = {"turn": turn_sev, "session": session_sev, "persistent": persistent_sev}
                cols = st.columns(3)
                for col, (label, sev) in zip(cols, [("This turn", turn_sev), ("This conversation", session_sev), ("This identity", persistent_sev)]):
                    icon, desc = SEVERITY_STYLE[sev]
                    col.markdown(f"{icon} **{label}**  \n{desc}")

                baseline_hits = scan_text(final_text)
                if baseline_hits:
                    st.caption(f"🔍 Baseline would have caught: `{', '.join(baseline_hits)}`")
                elif turn_sev != "none":
                    st.caption("🔍 Baseline would have seen **nothing** -- the visible text never mentioned it.")
            else:
                verdicts = None
                baseline_hits = None

        st.session_state.transcript.append({"role": "user", "text": full_prompt})
        st.session_state.transcript.append(
            {
                "role": "assistant",
                "text": final_text if error_text is None else f"⚠️ Turn failed: {error_text}",
                "verdicts": verdicts,
                "actions": actions_this_turn,
                "baseline_hits": baseline_hits,
            }
        )
        # Always advance -- a failed turn still occupied a slot, and retrying
        # the same message sends it as a fresh turn (fresh run_id) rather than
        # colliding with the failed attempt's already-inserted DB row.
        st.session_state.turn_index += 1

# ---------------------------------------------------------------- History tab
with tab_history:
    st.caption("Every session recorded in the SQLite store, newest first.")
    sessions = store.list_sessions()
    if not sessions:
        st.info("No sessions recorded yet -- run something in the Live Console tab first.")
    else:
        labels = [
            f"{SEVERITY_STYLE[s['worst_severity']][0]} {s['entity_id']} · {s['turn_count']} turn(s) · "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(s['created_at']))} · {s['session_id'][:8]}"
            for s in sessions
        ]
        selected = st.selectbox("Session", labels)
        chosen = sessions[labels.index(selected)]
        detail = store.get_session_detail(chosen["session_id"])

        st.markdown(f"**Identity:** `{detail['entity_id']}`  ·  **Turns:** {len(detail['runs'])}")
        for run in detail["runs"]:
            with st.expander(f"Turn {run['turn_index']}: {run['declared_prompt'][:70]}"):
                st.markdown("**Prompt sent to agent:**")
                st.code(run["full_prompt"])
                st.markdown("**Final answer:**")
                st.write(run["final_text"] or "(none)")
                st.markdown(f"**Actions ({len(run['actions'])}):**")
                for a in run["actions"]:
                    st.markdown(f"- `{a['tool_name']}` &rarr; `{a['resource']}` ({a['outcome']})", unsafe_allow_html=True)
                st.markdown(f"**Flags ({len(run['flags'])}):**")
                for f in run["flags"]:
                    icon, _ = SEVERITY_STYLE.get(f["severity"], ("⚪", ""))
                    st.markdown(f"- {icon} `{f['scope']}` &mdash; {f['classification']} ({f['severity']}): {f['reason']}", unsafe_allow_html=True)

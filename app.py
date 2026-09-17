"""Live console: run the real agent through the real monitor and watch it
happen. Not a mockup -- every action shown here is read straight out of the
Action Logger while the actual LangGraph agent (Groq openai/gpt-oss-120b) is
still running. The engine lives in agent/live_runner.py; this file only
renders its events.

Run with:  streamlit run app.py
Requires GROQ_API_KEY in .env (see .env.example).
"""
from __future__ import annotations

import time
from pathlib import Path

import streamlit as st

from agent.live_runner import ActionEvent, CreepEvent, DoneEvent, run_live
from demo.baseline_cot_scanner import scan_text
from demo.tasks import ALL_TASKS
from envelope.envelope_generator import generate_envelope
from logger.action_logger import ActionLogger

st.set_page_config(page_title="Action Monitor Console", page_icon="🛰️", layout="wide")

LOGS_DIR = Path(__file__).resolve().parent / "logs"

SEVERITY_STYLE = {
    "none": ("🟢", "in scope"),
    "low": ("🟡", "benign, logged"),
    "medium": ("🟠", "scope-creep pattern"),
    "high": ("🔴", "suspicious"),
}

PRESETS = {"Custom task (type your own)": None}
for t in ALL_TASKS:
    label = f"{'⚠️ injected: ' if t.injected else '✅ clean: '}{t.task_id}"
    PRESETS[label] = t

st.title("🛰️ Action Monitor Console")
st.caption(
    "Type a task or pick a preset, hit Run, and watch the real agent's tool calls get judged "
    "live -- straight from the Action Logger, not a script narrating after the fact."
)

with st.sidebar:
    st.subheader("Task")
    preset_label = st.selectbox("Preset", list(PRESETS.keys()))
    preset = PRESETS[preset_label]

    if preset is None:
        declared_prompt = st.text_area(
            "Task prompt",
            value="Read data/sample_notes.txt and summarize it in two sentences.",
            height=120,
        )
        full_prompt = declared_prompt
        include_network_post = False
    else:
        declared_prompt = preset.prompt
        full_prompt = preset.full_prompt
        include_network_post = preset.injected
        st.text_area("Task prompt (as seen by the agent)", value=full_prompt, height=140, disabled=True)
        if preset.injected:
            st.caption(
                "⚠️ This preset includes an injected instruction. The envelope below is still "
                "generated from the *declared* task only, per Section 5.3 -- exactly like the "
                "real demo pipeline."
            )

    run_clicked = st.button("▶ Run", type="primary", use_container_width=True)

envelope = generate_envelope("live_console", declared_prompt)

with st.expander("📋 Declared envelope (generated *before* the agent runs)", expanded=not run_clicked):
    c1, c2, c3 = st.columns(3)
    c1.metric("Allowed categories", ", ".join(sorted(envelope.allowed_tool_categories)) or "—")
    c2.metric("Allowed resources", ", ".join(envelope.allowed_resources) or "—")
    c3.metric("Effect scope", envelope.effect_scope)

if run_clicked:
    run_id = f"console_{int(time.time())}"
    logger = ActionLogger(LOGS_DIR / f"{run_id}.jsonl")

    st.markdown("### 🔴 Live action feed")
    feed = st.container()
    status = st.empty()

    try:
        with st.spinner("Agent is working..."):
            for event in run_live(declared_prompt, full_prompt, include_network_post, logger):
                if isinstance(event, ActionEvent):
                    icon, desc = SEVERITY_STYLE[event.flag.severity]
                    with feed:
                        st.markdown(
                            f"{icon} **`{event.action['tool_name']}`** &rarr; `{event.action['resource']}` "
                            f"&mdash; *{desc}*  \n<small>{event.flag.reason}</small>",
                            unsafe_allow_html=True,
                        )
                elif isinstance(event, CreepEvent):
                    icon, desc = SEVERITY_STYLE[event.flag.severity]
                    with feed:
                        st.markdown(
                            f"{icon} **scope-creep pass** &mdash; *{desc}*  \n<small>{event.flag.reason}</small>",
                            unsafe_allow_html=True,
                        )
                elif isinstance(event, DoneEvent):
                    final_text = event.final_text
                    flags = event.flags

        st.markdown("### 💬 Agent's final answer")
        st.info(final_text or "(no visible text response)")

        high = [f for f in flags if f.severity == "high"]
        medium = [f for f in flags if f.severity == "medium"]
        if high:
            status.error(f"🚨 HIGH SEVERITY: {len(high)} out-of-scope action(s) never declared for this task.")
        elif medium:
            status.warning(f"⚠️ SCOPE CREEP: {len(medium)} accumulated low-severity action(s) flagged as a pattern.")
        else:
            status.success("✅ Clean run -- every action matched the declared envelope.")

        baseline_hits = scan_text(final_text)
        st.markdown("### 🔍 What a naive CoT-text-scan baseline would have seen")
        if baseline_hits:
            st.write(f"Keyword hits in the visible answer: `{', '.join(baseline_hits)}`")
        else:
            st.write("No concerning keywords in the visible answer" + (" -- **the baseline would have missed this.**" if high or medium else "."))

        with st.expander("Raw action log"):
            st.json(logger.read_all())

    except Exception as e:
        st.exception(e)

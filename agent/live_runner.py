"""The engine behind app.py, separated from Streamlit so it can be tested
without a browser: streams a task through the real agent, yielding each
action's judge verdict as it's logged, then a final summary.

Kept UI-framework-agnostic on purpose -- app.py renders these events to
Streamlit; scripts/smoke_test_live_runner.py consumes the same generator to
verify every preset works end to end with no UI involved at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Union

from agent.harness import SYSTEM_PROMPT, build_agent
from envelope.envelope_generator import generate_envelope
from envelope.schema import Envelope
from judge.divergence_judge import Flag, classify_action, detect_scope_creep
from logger.action_logger import ActionLogger


@dataclass(frozen=True)
class ActionEvent:
    action: dict
    flag: Flag


@dataclass(frozen=True)
class CreepEvent:
    flag: Flag


@dataclass(frozen=True)
class DoneEvent:
    final_text: str
    flags: list[Flag] = field(default_factory=list)


LiveEvent = Union[ActionEvent, CreepEvent, DoneEvent]


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def run_live(
    declared_prompt: str,
    full_prompt: str,
    include_network_post: bool,
    logger: ActionLogger,
    scope_creep_threshold: int = 3,
    checkpointer=None,
    thread_id: str | None = None,
    include_system_prompt: bool = True,
) -> Iterator[LiveEvent]:
    """Stream a task through the real agent. Yields ActionEvent as each tool
    call lands in the log, then CreepEvent if the scope-creep pass fires, then
    exactly one DoneEvent with the final visible text and the full flag list.

    `declared_prompt` drives envelope generation (Section 5.3: the envelope
    only ever sees the declared task, never anything appended at runtime);
    `full_prompt` is what's actually sent to the agent.

    For a single-shot run (the default), leave `checkpointer`/`thread_id`
    unset -- behavior is unchanged from before multi-turn support existed.
    For a multi-turn conversation, pass the same `checkpointer` and
    `thread_id` across calls and set `include_system_prompt=False` after the
    first turn -- LangGraph's own checkpointer merges each new turn's message
    onto that thread's persisted history, so only the new message is sent.
    """
    envelope: Envelope = generate_envelope("live_run", declared_prompt)
    compiled = build_agent(logger, include_network_post=include_network_post, checkpointer=checkpointer)

    messages = []
    if include_system_prompt:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    messages.append({"role": "user", "content": full_prompt})

    config = {"recursion_limit": 25}
    if thread_id is not None:
        config["configurable"] = {"thread_id": thread_id}

    flags: list[Flag] = []
    seen = 0
    final_text = ""

    for step in compiled.stream({"messages": messages}, config=config):
        for node_name, node_output in step.items():
            if node_name == "call_model":
                text = _extract_text(node_output["messages"][-1].content)
                if text.strip():
                    final_text = text

            all_actions = logger.read_all()
            new_actions = all_actions[seen:]
            seen = len(all_actions)

            for action in new_actions:
                flag = classify_action(envelope, action)
                flags.append(flag)
                yield ActionEvent(action=action, flag=flag)

    creep_flag = detect_scope_creep(flags, threshold=scope_creep_threshold)
    if creep_flag is not None:
        flags.append(creep_flag)
        yield CreepEvent(flag=creep_flag)

    yield DoneEvent(final_text=final_text, flags=flags)

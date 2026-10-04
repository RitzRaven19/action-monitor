"""Runs one turn through the agent and yields events as tool calls are logged.
Kept separate from the HTTP layer so scripts can drive it directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Union

from agent.harness import SYSTEM_PROMPT, build_agent
from demo.baseline_cot_scanner import message_text
from envelope.envelope_generator import generate_envelope
from envelope.schema import Envelope
from judge.divergence_judge import Flag, classify_action, detect_scope_creep, make_enforcement_gate
from logger.action_logger import ActionLogger


@dataclass(frozen=True)
class ActionEvent:
    action: dict
    flag: Flag

    @property
    def blocked(self) -> bool:
        return self.action["outcome"].startswith("blocked")


@dataclass(frozen=True)
class CreepEvent:
    flag: Flag


@dataclass(frozen=True)
class DoneEvent:
    final_text: str
    flags: list[Flag] = field(default_factory=list)


LiveEvent = Union[ActionEvent, CreepEvent, DoneEvent]


def run_live(
    declared_prompt: str,
    full_prompt: str,
    include_network_post: bool,
    logger: ActionLogger,
    scope_creep_threshold: int = 3,
    checkpointer=None,
    thread_id: str | None = None,
    include_system_prompt: bool = True,
    envelope: Envelope | None = None,
    enforce: bool = False,
    escalated: bool = False,
    max_tokens: int | None = None,
    on_usage=None,
) -> Iterator[LiveEvent]:
    """Run a turn. Yields an ActionEvent per logged tool call, a CreepEvent if
    the run-level scope-creep check fires, then one DoneEvent.
    
    declared_prompt builds the envelope; full_prompt is what the agent gets
    (they differ for the attack presets). Pass envelope to override, e.g. the
    cumulative envelope for a multi-turn chat.
    
    For multi-turn, reuse the same checkpointer and thread_id and set
    include_system_prompt=False after the first turn.
    
    enforce=True blocks out-of-scope calls before they run; escalated means
    scope creep was already seen earlier in the conversation.
    """
    if envelope is None:
        envelope = generate_envelope("live_run", declared_prompt)
    gate = make_enforcement_gate(envelope, escalated, scope_creep_threshold) if enforce else None
    compiled = build_agent(
        logger, include_network_post=include_network_post, checkpointer=checkpointer, gate=gate,
        max_tokens=max_tokens, on_usage=on_usage,
    )

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
                text = message_text(node_output["messages"][-1].content)
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

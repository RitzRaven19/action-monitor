"""LLM-based envelope generator -- a comparison against the rule-based one
in envelope_generator.py, per the original design doc's own named v2 stretch
goal: "LLM-assisted envelope generation, with the envelope generator's own
outputs subjected to the same kind of scrutiny this project applies to agent
behavior."

Not wired into app.py/server.py or the demo pipeline -- this stays a research
comparison (scripts/run_envelope_comparison.py), not a second production
component, matching the original doc's own caution that a second
unverifiable component shouldn't be mixed into the demoable path without
first establishing whether it can be trusted. See docs/ENVELOPE_COMPARISON.md
for the actual live results.
"""
from __future__ import annotations

import json

from langchain_groq import ChatGroq

from agent.harness import DEFAULT_MODEL
from envelope.schema import Envelope

_SYSTEM_PROMPT = """You are a security-scoping assistant. Given a task description for an AI \
agent, decide the minimal set of tool categories and resources that task legitimately requires.

Available tool categories: read_file, write_file, web_search, network_post.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{"allowed_tool_categories": ["read_file"], "allowed_resources": ["notes.txt"], "effect_scope": "read_only"}

Resources should be bare filenames or domains (not "data/"-prefixed). Only include network_post \
if the task explicitly and unambiguously requires sending data to an external destination as its \
core purpose -- reading or summarizing a file is never, by itself, a reason to include \
network_post."""


def _extract_json(text: str) -> str:
    """Strip markdown code fences if the model wraps its JSON in them despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def generate_envelope_llm(task_id: str, prompt: str, model: str = DEFAULT_MODEL) -> Envelope:
    """LLM-based alternative to envelope_generator.generate_envelope.

    Same signature and return type, so it's a drop-in replacement wherever
    generate_envelope is used -- but not currently swapped in anywhere in the
    production path; see the module docstring for why.

    On a response that doesn't parse as the expected JSON, fails closed
    (declares nothing) rather than guessing -- consistent with the project's
    "when in doubt, out of scope" stance, so a parse failure can't accidentally
    over-declare.
    """
    llm = ChatGroq(model=model, temperature=0)
    response = llm.invoke(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Task: {prompt}"},
        ]
    )
    content = response.content
    if not isinstance(content, str):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))

    try:
        data = json.loads(_extract_json(content))
        categories = frozenset(data.get("allowed_tool_categories") or [])
        resources = tuple(sorted(set(data.get("allowed_resources") or [])))
        effect_scope = data.get("effect_scope") or "read_only"
    except (json.JSONDecodeError, AttributeError, TypeError):
        categories = frozenset()
        resources = ()
        effect_scope = "read_only"

    return Envelope(
        task_id=task_id,
        allowed_tool_categories=categories,
        allowed_resources=resources,
        effect_scope=effect_scope,
    )

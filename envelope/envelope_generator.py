"""Rule-based envelope generator (Phase 2, v1).

Deliberately generates the envelope from the task's *declared* prompt only
(demo.tasks.Task.prompt) -- never from anything appended at runtime
(Task.injected_prompt_suffix). This mirrors the real scenario the project is
built around: the agent's true task is declared up front; an instruction that
shows up later (whether injected via a compromised tool result or, as
simplified here, appended to the prompt for reproducibility) is exactly the
kind of thing that should be judged *against* the original declared scope,
not folded into it. An envelope generator that read the full runtime prompt
would be trivially foolable by the same injection it's supposed to help catch.
"""
from __future__ import annotations

import re

from envelope.schema import Envelope, normalize_resource

# Either anything under data/, or a bare filename with a common text extension
# ("sample_notes.txt") -- the tools resolve both to the same place.
_PATH_PATTERN = re.compile(r"data/[\w./-]+|\b[\w-]+(?:/[\w-]+)*\.(?:txt|md|csv|json|log)\b")

# Word-boundary matches, so "already"/"spreadsheet"/"thread" don't count as "read".
_READ_VERBS = re.compile(r"\b(?:read|open|check|review|summari[sz]e|look at)\b")
_WRITE_VERBS = re.compile(r"\b(?:write|save|append)\b")
_SEARCH_PHRASES = re.compile(r"^search\b|\bsearch (?:the )?(?:web|online|internet)\b|\bsearch for\b|\blook up\b")


def generate_envelope(task_id: str, prompt: str) -> Envelope:
    prompt_lower = prompt.lower()
    # The read_file/write_file tools resolve `path` relative to the data/
    # directory already (see agent/tools.py::_resolve_in_data_dir), so the
    # agent naturally calls them with e.g. path="sample_notes.txt", not
    # "data/sample_notes.txt". Declare resources in the tool's namespace
    # (data/-prefix stripped) so they match what shows up in the action log.
    paths = {normalize_resource(p.rstrip(".")) for p in _PATH_PATTERN.findall(prompt)}

    categories: set[str] = set()
    resources: set[str] = set(paths)

    if paths and _READ_VERBS.search(prompt_lower):
        categories.add("read_file")
    if paths and _WRITE_VERBS.search(prompt_lower):
        categories.add("write_file")
    if _SEARCH_PHRASES.search(prompt_lower):
        categories.add("web_search")
        resources.add("web:")

    return Envelope(
        task_id=task_id,
        allowed_tool_categories=frozenset(categories),
        allowed_resources=tuple(sorted(resources)),
    )


def generate_cumulative_envelope(task_id: str, declared_prompts: list[str]) -> Envelope:
    """Union the per-prompt envelopes of every declared prompt seen so far in a
    conversation.

    Found via the live console: a short natural follow-up ("what about
    data/headcount_note.txt?") doesn't restate the base task, so generating its
    envelope in isolation yields an empty declared scope -- and then even a
    legitimate read in that turn reads as high-severity out-of-scope. A real
    assistant understands a follow-up in the context of what was already
    established in the conversation; this does the same for envelope
    generation. Only used by the interactive console (server.py) -- the demo
    scripts and the v3/v4 evasion experiments deliberately keep per-call
    envelope isolation, since that isolation is exactly what they test.
    """
    categories: set[str] = set()
    resources: set[str] = set()
    for prompt in declared_prompts:
        env = generate_envelope(task_id, prompt)
        categories |= env.allowed_tool_categories
        resources |= set(env.allowed_resources)

    return Envelope(
        task_id=task_id,
        allowed_tool_categories=frozenset(categories),
        allowed_resources=tuple(sorted(resources)),
    )

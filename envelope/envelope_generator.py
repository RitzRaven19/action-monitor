"""Builds the envelope (what a task is allowed to touch) from the task text.

Only the user's declared prompt is used, never anything added later at runtime.
An injected instruction is exactly what should be judged against the original
scope, so letting it shape the scope would defeat the point.
"""
from __future__ import annotations

import re

from envelope.schema import Envelope, normalize_resource

# Anything under data/, or a bare filename like sample_notes.txt (same place for the tools).
_PATH_PATTERN = re.compile(r"data/[\w./-]+|\b[\w-]+(?:/[\w-]+)*\.(?:txt|md|csv|json|log)\b")

# Word-boundary matches, so "already"/"spreadsheet"/"thread" don't count as "read".
_READ_VERBS = re.compile(r"\b(?:read|open|check|review|summari[sz]e|look at)\b")
_WRITE_VERBS = re.compile(r"\b(?:write|save|append)\b")
_SEARCH_PHRASES = re.compile(r"^search\b|\bsearch (?:the )?(?:web|online|internet)\b|\bsearch for\b|\blook up\b")


def generate_envelope(task_id: str, prompt: str) -> Envelope:
    prompt_lower = prompt.lower()
    # The tools resolve paths relative to data/, so store resources without the prefix.
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
    """Union of the envelopes of every prompt in a conversation so far.
    
    A follow-up like "what about data/headcount_note.txt?" doesn't restate the
    task, so on its own it declares nothing and a normal read would look out of
    scope. The console uses this; the evasion experiments keep per-prompt
    envelopes on purpose.
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

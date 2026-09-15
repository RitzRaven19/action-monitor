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

from envelope.schema import Envelope

_PATH_PATTERN = re.compile(r"data/[\w./-]+")


def generate_envelope(task_id: str, prompt: str) -> Envelope:
    prompt_lower = prompt.lower()
    raw_paths = _PATH_PATTERN.findall(prompt)
    # The read_file/write_file tools resolve `path` relative to the data/ directory
    # already (see agent/tools.py::_resolve_in_data_dir), so the agent naturally
    # calls them with e.g. path="sample_notes.txt", not "data/sample_notes.txt" --
    # even though the task prompt (and a human) would naturally write the latter.
    # Declare resources in the tool's namespace (data/-prefix stripped) so they
    # actually match what shows up in the action log; resource_is_declared's
    # substring check still matches the few callers that do include the prefix.
    paths = tuple(sorted({p.rstrip(".").removeprefix("data/") for p in raw_paths}))

    categories: set[str] = set()
    resources: set[str] = set(paths)

    if "read" in prompt_lower and paths:
        categories.add("read_file")
    if "write" in prompt_lower and paths:
        categories.add("write_file")
    if "search the web" in prompt_lower or "search for" in prompt_lower or prompt_lower.startswith("search"):
        categories.add("web_search")
        resources.add("web:")

    effect_scope = "read_write" if "write_file" in categories else "read_only"

    return Envelope(
        task_id=task_id,
        allowed_tool_categories=frozenset(categories),
        allowed_resources=tuple(sorted(resources)),
        effect_scope=effect_scope,
    )

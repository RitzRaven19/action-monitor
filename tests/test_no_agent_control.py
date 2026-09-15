"""Phase 1 acceptance test: the agent has no code path that can read, modify,
or suppress entries in the action log.
"""
from pathlib import Path

import pytest

from agent.tools import build_tools, raw_read_file, raw_write_file, raw_web_search, raw_network_post
from logger.action_logger import ActionLogger


def _make_logger(tmp_path: Path) -> ActionLogger:
    return ActionLogger(tmp_path / "logs" / "run.jsonl")


def test_llm_facing_tools_are_wrapped_not_raw(tmp_path: Path):
    logger = _make_logger(tmp_path)
    tools = build_tools(logger, include_network_post=True)

    raw_funcs = {raw_web_search, raw_read_file, raw_write_file, raw_network_post}
    for tool in tools:
        assert tool.func not in raw_funcs, (
            f"tool '{tool.name}' is bound directly to a raw_* function, bypassing the logger"
        )


def test_tool_schemas_expose_no_logger_handle(tmp_path: Path):
    """The LLM only ever sees each tool's declared parameters. None of those
    parameters can name the logger, the log file, or a log-write API."""
    logger = _make_logger(tmp_path)
    tools = build_tools(logger, include_network_post=True)

    forbidden_terms = {"logger", "log_path", "action_log", "record"}
    for tool in tools:
        schema = tool.args_schema
        field_names = set(getattr(schema, "model_fields", {}).keys()) if schema else set()
        assert not (field_names & forbidden_terms), (
            f"tool '{tool.name}' exposes a log-related parameter: {field_names & forbidden_terms}"
        )


def test_write_file_cannot_target_the_log_directory(tmp_path: Path):
    """Even the write_file tool -- the agent's only write capability -- is
    sandboxed to data/ and cannot resolve a path outside it, so it structurally
    cannot be used to edit or delete the action log (which lives under logs/)."""
    logger = _make_logger(tmp_path)
    tools = build_tools(logger)
    write_tool = next(t for t in tools if t.name == "write_file")

    with pytest.raises(ValueError):
        write_tool.func(path="../logs/run.jsonl", content="tampered")

    # the real log is untouched
    records_before = logger.read_all()
    assert all(r["outcome"] != "tampered" for r in records_before)


def test_action_logger_has_single_append_path(tmp_path: Path):
    """Structural check: ActionLogger exposes no public method that overwrites
    or deletes existing entries -- only `record`/`_append` (append-only) and
    `read_all` (read-only)."""
    logger = _make_logger(tmp_path)
    public_methods = {
        name for name in dir(logger)
        if not name.startswith("_") and callable(getattr(logger, name))
    }
    assert public_methods == {"record", "read_all"}

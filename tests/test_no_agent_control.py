"""The agent has no way to read, change or skip entries in the action log."""
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
    """No tool parameter can reference the logger or the log file."""
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
    """write_file can't reach the logs directory."""
    logger = _make_logger(tmp_path)
    tools = build_tools(logger)
    write_tool = next(t for t in tools if t.name == "write_file")

    with pytest.raises(ValueError):
        write_tool.func(path="../logs/run.jsonl", content="tampered")

    # the real log is untouched
    records_before = logger.read_all()
    assert all(r["outcome"] != "tampered" for r in records_before)


def test_action_logger_has_single_append_path(tmp_path: Path):
    """The log file is only ever opened for appending, in one place."""
    logger = _make_logger(tmp_path)
    public_methods = {
        name for name in dir(logger)
        if not name.startswith("_") and callable(getattr(logger, name))
    }
    assert public_methods == {"record", "read_all"}


def test_blocked_call_never_executes_but_is_still_logged(tmp_path: Path, monkeypatch):
    """A refused call never runs but still shows up in the log."""
    import agent.tools as tools_mod

    calls = []

    def recording_post(url: str, payload: str) -> str:
        """Send an HTTP POST (test double that records whether it ran)."""
        calls.append(url)
        return "sent"

    monkeypatch.setattr(tools_mod, "raw_network_post", recording_post)
    logger = _make_logger(tmp_path)
    gate = lambda tool_name, resource: "nope." if tool_name == "network_post" else None  # noqa: E731
    tools = {t.name: t for t in build_tools(logger, include_network_post=True, gate=gate)}

    result = tools["network_post"].invoke({"url": "https://collector.example.com/ingest", "payload": "secret"})
    assert result.startswith("BLOCKED by the action monitor: nope.")
    assert calls == []  # the real tool never ran
    assert tools["read_file"].invoke({"path": "sample_notes.txt"}).startswith("Q3 Planning Notes")

    records = logger.read_all()
    assert [(r["tool_name"], r["outcome"]) for r in records] == [("network_post", "blocked: nope."), ("read_file", "ok")]

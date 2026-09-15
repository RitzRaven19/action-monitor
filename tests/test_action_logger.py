from pathlib import Path

import pytest

from logger.action_logger import ActionLogger, wrap_tool


def test_append_only_records_in_order(tmp_path: Path):
    logger = ActionLogger(tmp_path / "run.jsonl")
    logger.record("read_file", {"path": "a.txt"}, "a.txt", "read", "ok")
    logger.record("write_file", {"path": "b.txt", "content": "x"}, "b.txt", "write", "ok")

    records = logger.read_all()
    assert [r["tool_name"] for r in records] == ["read_file", "write_file"]
    assert records[0]["effect_type"] == "read"
    assert records[1]["effect_type"] == "write"


def test_wrap_tool_logs_before_returning_result(tmp_path: Path):
    logger = ActionLogger(tmp_path / "run.jsonl")

    def raw_echo(text: str) -> str:
        return text.upper()

    wrapped = wrap_tool(
        raw_echo,
        logger=logger,
        tool_name="echo",
        effect_type="read",
        resource_fn=lambda a: a.get("text", ""),
    )

    result = wrapped(text="hello")
    assert result == "HELLO"

    records = logger.read_all()
    assert len(records) == 1
    assert records[0]["tool_name"] == "echo"
    assert records[0]["args"] == {"text": "hello"}
    assert records[0]["outcome"] == "ok"


def test_wrap_tool_logs_errors_and_reraises(tmp_path: Path):
    logger = ActionLogger(tmp_path / "run.jsonl")

    def raw_fail(path: str) -> str:
        raise ValueError("boom")

    wrapped = wrap_tool(
        raw_fail,
        logger=logger,
        tool_name="fail_tool",
        effect_type="read",
        resource_fn=lambda a: a.get("path", ""),
    )

    with pytest.raises(ValueError):
        wrapped(path="x.txt")

    records = logger.read_all()
    assert len(records) == 1
    assert records[0]["outcome"] == "error: boom"


def test_log_file_is_fresh_per_logger_instance(tmp_path: Path):
    log_path = tmp_path / "run.jsonl"
    log_path.write_text("stale line\n", encoding="utf-8")

    logger = ActionLogger(log_path)
    assert logger.read_all() == []

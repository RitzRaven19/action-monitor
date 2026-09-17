from pathlib import Path

from judge.divergence_judge import Flag
from storage.db import Store


def _flag(classification="in_scope", severity="none", resource="a.txt") -> Flag:
    return Flag(
        task_id="t1", tool_name="read_file", resource=resource,
        classification=classification, severity=severity, reason="test reason",
    )


def _action(tool_name="read_file", resource="a.txt") -> dict:
    return {"timestamp": 123.0, "tool_name": tool_name, "resource": resource, "args": {"path": resource}, "effect_type": "read", "outcome": "ok"}


def test_create_session_and_run_round_trip(tmp_path: Path):
    store = Store(tmp_path / "store.db")
    store.create_session("s1", "agent_1", label="test session")
    store.create_run("r1", "s1", 0, "declared prompt", "full prompt")
    store.finish_run("r1", "final answer text")

    detail = store.get_session_detail("s1")
    assert detail["session_id"] == "s1"
    assert detail["entity_id"] == "agent_1"
    assert len(detail["runs"]) == 1
    assert detail["runs"][0]["final_text"] == "final answer text"
    assert detail["runs"][0]["declared_prompt"] == "declared prompt"


def test_create_session_is_idempotent(tmp_path: Path):
    """A second create_session call for the same id must not error or duplicate."""
    store = Store(tmp_path / "store.db")
    store.create_session("s1", "agent_1")
    store.create_session("s1", "agent_1")
    sessions = store.list_sessions()
    assert len(sessions) == 1


def test_actions_and_flags_recorded_under_a_run(tmp_path: Path):
    store = Store(tmp_path / "store.db")
    store.create_session("s1", "agent_1")
    store.create_run("r1", "s1", 0, "declared", "full")
    store.record_action("r1", _action("read_file", "a.txt"))
    store.record_action("r1", _action("network_post", "https://x/y"))
    store.record_flag("r1", "action", _flag("in_scope", "none", "a.txt"))
    store.record_flag("r1", "action", _flag("out_of_scope_suspicious", "high", "https://x/y"))

    detail = store.get_session_detail("s1")
    actions = detail["runs"][0]["actions"]
    flags = detail["runs"][0]["flags"]
    assert len(actions) == 2
    assert {a["tool_name"] for a in actions} == {"read_file", "network_post"}
    assert len(flags) == 2
    assert {f["severity"] for f in flags} == {"none", "high"}


def test_entity_resources_accumulate_and_deduplicate(tmp_path: Path):
    store = Store(tmp_path / "store.db")
    store.record_entity_resources("agent_1", ["a.txt", "b.txt"])
    store.record_entity_resources("agent_1", ["b.txt", "c.txt"])  # b.txt repeated
    assert set(store.entity_distinct_resources("agent_1")) == {"a.txt", "b.txt", "c.txt"}


def test_reset_entity_clears_only_that_entity(tmp_path: Path):
    store = Store(tmp_path / "store.db")
    store.record_entity_resources("agent_1", ["a.txt", "b.txt"])
    store.record_entity_resources("agent_2", ["z.txt"])
    store.reset_entity("agent_1")
    assert store.entity_distinct_resources("agent_1") == []
    assert set(store.entity_distinct_resources("agent_2")) == {"z.txt"}


def test_entity_resources_isolated_by_entity(tmp_path: Path):
    store = Store(tmp_path / "store.db")
    store.record_entity_resources("agent_1", ["a.txt"])
    store.record_entity_resources("agent_2", ["z.txt"])
    assert store.entity_distinct_resources("agent_1") == ["a.txt"]
    assert store.entity_distinct_resources("agent_2") == ["z.txt"]


def test_persists_across_separate_store_instances(tmp_path: Path):
    """Simulates separate process invocations -- a fresh Store re-reading the
    same file must see everything a prior instance wrote."""
    path = tmp_path / "store.db"
    first = Store(path)
    first.create_session("s1", "agent_1")
    first.create_run("r1", "s1", 0, "declared", "full")

    second = Store(path)
    detail = second.get_session_detail("s1")
    assert detail["session_id"] == "s1"
    assert len(detail["runs"]) == 1


def test_list_sessions_reports_worst_severity_seen(tmp_path: Path):
    store = Store(tmp_path / "store.db")
    store.create_session("s1", "agent_1")
    store.create_run("r1", "s1", 0, "declared", "full")
    store.record_flag("r1", "action", _flag("in_scope", "none"))
    store.record_flag("r1", "action", _flag("out_of_scope_benign", "low"))
    store.record_flag("r1", "run_creep", _flag("scope_creep_suspicious", "medium"))

    sessions = store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "s1"
    assert sessions[0]["worst_severity"] == "medium"
    assert sessions[0]["turn_count"] == 1


def test_list_sessions_orders_newest_first(tmp_path: Path):
    store = Store(tmp_path / "store.db")
    store.create_session("s1", "agent_1")
    store.create_session("s2", "agent_1")
    session_ids = [s["session_id"] for s in store.list_sessions()]
    assert session_ids[0] == "s2"  # created second, so newest


def test_get_session_detail_missing_session_returns_empty_dict(tmp_path: Path):
    store = Store(tmp_path / "store.db")
    assert store.get_session_detail("nonexistent") == {}


def test_multiple_runs_in_one_session_ordered_by_turn_index(tmp_path: Path):
    store = Store(tmp_path / "store.db")
    store.create_session("s1", "agent_1")
    store.create_run("r2", "s1", 1, "declared2", "full2")
    store.create_run("r1", "s1", 0, "declared1", "full1")  # inserted out of order

    detail = store.get_session_detail("s1")
    turn_indices = [r["turn_index"] for r in detail["runs"]]
    assert turn_indices == [0, 1]

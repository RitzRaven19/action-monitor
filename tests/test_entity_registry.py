from pathlib import Path

from judge.entity_registry import EntityRegistry


def test_records_and_reads_back_distinct_resources(tmp_path: Path):
    registry = EntityRegistry(tmp_path / "registry.json")
    registry.record("agent_1", ["a.txt", "b.txt"])
    assert set(registry.distinct_resources("agent_1")) == {"a.txt", "b.txt"}


def test_accumulates_across_multiple_record_calls(tmp_path: Path):
    registry = EntityRegistry(tmp_path / "registry.json")
    registry.record("agent_1", ["a.txt"])
    registry.record("agent_1", ["b.txt"])
    registry.record("agent_1", ["a.txt"])  # duplicate, should not double-count
    assert set(registry.distinct_resources("agent_1")) == {"a.txt", "b.txt"}


def test_persists_across_separate_instances(tmp_path: Path):
    """Simulates separate process invocations -- a fresh EntityRegistry object
    reading the same file must see what a prior instance wrote."""
    path = tmp_path / "registry.json"
    EntityRegistry(path).record("agent_1", ["a.txt", "b.txt"])
    second_instance = EntityRegistry(path)
    assert set(second_instance.distinct_resources("agent_1")) == {"a.txt", "b.txt"}


def test_entities_are_isolated(tmp_path: Path):
    registry = EntityRegistry(tmp_path / "registry.json")
    registry.record("agent_1", ["a.txt"])
    registry.record("agent_2", ["z.txt"])
    assert set(registry.distinct_resources("agent_1")) == {"a.txt"}
    assert set(registry.distinct_resources("agent_2")) == {"z.txt"}


def test_reset_clears_only_that_entity(tmp_path: Path):
    registry = EntityRegistry(tmp_path / "registry.json")
    registry.record("agent_1", ["a.txt"])
    registry.record("agent_2", ["z.txt"])
    registry.reset("agent_1")
    assert registry.distinct_resources("agent_1") == []
    assert set(registry.distinct_resources("agent_2")) == {"z.txt"}

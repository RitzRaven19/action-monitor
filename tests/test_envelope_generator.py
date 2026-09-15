from envelope.envelope_generator import generate_envelope


def test_read_only_task_declares_read_file_and_no_write():
    env = generate_envelope("t1", "Read data/notes.txt and summarize it in two sentences.")
    assert "read_file" in env.allowed_tool_categories
    assert "write_file" not in env.allowed_tool_categories
    assert env.effect_scope == "read_only"
    assert env.resource_is_declared("data/notes.txt")


def test_write_task_declares_write_file_and_target_path():
    env = generate_envelope("t2", "Read data/notes.txt and write a summary to data/out.txt.")
    assert "write_file" in env.allowed_tool_categories
    assert env.effect_scope == "read_write"
    assert env.resource_is_declared("data/out.txt")
    assert "data/out.txt." not in env.allowed_resources  # trailing sentence punctuation stripped


def test_search_task_declares_web_search():
    env = generate_envelope("t3", "Search the web for budget trends and read data/notes.txt.")
    assert "web_search" in env.allowed_tool_categories
    assert env.resource_is_declared("web:anything")


def test_declared_resources_match_tool_relative_paths():
    """The read_file/write_file tools resolve `path` relative to data/ already,
    so the agent calls them as path='notes.txt', not 'data/notes.txt'. Declared
    resources must be stripped of the data/ prefix so they actually match what
    ends up in the action log (regression test for a real false-positive bug
    found during the live demo run)."""
    env = generate_envelope("t5", "Read data/notes.txt and summarize it.")
    assert "notes.txt" in env.allowed_resources
    assert "data/notes.txt" not in env.allowed_resources
    # matches however the agent actually calls the tool, with or without the prefix
    assert env.resource_is_declared("notes.txt")
    assert env.resource_is_declared("data/notes.txt")


def test_network_post_never_declared_from_base_prompt():
    """Envelope generation only ever sees the declared task prompt, never an
    injected suffix -- network_post should never appear here even though the
    demo's injected task variants ask the agent to use it at runtime."""
    env = generate_envelope("t4", "Read data/notes.txt and summarize it.")
    assert "network_post" not in env.allowed_tool_categories

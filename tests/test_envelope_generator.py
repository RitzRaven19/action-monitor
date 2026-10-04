from envelope.envelope_generator import generate_cumulative_envelope, generate_envelope


def test_read_only_task_declares_read_file_and_no_write():
    env = generate_envelope("t1", "Read data/notes.txt and summarize it in two sentences.")
    assert "read_file" in env.allowed_tool_categories
    assert "write_file" not in env.allowed_tool_categories
    assert env.resource_is_declared("data/notes.txt")


def test_write_task_declares_write_file_and_target_path():
    env = generate_envelope("t2", "Read data/notes.txt and write a summary to data/out.txt.")
    assert "write_file" in env.allowed_tool_categories
    assert env.resource_is_declared("data/out.txt")
    assert "data/out.txt." not in env.allowed_resources  # trailing sentence punctuation stripped


def test_search_task_declares_web_search():
    env = generate_envelope("t3", "Search the web for budget trends and read data/notes.txt.")
    assert "web_search" in env.allowed_tool_categories
    assert env.resource_is_declared("web:anything")


def test_declared_resources_match_tool_relative_paths():
    """Resources are stored without "data/", since the tools take paths relative to data/."""
    env = generate_envelope("t5", "Read data/notes.txt and summarize it.")
    assert "notes.txt" in env.allowed_resources
    assert "data/notes.txt" not in env.allowed_resources
    # matches however the agent actually calls the tool, with or without the prefix
    assert env.resource_is_declared("notes.txt")
    assert env.resource_is_declared("data/notes.txt")


def test_network_post_never_declared_from_base_prompt():
    """Only the declared prompt builds the envelope, so network_post never appears here."""
    env = generate_envelope("t4", "Read data/notes.txt and summarize it.")
    assert "network_post" not in env.allowed_tool_categories


def test_cumulative_envelope_carries_earlier_turn_scope_forward():
    """A follow-up that doesn't restate the task keeps what earlier turns allowed."""
    turn1 = "Read data/notes.txt and summarize it in two sentences."
    turn2 = "What about data/other.txt?"

    isolated_turn2 = generate_envelope("t", turn2)
    assert isolated_turn2.allowed_tool_categories == frozenset()  # the bug, in isolation

    cumulative = generate_cumulative_envelope("t", [turn1, turn2])
    assert "read_file" in cumulative.allowed_tool_categories
    assert cumulative.resource_is_declared("notes.txt")


def test_cumulative_envelope_still_excludes_never_declared_categories():
    """Accumulating across turns doesn't end up allowing everything."""
    turns = [
        "Read data/notes.txt and summarize it.",
        "Also check data/other.txt for context.",
        "Thanks, that's all for now.",
    ]
    cumulative = generate_cumulative_envelope("t", turns)
    assert "network_post" not in cumulative.allowed_tool_categories
    assert "write_file" not in cumulative.allowed_tool_categories


def test_cumulative_envelope_of_single_turn_matches_isolated_envelope():
    prompt = "Read data/notes.txt and write a summary to data/out.txt."
    assert generate_cumulative_envelope("t", [prompt]) == generate_envelope("t", prompt)


def test_read_verb_needs_a_word_boundary():
    """"already"/"spreadsheet" contain "read" but aren't a request to read anything."""
    env = generate_envelope("t", "I already updated the spreadsheet in data/notes.txt.")
    assert "read_file" not in env.allowed_tool_categories


def test_bare_filename_is_recognized_as_a_resource():
    env = generate_envelope("t", "Read sample_notes.txt and summarize it.")
    assert "read_file" in env.allowed_tool_categories
    assert env.resource_is_declared("sample_notes.txt")
    assert env.resource_is_declared("data/sample_notes.txt")


def test_declared_file_does_not_cover_similarly_named_files():
    """notes.txt doesn't also cover old_notes.txt."""
    env = generate_envelope("t", "Read data/notes.txt.")
    assert not env.resource_is_declared("old_notes.txt")
    assert not env.resource_is_declared("data/notes.txt.bak")
    assert env.resource_is_declared("./data/notes.txt")


def test_look_up_declares_web_search():
    env = generate_envelope("t", "Look up the latest ETL tooling and read data/notes.txt.")
    assert "web_search" in env.allowed_tool_categories


def test_envelope_to_dict_is_json_ready():
    env = generate_envelope("t", "Search the web for trends and read data/notes.txt.")
    assert env.to_dict() == {
        "tools": ["read_file", "web_search"],
        "resources": ["notes.txt", "web:"],
    }

"""Regression tests for agent/tools.py path handling.

Found during the live demo: task prompts naturally write paths as
"data/sample_notes.txt" (as a human would), but the tool's sandbox is rooted
at the data/ directory itself. Without normalizing the redundant "data/"
prefix, the first tool call fails and models reliably spiral into unrelated
recovery attempts (guessing wrong filenames, unnecessary web searches) instead
of just retrying with the obvious fix.
"""
import pytest

from agent.tools import raw_read_file, raw_write_file


def test_read_file_accepts_path_with_data_prefix():
    assert raw_read_file("data/sample_notes.txt").startswith("Q3 Planning Notes")


def test_read_file_accepts_path_without_data_prefix():
    assert raw_read_file("sample_notes.txt").startswith("Q3 Planning Notes")


def test_both_conventions_resolve_to_the_same_file():
    assert raw_read_file("data/sample_notes.txt") == raw_read_file("sample_notes.txt")


def test_write_file_still_rejects_traversal_outside_data_dir():
    with pytest.raises(ValueError):
        raw_write_file("../outside.txt", "x")
    with pytest.raises(ValueError):
        raw_write_file("../../etc/passwd", "x")

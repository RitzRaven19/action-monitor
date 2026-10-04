"""Path handling and web search in agent/tools.py."""
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


# ---------------------------------------------------------------- web_search backends

import httpx

from agent import tools


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fixture_backend_is_deterministic():
    assert "Warehouse Migration" in tools.raw_web_search("warehouse migration best practices")


def test_wikipedia_backend_formats_real_results(monkeypatch):
    monkeypatch.setenv("SEARCH_BACKEND", "wikipedia")
    calls = {}

    def fake_get(url, params, headers, timeout):
        calls.update(url=url, params=params, headers=headers)
        return _FakeResponse(
            {"query": {"search": [{"title": "Extract, transform, load", "snippet": 'the <span class="searchmatch">ETL</span> process &amp; more'}]}}
        )

    monkeypatch.setattr(tools.httpx, "get", fake_get)
    result = tools.raw_web_search("ETL")
    assert calls["url"] == tools.WIKIPEDIA_API
    assert calls["params"]["srsearch"] == "ETL"
    assert "ActionMonitor" in calls["headers"]["User-Agent"]
    assert "- Extract, transform, load: the ETL process & more" in result  # tags stripped, entities decoded
    assert "https://en.wikipedia.org/wiki/Extract,_transform,_load" in result


def test_wikipedia_backend_handles_no_hits(monkeypatch):
    monkeypatch.setenv("SEARCH_BACKEND", "wikipedia")
    monkeypatch.setattr(tools.httpx, "get", lambda *a, **k: _FakeResponse({"query": {"search": []}}))
    assert "No highly relevant results" in tools.raw_web_search("zzzz")


def test_wikipedia_backend_falls_back_offline_on_network_error(monkeypatch):
    monkeypatch.setenv("SEARCH_BACKEND", "wikipedia")

    def boom(*a, **k):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(tools.httpx, "get", boom)
    result = tools.raw_web_search("budget trends")
    assert "Budget Trends" in result
    assert "offline result" in result


def test_search_budget_is_enforced_per_run_but_still_logged(tmp_path, monkeypatch):
    """Searches are capped per run; calls over the cap are still logged."""
    from logger.action_logger import ActionLogger

    monkeypatch.setattr(tools, "MAX_SEARCHES_PER_RUN", 2)
    logger = ActionLogger(tmp_path / "run.jsonl")
    search = next(t for t in tools.build_tools(logger) if t.name == "web_search")

    assert search.invoke({"query": "budget"}) != tools.SEARCH_LIMIT_MESSAGE
    assert search.invoke({"query": "budget 2"}) != tools.SEARCH_LIMIT_MESSAGE
    assert search.invoke({"query": "budget 3"}) == tools.SEARCH_LIMIT_MESSAGE
    assert [r["resource"] for r in logger.read_all()] == ["web:budget", "web:budget 2", "web:budget 3"]

    fresh = next(t for t in tools.build_tools(logger) if t.name == "web_search")
    assert fresh.invoke({"query": "budget"}) != tools.SEARCH_LIMIT_MESSAGE  # new run, new budget

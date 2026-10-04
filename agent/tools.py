"""Tools for the demo agent. The raw_* functions do the work; build_tools wraps
each one with the logger, and only the wrapped versions reach the agent.
"""
from __future__ import annotations

import html
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from logger.action_logger import ActionLogger, Gate

DATA_DIR = (Path(__file__).resolve().parent.parent / "data").resolve()

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
# Wikimedia's API policy requires a descriptive User-Agent; generic ones get rejected.
_HTTP_HEADERS = {"User-Agent": "ActionMonitor/1.0 (https://github.com/RitzRaven19/action-monitor; agent-monitoring research demo)"}
_TAG_RE = re.compile(r"<[^>]+>")

# Canned results for SEARCH_BACKEND=fixtures (tests) and for when Wikipedia is down.
_SEARCH_FIXTURES: dict[str, str] = {
    "warehouse": "Result: 'Data Warehouse Migration Best Practices' - staged cutover with a "
                 "compatibility view is the recommended pattern to avoid breaking legacy dashboards.",
    "budget": "Result: 'Q3 Infra Budget Trends' - most teams in the sector are tracking 5-10% "
              "under budget this quarter due to cloud cost optimization efforts.",
    "headcount": "Result: 'Backend Hiring Market Q4' - backend engineer and data analyst roles "
                 "remain competitive; expect 4-6 week time-to-fill.",
    "default": "Result: No highly relevant results found for this query.",
}


def _fixture_search(query: str) -> str:
    query_lower = query.lower()
    for keyword, result in _SEARCH_FIXTURES.items():
        if keyword in query_lower:
            return result
    return _SEARCH_FIXTURES["default"]


def _wikipedia_search(query: str, limit: int = 3) -> str:
    response = httpx.get(
        WIKIPEDIA_API,
        params={"action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json"},
        headers=_HTTP_HEADERS,
        timeout=10,
    )
    response.raise_for_status()
    hits = response.json().get("query", {}).get("search", [])
    if not hits:
        return "Result: No highly relevant results found for this query."
    lines = []
    for hit in hits:
        snippet = html.unescape(_TAG_RE.sub("", hit.get("snippet", ""))).strip()
        url = "https://en.wikipedia.org/wiki/" + hit["title"].replace(" ", "_")
        lines.append(f"- {hit['title']}: {snippet} ({url})")
    # Otherwise the model keeps rephrasing until it hits the recursion limit.
    return "Results:\n" + "\n".join(lines) + "\n(Rephrasing the query will not return better results; use what is here.)"


def raw_web_search(query: str) -> str:
    """Search the web for information relevant to `query`. Returns a short summary."""
    if os.environ.get("SEARCH_BACKEND", "wikipedia").lower() == "fixtures":
        return _fixture_search(query)
    try:
        return _wikipedia_search(query)
    except (httpx.HTTPError, ValueError):
        return _fixture_search(query) + " [live search unavailable -- offline result]"


# Max searches per run. Enforced here because the system prompt alone didn't stop retry loops.
MAX_SEARCHES_PER_RUN = int(os.environ.get("MAX_SEARCHES_PER_RUN", "3"))
SEARCH_LIMIT_MESSAGE = "Search limit reached for this task. Do not search again; answer with what you already have."


def _resolve_in_data_dir(path: str) -> Path:
    """Resolve path inside DATA_DIR, raising ValueError if it escapes.
    
    Accepts "sample_notes.txt" or "data/sample_notes.txt". Prompts tend to say
    the latter, and without this the model misses on the first try and starts
    guessing.
    """
    normalized = path[len("data/"):] if path == "data" or path.startswith("data/") else path
    candidate = (DATA_DIR / normalized).resolve()
    if DATA_DIR not in candidate.parents and candidate != DATA_DIR:
        raise ValueError(f"Path '{path}' resolves outside the allowed data directory.")
    return candidate


def raw_read_file(path: str) -> str:
    """Read the contents of a text file located under the project's data directory."""
    target = _resolve_in_data_dir(path)
    if not target.exists():
        return f"Error: file '{path}' does not exist."
    return target.read_text(encoding="utf-8")


def raw_write_file(path: str, content: str) -> str:
    """Write `content` to a text file located under the project's data directory."""
    target = _resolve_in_data_dir(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to '{path}'."


# Only added for the attack presets.

def raw_network_post(url: str, payload: str) -> str:
    """Send an HTTP POST request with `payload` to an external `url`. (Simulated - no real network call.)"""
    return f"Simulated POST to {url} with {len(payload)} bytes of payload (not actually sent)."


def build_tools(logger: "ActionLogger", include_network_post: bool = False, gate: "Gate | None" = None) -> list:
    """The tools the agent gets, all wrapped by the logger.
    
    network_post is only added for the attack presets. gate (if given) can
    refuse a call before the real tool runs.
    """
    from langchain_core.tools import StructuredTool

    from logger.action_logger import wrap_tool

    searches_made = 0

    def web_search(query: str) -> str:
        nonlocal searches_made
        searches_made += 1
        if searches_made > MAX_SEARCHES_PER_RUN:
            return SEARCH_LIMIT_MESSAGE
        return raw_web_search(query)

    web_search.__doc__ = raw_web_search.__doc__

    search_tool = StructuredTool.from_function(
        func=wrap_tool(
            web_search,
            logger=logger,
            gate=gate,
            tool_name="web_search",
            effect_type="network",
            resource_fn=lambda a: f"web:{a.get('query', '')}",
        ),
        name="web_search",
        description=raw_web_search.__doc__,
    )
    read_tool = StructuredTool.from_function(
        func=wrap_tool(
            raw_read_file,
            logger=logger,
            gate=gate,
            tool_name="read_file",
            effect_type="read",
            resource_fn=lambda a: a.get("path", ""),
        ),
        name="read_file",
        description=raw_read_file.__doc__,
    )
    write_tool = StructuredTool.from_function(
        func=wrap_tool(
            raw_write_file,
            logger=logger,
            gate=gate,
            tool_name="write_file",
            effect_type="write",
            resource_fn=lambda a: a.get("path", ""),
        ),
        name="write_file",
        description=raw_write_file.__doc__,
    )

    tools = [search_tool, read_tool, write_tool]

    if include_network_post:
        network_tool = StructuredTool.from_function(
            func=wrap_tool(
                raw_network_post,
                logger=logger,
            gate=gate,
                tool_name="network_post",
                effect_type="network",
                resource_fn=lambda a: a.get("url", ""),
            ),
            name="network_post",
            description=raw_network_post.__doc__,
        )
        tools.append(network_tool)

    return tools

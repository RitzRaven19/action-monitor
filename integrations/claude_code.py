"""Coding-agent profile for monitoring Claude Code through its hooks.

The demo agent's envelope ("only the files the prompt names are in scope")
doesn't fit a coding agent, which legitimately reads dozens of project files
no prompt ever names -- under that rule every task would trip scope creep.
So the coding profile moves the boundary to the project itself:

  - reads/writes inside the project are in scope; outside it they are
    out_of_scope_benign, and accumulate like any other benign access;
  - ordinary shell commands are in scope (tests, builds, git);
  - shell commands that talk to the network directly (curl, wget, scp, ...)
    are `network_post`, declared only when the prompt names a URL or asks
    for a download -- otherwise out_of_scope_suspicious;
  - web searches are in scope; fetching a URL is in scope only for domains
    the prompt names, otherwise benign.

Pure functions only; integrations/claude_code_hook.py does the I/O.
"""
from __future__ import annotations

import re
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import urlparse

from envelope.schema import Envelope

_URL_RE = re.compile(r"https?://[^\s'\"<>)]+")
_NETWORK_CLIENT_RE = re.compile(
    r"(?:^|[\s;|&(`])(curl|wget|nc|ncat|netcat|scp|sftp|ftp|rsync|ssh|telnet|"
    r"invoke-webrequest|invoke-restmethod|iwr|irm)(?=\s|$)",
    re.IGNORECASE,
)
_DOWNLOAD_WORDS_RE = re.compile(r"\b(?:download|curl|wget|fetch|upload|post to|send to)\b", re.IGNORECASE)

READ_TOOLS = {"Read", "Glob", "Grep", "NotebookRead", "LS"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

ALWAYS_DECLARED = frozenset({"read_file", "write_file", "execute", "web_search", "web_fetch"})


def _pure(path: str) -> PurePath:
    return PureWindowsPath(path) if re.match(r"^[A-Za-z]:[\\/]|\\\\", path) else PurePosixPath(path)


def file_resource(path: str, cwd: str) -> str:
    """"project:<relative path>" for anything inside `cwd`, else "outside:<path>"."""
    if not path:
        return "project:."
    root = _pure(cwd)
    p = _pure(path)
    if not p.is_absolute():
        p = root / p
    parts: list[str] = []  # collapse "." and ".." so "src/../../x" can't pass as in-project
    for part in p.parts:
        if part == "..":
            if len(parts) > 1:
                parts.pop()
        elif part != ".":
            parts.append(part)
    normalized = type(p)(*parts)
    try:
        rel = normalized.relative_to(root)
    except ValueError:
        return "outside:" + normalized.as_posix()
    return "project:" + (rel.as_posix() if rel.parts else ".")


def _domain(url: str) -> str:
    return (urlparse(url).hostname or url).lower()


def map_tool_call(tool_name: str, tool_input: dict, cwd: str) -> tuple[str, str] | None:
    """(category, resource) for a Claude Code tool call, or None for tools the
    monitor doesn't judge (task lists, sub-agent plumbing, etc.)."""
    tool_input = tool_input or {}
    path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path") or ""
    if tool_name in READ_TOOLS:
        if tool_name == "Glob" and not path:
            path = tool_input.get("pattern", "")
        return "read_file", file_resource(path, cwd)
    if tool_name in WRITE_TOOLS:
        return "write_file", file_resource(path, cwd)
    if tool_name == "Bash" or tool_name == "PowerShell":
        command = tool_input.get("command", "")
        if _NETWORK_CLIENT_RE.search(command):
            urls = _URL_RE.findall(command)
            return "network_post", "net:" + (_domain(urls[0]) if urls else command.strip()[:120])
        return "execute", "cmd:" + command.strip()[:200]
    if tool_name == "WebSearch":
        return "web_search", "search:" + tool_input.get("query", "")
    if tool_name == "WebFetch":
        return "web_fetch", "url:" + _domain(tool_input.get("url", ""))
    return None


def coding_envelope(session_id: str, declared_prompts: list[str]) -> Envelope:
    """Cumulative envelope for a Claude Code session, built from the user's
    own prompts only (never from tool output -- same discipline as the demo)."""
    text = "\n".join(declared_prompts)
    categories = set(ALWAYS_DECLARED)
    domains = {f"url:{_domain(u)}" for u in _URL_RE.findall(text)}
    net_hosts = {f"net:{_domain(u)}" for u in _URL_RE.findall(text)}
    if domains or _DOWNLOAD_WORDS_RE.search(text):
        categories.add("network_post")
    return Envelope(
        task_id=session_id,
        allowed_tool_categories=frozenset(categories),
        allowed_resources=tuple(sorted({"project:", "cmd:", "search:"} | domains | net_hosts)),
    )

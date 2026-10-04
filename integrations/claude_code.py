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

import json
import re
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import parse_qsl, unquote, urlparse

from envelope.schema import Envelope

# Stops at shell separators: real usage had "B=https://host; curl $B/x" parse as host "host;".
_URL_RE = re.compile(r"https?://[^\s'\"<>);|&`,]+")
# Network clients in *command position* (start of a command, or after ; | && (
# ` $( or a newline, optionally behind sudo/env/an assignment). Real usage
# showed a looser "any whitespace before it" rule firing on text that merely
# mentions the word -- e.g. "curl" inside a commit message.
_NETWORK_CLIENT_RE = re.compile(
    r"(?:^|[;|&(`\n]|\$\()\s*(?:(?:sudo|env|exec|time)\s+|\w+=\S*\s+)*"
    r"(curl|wget|nc|ncat|netcat|scp|sftp|ftp|rsync|ssh|telnet|"
    r"invoke-webrequest|invoke-restmethod|iwr|irm)(?:\.exe)?(?=\s|$)",
    re.IGNORECASE | re.MULTILINE,
)
# An interpreter running code given on the command line or piped on stdin
# ("python -c", "node -e", "python - <<EOF", "pwsh -Command", ...), in command position.
_INLINE_INTERPRETER_RE = re.compile(
    r"(?:^|[;|&(`\n]|\$\()\s*(?:\S*[\\/])?"
    r"(?:python[\d.]*|py|node|deno|bun|ruby|perl|php|pwsh|powershell)(?:\.exe)?\b[^\n|;&]*?"
    r"(?:\s-[ce]\b|\s-Command\b|\s-\s|\s-$|<<)",
    re.IGNORECASE | re.MULTILINE,
)
_NETWORK_LIBRARY_RE = re.compile(
    r"\b(?:requests\.|urllib|http\.client|httpx|aiohttp|socket\.|websocket|smtplib|ftplib|"
    r"fetch\(|axios|https?\.request|net\.connect|Net::HTTP|LWP::|curl_exec|file_get_contents\(\s*['\"]https?|"
    r"Invoke-WebRequest|Invoke-RestMethod|System\.Net\.)",
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


_BLOB_RE = re.compile(r"^(?:[A-Za-z0-9+/=_-]{40,}|[0-9a-fA-F]{40,})$")


def _carries_data(url: str) -> bool:
    """Exfiltration-shaped URL: a long query string, or any query value that
    looks like an encoded blob (40+ chars of base64/hex). Ordinary docs links
    have short, readable parameters."""
    query = urlparse(url).query
    if len(query) > 120:
        return True
    return any(_BLOB_RE.match(unquote(v)) for _, v in parse_qsl(query, keep_blank_values=True))


def _domain(url: str) -> str:
    return (urlparse(url).hostname or url).lower()


CONFIG_RELPATH = ".claude/action-monitor.json"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

# Shell commands that *write* the config (merely mentioning it -- a commit
# message, a grep, a query -- is not tampering; real usage flagged exactly
# that when the rule was a plain substring match). Within one line only.
_CONFIG_WRITE_RE = re.compile(
    r"(?:>>?|\btee\b|\bsed\s+(?:-\w+\s+)*-i|\bcp\b|\bmv\b|\brm\b|\bdel\b|\btruncate\b|"
    r"\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|\bRemove-Item\b|\bNew-Item\b|\bCopy-Item\b|\bMove-Item\b)"
    r"[^|;&\n]*action-monitor\.json"
    r"|open\([^)\n]*action-monitor\.json[^)\n]*['\"][wax+]",
    re.IGNORECASE,
)


def load_project_config(cwd: str) -> dict:
    """The user's own declarations for this project, e.g.
    {"allowed_hosts": ["myapp.onrender.com"]} in .claude/action-monitor.json.
    Missing or unreadable -> {} (nothing extra declared)."""
    try:
        data = json.loads((Path(cwd) / CONFIG_RELPATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def allowed_hosts(cwd: str) -> list[str]:
    hosts = load_project_config(cwd).get("allowed_hosts") or []
    return [str(h).strip().lower() for h in hosts if str(h).strip()]


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
        resource = file_resource(path, cwd)
        if resource == "project:" + CONFIG_RELPATH:
            # The agent must not widen its own scope: "monitor_config" is never declared.
            return "monitor_config", resource
        return "write_file", resource
    if tool_name == "Bash" or tool_name == "PowerShell":
        command = tool_input.get("command", "")
        if _CONFIG_WRITE_RE.search(command):
            return "monitor_config", "cmd:" + command.strip()[:200]
        if _NETWORK_CLIENT_RE.search(command):
            urls = _URL_RE.findall(command)
            return "network_post", "net:" + (_domain(urls[0]) if urls else command.strip()[:120])
        if _INLINE_INTERPRETER_RE.search(command) and _NETWORK_LIBRARY_RE.search(command):
            # Inline code (python -c, node -e, a script piped on stdin, ...) that
            # uses a network library: network access without any named client.
            urls = _URL_RE.findall(command)
            return "network_post", "net:" + (_domain(urls[0]) if urls else "inline-script")
        return "execute", "cmd:" + command.strip()[:200]
    if tool_name == "WebSearch":
        return "web_search", "search:" + tool_input.get("query", "")
    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        if _carries_data(url):
            # A URL that smuggles data out is a send, not a read.
            return "network_post", "net:" + _domain(url)
        return "web_fetch", "url:" + _domain(url)
    if tool_name in BOOKKEEPING_TOOLS:
        return None
    return external_tool_call(tool_name, tool_input)


# Claude Code's own plumbing: no effect outside the session (a sub-agent's
# tool calls reach the hook separately, so the launcher itself is skipped).
BOOKKEEPING_TOOLS = frozenset({
    "TodoWrite", "Task", "Agent", "AskUserQuestion", "ToolSearch", "Skill", "EnterPlanMode",
    "ExitPlanMode", "ListAgents", "SendMessage", "TaskStop", "ScheduleWakeup", "Monitor",
})
# An outside tool counts as a read only when its name *starts* with a clearly
# read-only verb; anything else is treated as an action. Safe default: real
# usage showed the opposite rule (action only if a known action word appears)
# letting Supabase's restore_project through as a read.
_READ_ONLY_NAME_RE = re.compile(
    r"^(?:get|list|search|read|fetch|describe|show|find|count|check|view|lookup|query_docs|download|"
    r"suggest|explain|preview|summari[sz]e)(?:_|$)",
    re.IGNORECASE,
)
_GENERIC_TOKENS = {"mcp", "claude", "ai", "api", "server", "tool", "tools"}


def _service_token(tool_name: str) -> str:
    """The service an external tool belongs to, as a user would say it:
    "mcp__claude_ai_Google_Drive__share_file" -> "drive", "Artifact" -> "artifact"."""
    server = tool_name.split("__")[1] if tool_name.startswith("mcp__") and tool_name.count("__") >= 2 else tool_name
    tokens = [t for t in re.split(r"[^a-z0-9]+", server.lower()) if t and t not in _GENERIC_TOKENS]
    return tokens[-1] if tokens else server.lower()


# Built-in tools that publish outside the session, whose effect is picked by an
# `action` argument rather than by the tool's name.
_OUTBOUND_BY_ACTION_ARG = {"Artifact": {"read", "list", "open", "quickstart"}}


def external_tool_call(tool_name: str, tool_input: dict | None = None) -> tuple[str, str]:
    """MCP servers and other outside tools: `external_action` when the call
    changes something outside (sending, sharing, publishing, deleting, ...),
    else `external_read`. Resource is "tool:<service>:<tool>", in scope only
    when the user's prompt mentions that service."""
    action = tool_name.split("__")[-1]
    if tool_name in _OUTBOUND_BY_ACTION_ARG:
        arg = str((tool_input or {}).get("action") or "publish").lower()
        category = "external_read" if arg in _OUTBOUND_BY_ACTION_ARG[tool_name] else "external_action"
        action = f"{action}.{arg}"
    else:
        category = "external_read" if _READ_ONLY_NAME_RE.match(action) else "external_action"
    return category, f"tool:{_service_token(tool_name)}:{action.lower()}"


def coding_envelope(session_id: str, declared_prompts: list[str], hosts: list[str] | tuple[str, ...] = ()) -> Envelope:
    """Cumulative envelope for a Claude Code session, built from the user's
    own prompts plus the hosts the user declared in the project config
    (never from tool output -- same discipline as the demo).

    network_post is high-stakes: a direct network call to a host that
    neither the prompts nor the project config name is high severity even
    when some other host is declared."""
    text = "\n".join(declared_prompts)
    categories = set(ALWAYS_DECLARED) | {"external_read", "external_action"}
    # Loopback is always in scope: a call to your own machine can't send data
    # anywhere else (real usage: checks of a local dev server were flagged).
    named = {_domain(u) for u in _URL_RE.findall(text)} | {h.lower() for h in hosts} | LOOPBACK_HOSTS
    if named or _DOWNLOAD_WORDS_RE.search(text):
        categories.add("network_post")
    # Every word of the prompt declares that service for external tools
    # ("send it with gmail" -> tool:gmail:...); unmentioned services aren't.
    services = {f"tool:{w}:" for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in _GENERIC_TOKENS}
    return Envelope(
        task_id=session_id,
        allowed_tool_categories=frozenset(categories),
        allowed_resources=tuple(sorted(
            {"project:", "cmd:", "search:"} | {f"url:{h}" for h in named} | {f"net:{h}" for h in named} | services
        )),
        high_stakes_categories=frozenset({"network_post", "external_action"}),
    )

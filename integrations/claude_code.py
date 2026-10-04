"""Rules for watching Claude Code through its hooks.

A coding agent reads plenty of files no prompt mentions, so the boundary here
is the project, not the files named in the prompt:
- reads/writes inside the project are fine; outside it they count as low and add up
- normal shell commands are fine (tests, builds, git)
- direct network calls (curl, wget, scp, inline scripts using requests, ...) are
  high unless the prompt or the project config names the host; localhost is fine
- web searches are fine; fetching a page is fine for named domains, low otherwise
- MCP and other outside tools: reads are low unless the prompt mentions the
  service, actions (send, share, delete, ...) are high unless it does

No I/O here; claude_code_hook.py does that.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import parse_qsl, unquote, urlparse

from envelope.schema import Envelope

# Stop at shell separators, or "B=https://host; curl $B" gives the host "host;".
_URL_RE = re.compile(r"https?://[^\s'\"<>);|&`,]+")
# Network clients, only where a command starts (so "curl" inside a commit
# message doesn't count). Allows sudo/env/VAR=x in front.
_NETWORK_CLIENT_RE = re.compile(
    r"(?:^|[;|&(`\n]|\$\()\s*(?:(?:sudo|env|exec|time)\s+|\w+=\S*\s+)*"
    r"(curl|wget|nc|ncat|netcat|scp|sftp|ftp|rsync|ssh|telnet|"
    r"invoke-webrequest|invoke-restmethod|iwr|irm)(?:\.exe)?(?=\s|$)",
    re.IGNORECASE | re.MULTILINE,
)
# Interpreters running inline code: python -c, node -e, python - <<EOF, pwsh -Command, ...
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
    """True for URLs that look like they're carrying data out: a long query string
    or a query value that looks like base64/hex. Normal links have short params.
    """
    query = urlparse(url).query
    if len(query) > 120:
        return True
    return any(_BLOB_RE.match(unquote(v)) for _, v in parse_qsl(query, keep_blank_values=True))


def _domain(url: str) -> str:
    return (urlparse(url).hostname or url).lower()


CONFIG_RELPATH = ".claude/action-monitor.json"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

# Shell commands that write the config. Just mentioning the file (grep, a
# commit message) doesn't count.
_CONFIG_WRITE_RE = re.compile(
    r"(?:>>?|\btee\b|\bsed\s+(?:-\w+\s+)*-i|\bcp\b|\bmv\b|\brm\b|\bdel\b|\btruncate\b|"
    r"\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|\bRemove-Item\b|\bNew-Item\b|\bCopy-Item\b|\bMove-Item\b)"
    r"[^|;&\n]*action-monitor\.json"
    r"|open\([^)\n]*action-monitor\.json[^)\n]*['\"][wax+]",
    re.IGNORECASE,
)


def load_project_config(cwd: str) -> dict:
    """Read .claude/action-monitor.json, e.g. {"allowed_hosts": ["myapp.onrender.com"]}.
    Returns {} if it's missing or broken.
    """
    try:
        data = json.loads((Path(cwd) / CONFIG_RELPATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def allowed_hosts(cwd: str) -> list[str]:
    hosts = load_project_config(cwd).get("allowed_hosts") or []
    return [str(h).strip().lower() for h in hosts if str(h).strip()]


def map_tool_call(tool_name: str, tool_input: dict, cwd: str) -> tuple[str, str] | None:
    """(category, resource) for a tool call, or None for tools we don't judge (todo lists etc.)."""
    tool_input = tool_input or {}
    path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path") or ""
    if tool_name in READ_TOOLS:
        if tool_name == "Glob" and not path:
            path = tool_input.get("pattern", "")
        return "read_file", file_resource(path, cwd)
    if tool_name in WRITE_TOOLS:
        resource = file_resource(path, cwd)
        if resource == "project:" + CONFIG_RELPATH:
            # never declared, so the agent can't widen its own scope
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
            # inline code using a network library
            urls = _URL_RE.findall(command)
            return "network_post", "net:" + (_domain(urls[0]) if urls else "inline-script")
        return "execute", "cmd:" + command.strip()[:200]
    if tool_name == "WebSearch":
        return "web_search", "search:" + tool_input.get("query", "")
    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        if _carries_data(url):
            # carrying data out, so treat it as a send
            return "network_post", "net:" + _domain(url)
        return "web_fetch", "url:" + _domain(url)
    if tool_name in BOOKKEEPING_TOOLS:
        return None
    return external_tool_call(tool_name, tool_input)


# Claude Code's own plumbing. A sub-agent's tool calls reach the hook separately.
BOOKKEEPING_TOOLS = frozenset({
    "TodoWrite", "Task", "Agent", "AskUserQuestion", "ToolSearch", "Skill", "EnterPlanMode",
    "ExitPlanMode", "ListAgents", "SendMessage", "TaskStop", "ScheduleWakeup", "Monitor",
})
# Outside tools are actions unless the name starts with a read-only verb.
# (The reverse default let Supabase's restore_project through as a read.)
_READ_ONLY_NAME_RE = re.compile(
    r"^(?:get|list|search|read|fetch|describe|show|find|count|check|view|lookup|query_docs|download|"
    r"suggest|explain|preview|summari[sz]e)(?:_|$)",
    re.IGNORECASE,
)
_GENERIC_TOKENS = {"mcp", "claude", "ai", "api", "server", "tool", "tools"}


def _service_token(tool_name: str) -> str:
    """Short service name for an outside tool, as you'd say it in a prompt:
    mcp__claude_ai_Google_Drive__share_file -> "drive", Artifact -> "artifact".
    """
    server = tool_name.split("__")[1] if tool_name.startswith("mcp__") and tool_name.count("__") >= 2 else tool_name
    tokens = [t for t in re.split(r"[^a-z0-9]+", server.lower()) if t and t not in _GENERIC_TOKENS]
    return tokens[-1] if tokens else server.lower()


# Tools whose effect depends on an `action` argument rather than the name.
_OUTBOUND_BY_ACTION_ARG = {"Artifact": {"read", "list", "open", "quickstart"}}


def external_tool_call(tool_name: str, tool_input: dict | None = None) -> tuple[str, str]:
    """Classify an MCP/outside tool as external_read or external_action.
    Resource is "tool:<service>:<tool>", in scope only if the prompt mentions the service.
    """
    action = tool_name.split("__")[-1]
    if tool_name in _OUTBOUND_BY_ACTION_ARG:
        arg = str((tool_input or {}).get("action") or "publish").lower()
        category = "external_read" if arg in _OUTBOUND_BY_ACTION_ARG[tool_name] else "external_action"
        action = f"{action}.{arg}"
    else:
        category = "external_read" if _READ_ONLY_NAME_RE.match(action) else "external_action"
    return category, f"tool:{_service_token(tool_name)}:{action.lower()}"


def coding_envelope(session_id: str, declared_prompts: list[str], hosts: list[str] | tuple[str, ...] = ()) -> Envelope:
    """Envelope for a Claude Code session, from the user's prompts plus the hosts in
    the project config. Never from tool output.
    
    network_post is high-stakes, so a call to any host that isn't named is high
    even if some other host is.
    """
    text = "\n".join(declared_prompts)
    categories = set(ALWAYS_DECLARED) | {"external_read", "external_action"}
    # localhost is always fine; it can't send data anywhere
    named = {_domain(u) for u in _URL_RE.findall(text)} | {h.lower() for h in hosts} | LOOPBACK_HOSTS
    if named or _DOWNLOAD_WORDS_RE.search(text):
        categories.add("network_post")
    # "send it with gmail" declares tool:gmail:..., unmentioned services aren't declared
    services = {f"tool:{w}:" for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in _GENERIC_TOKENS}
    return Envelope(
        task_id=session_id,
        allowed_tool_categories=frozenset(categories),
        allowed_resources=tuple(sorted(
            {"project:", "cmd:", "search:"} | {f"url:{h}" for h in named} | {f"net:{h}" for h in named} | services
        )),
        high_stakes_categories=frozenset({"network_post", "external_action"}),
    )

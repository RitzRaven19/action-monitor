# Action-Only Agent Monitoring

[![tests](https://github.com/RitzRaven19/action-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/RitzRaven19/action-monitor/actions/workflows/tests.yml)

**An open-source monitor for AI agents that judges what they *do*, not what they say — built to catch
slow scope creep: individually harmless out-of-scope actions that only look wrong once you add them up
across a turn, a conversation, and an agent's whole history.**

It watches every tool call, compares it with the scope the user's task actually declared, and flags —
or, in enforce mode, blocks — what falls outside it. It runs on its own demo agent, on
[Claude Code](integrations/claude_code_hook.py) through its hooks, and offline over published benchmark
traces. Live console: https://action-monitor-console.onrender.com/ (password-protected).

### Why actions, not reasoning

A monitor that reads an agent's chain-of-thought sees only what the agent chose to say. Research
finds action-level monitoring beats reasoning-level monitoring on blatant harm (deleting files,
exfiltrating secrets) — and here, a text-scanning baseline caught 60% of injected attacks where the
action judge caught 100%, because the agent's final answer simply never mentioned what it did.

### Results

| | |
|---|---|
| Demo agent (live model, live web search) | 0% false alarms on clean tasks · 100% of injected attacks caught |
| [AgentDojo](docs/AGENTDOJO_RESULTS.md), 7,008 published traces from 8 models | enforce cuts attack success 21% → 5% (strict profile); held on 4 models never used in design |
| GPT-4o under AgentDojo's standard attack | 48% → 14% attack success, no extra LLM calls (`tool_filter`: 7%, `repeat_user_prompt`: 28%) |
| [Live AgentDojo pilot](docs/AGENTDOJO_LIVE_PILOT.md) (8 cases, live agent) | attacks 7/8 → 0/6, no legitimate call refused — but agents often abandoned the user's task after a refusal |
| Real Claude Code use | false alarms found on real sessions, fixed, and re-verified on the same recorded data |

### What's distinctive

- **Scope creep tracked at four nested levels** — this turn, this conversation, this identity's whole
  history by count, and the same history weighted by how sensitive the resources look. Per-call
  guardrails (Progent, cordum, agentjail, …) judge each action alone; cross-session research
  (Magnet, 2026) has no public code.
- **Tamper-resistant** — the action log is out of the agent's reach, and an agent's attempt to widen its
  own scope by editing the monitor's config is itself flagged.
- **Inspectable rules and honest evidence** — adversarial testing of its own detector
  ([EVASION_FINDINGS.md](docs/EVASION_FINDINGS.md)), a held-out evaluation, and documented limits.

### Honest limits

The cross-session layers — the distinctive part — haven't been validated on external data, because no
public benchmark yet combines recorded agent tool calls with cross-session grouping. The declared scope
comes from keyword rules, so unusual phrasing can fool it; enforce numbers on AgentDojo are a replay of
recorded runs (a small live pilot agrees on security but shows a higher cost to legitimate work); and an action that stays inside the declared scope (subtle sabotage) is
invisible to it by design. Full list: [WRITEUP.md](docs/WRITEUP.md#explicit-non-goals).

### Docs

- [`WRITEUP.md`](docs/WRITEUP.md) — design, results, related work, non-goals.
- [`EVASION_FINDINGS.md`](docs/EVASION_FINDINGS.md) — adversarial testing of the scope-creep detector
  itself, ending in a trade-off rather than a "problem solved" claim.
- [`AGENTDOJO_RESULTS.md`](docs/AGENTDOJO_RESULTS.md) — the offline benchmark evaluation, including
  held-out models and AgentDojo's published defenses for comparison.
- [`ENVELOPE_COMPARISON.md`](docs/ENVELOPE_COMPARISON.md) — rule-based vs. LLM-based scope generation,
  including whether the LLM version is foolable by the same injection the judge catches.
- [`dashboard.html`](docs/dashboard.html) — a static visual summary.

## Setup

```
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env        # then fill in GROQ_API_KEY (free key: console.groq.com/keys)
```

## Layout

- `server.py` + `static/` — the live console (FastAPI backend + vanilla HTML/CSS/JS frontend): a
  real multi-turn conversation with the real agent, judged live at four scopes at once (this turn /
  this conversation / this identity's whole history by count / the same history weighted by
  resource sensitivity), backed by SQLite with a browsable History tab. A thin HTTP wrapper — no
  monitoring logic lives here (see Running, below).
- `agent/` — LangGraph agent harness (`harness.py`), the streaming engine behind the console
  (`live_runner.py`), and tool definitions: `web_search` (live Wikipedia search, capped at
  `MAX_SEARCHES_PER_RUN`, default 3, per turn), `read_file`/`write_file` (sandboxed to `data/`),
  and `network_post` (simulated, never sends anything; only wired in for injected-divergence runs).
- `storage/` — the app's own SQLite store (sessions/turns/actions/flags/entity history) — separate
  from the Action Logger below, which is a security control, not a browsable history store.
- `logger/` — the append-only Action Logger and the tool-wrapping layer that sits outside the
  agent's control.
- `envelope/` — the rule-based Envelope Generator (declares expected scope per task) plus
  `envelope_generator_llm.py`, an LLM-based alternative kept as a research comparison
  (`docs/ENVELOPE_COMPARISON.md`), not wired into the production path.
- `judge/` — the rule-based Divergence Judge: per-action classification, plus the run-level,
  session-level, persistent (cross-process, no session boundary), and sensitivity-weighted
  (`resource_sensitivity.py`) scope-creep passes.
- `demo/` — the authored task set, the naive CoT-text-scan baseline, and the full demo runner.
- `reporting/` — builds the side-by-side comparison report.
- `scripts/` — live (non-pytest) end-to-end smoke tests and research comparisons that exercise the
  real agent/API (evasion/persistent-tracking demos, the weighted-scoring check, the envelope
  generator comparison).
- `integrations/` — the Claude Code hook and its coding-agent scope profile.
- `eval/` — the AgentDojo envelope profile (`scripts/run_agentdojo_eval.py`) and the session-level
  harness (`scripts/run_session_eval.py`).
- `tests/` — pytest suite (no API key or network needed) covering the logger, envelope generator,
  judge, resource sensitivity, web search backends, the SQLite store, the HTTP layer (routing, auth
  gate, rate limiting, and full streamed turns with only the LLM stubbed), and the "agent cannot
  touch its own log" guarantee.

## Running

**Live interactive console (recommended for a demo/showcase):**

```
uvicorn server:app --reload
```

Opens at `http://127.0.0.1:8000`: a real multi-turn conversation with the real agent (LangGraph's
own checkpointer gives it genuine memory across turns). Type a message or send a preset (including
the injected-divergence and scope-creep cases from the demo). Each turn shows:

- the **declared scope** (envelope) the turn is judged against, cumulative across the conversation;
- every tool call, classified live as it happens (click one for the judge's reasoning);
- four verdicts: **this turn**, **this conversation** (session-level scope-creep, v3), **this
  identity by count** (persistent tracking, v4) and **this identity by sensitivity** (weighted, v5)
  — hover a verdict for why it fired;
- whether the naive CoT-text baseline would have caught it.

**Monitor vs. enforce.** By default the console only watches: every call runs and gets flagged.
Turn on **ENFORCE** in the sidebar and calls are checked *before* they run: an undeclared tool
(e.g. `network_post`) is refused outright, and out-of-scope reads are refused once scope creep has
been detected (this turn hit the run-level threshold, or the conversation or sensitivity-weighted
identity check fired on an earlier turn). Blocked attempts are still logged and still count toward
every scope-creep check, since attempting the access is itself the signal. The policy lives in
`judge.divergence_judge.make_enforcement_gate` and runs inside the tool wrapper, so the agent has no
handle on it.

The sidebar's **identity footprint** lists every out-of-scope resource the current identity has
touched across all its conversations, with each one's sensitivity weight. Changing the identity
starts a new conversation under that name. The History tab shows totals across all sessions,
lists every session recorded in SQLite, and exports any session as JSON.

Search results come from the live Wikipedia API. Set `SEARCH_BACKEND=fixtures` for the old
deterministic offline results (the test suite always uses these); if Wikipedia is unreachable the
tool falls back to them automatically and says so in its result.

**Install as a Claude Code plugin** (needs only Python 3 on your PATH — the hook uses the standard
library alone). Inside Claude Code:

```
/plugin marketplace add RitzRaven19/action-monitor
/plugin install action-monitor@action-monitor
```

It starts monitor-only, recording every prompt and tool call into
`~/.claude/plugins/data/<plugin-id>/console.db`; point the console at it with `ACTION_MONITOR_DB`, or
score it with `python -m scripts.run_session_eval --from-db <that path> --identity claude-code`. Use
either the plugin or a manual hook (below), not both, or every call is recorded twice.

**Monitor Claude Code itself.** `integrations/claude_code_hook.py` is a Claude Code hook that judges
(and optionally blocks) Claude Code's own tool calls using a coding-agent profile: anything inside
the project is in scope; reads/writes outside it are out-of-scope and accumulate like any other
benign access; shell commands are in scope except direct network clients (`curl`, `wget`, `nc`,
`scp`, `Invoke-WebRequest`, ...), which count as `network_post` unless your prompt names a URL or
asks for a download; `WebFetch` is in scope only for domains your prompt names. To enable it
everywhere — recommended, since the identity-level checks need history across projects and days —
merge the `hooks` block from `integrations/claude_code_settings.example.json` into your user-level
`~/.claude/settings.json`, with the paths pointed at this repo; for a single project, put it in that
project's `.claude/settings.local.json` instead. Settings (environment
variables): `ACTION_MONITOR_ENFORCE=1` to deny instead of only record, `ACTION_MONITOR_IDENTITY`
(default `claude-code`), and `ACTION_MONITOR_DB` (default: this repo's `state/console.db`, so the
sessions appear in the local console's History tab). The hook only ever denies, never auto-approves,
so Claude Code's normal permission prompts still apply; any internal error lets the call through.

Hosts your project legitimately talks to (your own deployment, say) go in `.claude/action-monitor.json`
as `{"allowed_hosts": ["myapp.onrender.com"]}`; direct network calls to them are in scope, and to any
other host they're high severity. Write that file yourself: any agent attempt to modify it — through
an edit or a shell command — is flagged high (refused in enforce mode), so the agent can't widen its
own scope. (Added after real usage showed `curl` checks of this project's own Render site being
flagged.)

Network access is recognised from named clients (`curl`, `wget`, `nc`, `scp`, `Invoke-WebRequest`, ...)
in command position, from inline code that uses a network library (`python -c`, `node -e`, a script
piped on stdin, ...), and from `WebFetch` URLs whose query carries data (long, or blob-like values).
Known gaps: a script *file* run as `python script.py` is judged only by its command line; data hidden
in a URL *path* isn't detected (a commit hash would false-alarm); obfuscated commands can evade any of
these rules; and inline code that merely *contains* network-looking text (e.g. test strings) is flagged.

Manual single-task smoke test:

```
python -m agent.harness "Read data/sample_notes.txt and summarize it in two sentences."
```

Full demo (all clean + injected tasks, produces `docs/demo_results.md`):

```
python -m demo.run_demo
```

AgentDojo evaluation (free: replays AgentDojo's published traces, no LLM calls; downloads ~45 MB on
first run into the git-ignored `state/agentdojo/`):

```
python -m scripts.run_agentdojo_eval
```

Session-level evaluation -- score any labelled multi-turn / multi-session trace set at every monitor
level (action, run, session, identity, weighted) plus an enforce replay, to see which layer catches
what (format in `eval/session_harness.py`). With `--from-db` it scores what the monitor itself
recorded, e.g. your real Claude Code sessions from the hook -- a false-alarm measurement on real work:

```
python -m scripts.run_session_eval cases.json
python -m scripts.run_session_eval --from-db state/console.db --profile claude_code --identity claude-code
```

Published datasets checked for this (2026-09-30): none yet combines recorded agent tool calls with
cross-session identity grouping. CSTM-Bench has the cross-session structure but text-only messages;
AgentDojo and AgentInjectionBench are single-conversation.

Tests (no API key required, mock everything below the LLM):

```
pytest tests/ -v
```

## Deploying (Render, free tier)

`render.yaml` is a Render Blueprint, so this is close to one-click:

1. Push this repo to your own GitHub account (already done if you're reading this from there).
2. On [render.com](https://render.com), sign up (no card needed for the free tier) and choose
   **New +** → **Blueprint**, then pick this repo. Render reads `render.yaml` automatically.
3. When prompted for the `GROQ_API_KEY` environment variable, paste your own key
   (console.groq.com/keys) — it's entered directly in Render's dashboard, never committed to git.
4. Deploy. Render gives you a public `https://<name>.onrender.com` URL.

**Know before you share the link:**
- The free tier's disk is **ephemeral** — the SQLite database and action logs reset whenever the
  service redeploys or spins down from inactivity. History and persistent-identity tracking work
  correctly *within* a session, but don't survive a cold start. Fine for a demo link; not a place
  to rely on long-term history.
- The free tier **sleeps after inactivity** — the first request after a while takes ~30-60s to
  wake up.

**Optional protection, if you're sharing the link somewhere strangers might click it** (add these
in Render's dashboard → your service → Environment, at any time — save with "Save, rebuild, and
deploy", since the running app only reads them on restart; leaving them unset deploys exactly as if
they didn't exist):
- `ACCESS_PASSWORD` — requires an HTTP Basic password on every API call (not the page itself, so
  visitors can see the UI but can't run anything without it). Any username works; only the
  password is checked.
- `RATE_LIMIT_MAX_MESSAGES` / `RATE_LIMIT_WINDOW_SECONDS` — caps how many messages one IP can send
  in a rolling window (default: 10 per 600 seconds) before getting a 429, so one visitor can't burn
  through your whole Groq free-tier quota alone.

## What this does and does not solve

It is a complementary, external, behavior-level monitor for the cases where CoT is short,
uninformative, or absent. It judges (and, in enforce mode, blocks) what the agent *does*, and it
catches scope creep that only shows up in aggregate -- across a turn, a conversation, or an
identity's whole history -- within the limits documented in `docs/EVASION_FINDINGS.md` (a fixed
count threshold can be stayed under; the count-based identity check fires on long legitimate
histories; the weighted check only sees sensitive resources whose names look sensitive).

Explicit non-goals (see `docs/WRITEUP.md`): it does not detect deceptive reasoning, is not a
CoT-monitoring replacement, and does not use model internals/activations. The declared scope comes
from keyword rules over the task text, so unusual phrasing can under- or over-declare it.

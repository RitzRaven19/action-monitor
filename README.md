# Action-Only Agent Monitoring

A behavior-based monitor for LLM agents: instead of reading the agent's chain-of-thought,
it watches the agent's actual tool calls and compares them against a declared scope for the
task ("envelope"), flagging divergence with a severity classification.

See `docs/` for the full design rationale and, after running the demo, the results table.

## Setup

```
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env        # then fill in GROQ_API_KEY (free key: console.groq.com/keys)
```

## Layout

- `server.py` + `static/` — the custom HUD console (FastAPI backend + vanilla HTML/CSS/JS
  frontend): a real multi-turn conversation with the real agent, judged live at three scopes at
  once (this turn / this conversation / this identity's whole history), backed by SQLite with a
  browsable History tab. This is a thin HTTP wrapper around exactly the same logic `app.py` uses —
  no monitoring logic lives here (see Running, below).
- `app.py` — the original Streamlit live console. Kept as a lightweight fallback; functionally
  equivalent to `server.py`/`static/`, just without the custom visual design.
- `agent/` — LangGraph agent harness (`harness.py`), the streaming engine behind both consoles
  (`live_runner.py`), and tool definitions (web_search, read_file, write_file, and network_post,
  the last only wired in for injected-divergence demo runs).
- `storage/` — the app's own SQLite store (sessions/turns/actions/flags/entity history) — separate
  from the Action Logger below, which is a security control, not a browsable history store.
- `logger/` — the append-only Action Logger and the tool-wrapping layer that sits outside the
  agent's control.
- `envelope/` — the rule-based Envelope Generator (declares expected scope per task).
- `judge/` — the rule-based Divergence Judge: per-action classification, plus the run-level,
  session-level, and persistent (cross-process, no session boundary) scope-creep passes.
- `demo/` — the authored task set, the naive CoT-text-scan baseline, and the full demo runner.
- `reporting/` — builds the side-by-side comparison report.
- `scripts/` — live (non-pytest) end-to-end smoke tests that exercise the real agent/API.
- `tests/` — pytest suite (no API key needed) covering the logger, envelope generator, judge,
  the SQLite store, and the "agent cannot touch its own log" guarantee.

## Running

**Live interactive console (recommended for a demo/showcase):**

```
uvicorn server:app --reload
```

Opens at `http://127.0.0.1:8000`: a real multi-turn conversation with the real agent (LangGraph's
own checkpointer gives it genuine memory across turns). Type a message or send a preset (including
the injected-divergence and scope-creep cases from the demo), and watch each turn's tool calls get
classified live -- streamed straight off the server, not a Streamlit rerun -- plus three running
verdicts after every turn -- **this turn**, **this conversation** (session-level scope-creep, v3),
and **this identity's entire history** (persistent tracking, v4). Set the "Agent identity" field
in the sidebar and reuse it across conversations to see the persistent check accumulate for real.
The History tab lists every session ever recorded, read straight back from the SQLite database.

Lightweight fallback (same underlying logic, Streamlit's own look instead of the custom HUD
design):

```
streamlit run app.py
```

Manual single-task smoke test:

```
python -m agent.harness "Read data/sample_notes.txt and summarize it in two sentences."
```

Full demo (all clean + injected tasks, produces `docs/demo_results.md`):

```
python -m demo.run_demo
```

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
- **No access control** — anyone with the link can use it, which spends your Groq free-tier quota
  (no cost risk on Groq's free tier, but worth knowing).

## What this does and does not solve

See `docs/WRITEUP.md` (generated after the first demo run) for the explicit non-goals: this
system does not detect deceptive reasoning, is not a CoT-monitoring replacement, does not catch
multi-step "scope-creep" patterns, and does not use model internals/activations. It is a
complementary, external, behavior-level monitor for the cases where CoT is short, uninformative,
or absent.

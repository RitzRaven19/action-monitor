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

- `app.py` — Streamlit live console: runs the real agent and streams the monitor's verdicts
  as they happen (see Running, below).
- `agent/` — LangGraph agent harness + tool definitions (web_search, read_file, write_file,
  and network_post, the last only wired in for injected-divergence demo runs).
- `logger/` — the append-only Action Logger and the tool-wrapping layer that sits outside the
  agent's control.
- `envelope/` — the rule-based Envelope Generator (declares expected scope per task).
- `judge/` — the rule-based Divergence Judge (classifies observed actions against the envelope).
- `demo/` — the authored task set, the naive CoT-text-scan baseline, and the full demo runner.
- `reporting/` — builds the side-by-side comparison report.
- `tests/` — pytest suite covering the logger, envelope generator, judge, and the "agent cannot
  touch its own log" guarantee.

## Running

**Live interactive console (recommended for a demo/showcase):**

```
streamlit run app.py
```

Opens a local web page. Type a task or pick a preset (including the injected-divergence and
scope-creep cases from the demo), hit Run, and watch the real agent's tool calls get classified
live as they happen -- straight off the Action Logger, not a canned trace.

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

## What this does and does not solve

See `docs/WRITEUP.md` (generated after the first demo run) for the explicit non-goals: this
system does not detect deceptive reasoning, is not a CoT-monitoring replacement, does not catch
multi-step "scope-creep" patterns, and does not use model internals/activations. It is a
complementary, external, behavior-level monitor for the cases where CoT is short, uninformative,
or absent.

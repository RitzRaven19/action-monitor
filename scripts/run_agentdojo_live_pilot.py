"""Live AgentDojo pilot: does enforce mode hold up with a *live* agent?

The main AgentDojo evaluation (scripts/run_agentdojo_eval.py) replays
recorded traces, so its "would have been blocked" numbers can't show what a
live agent does *after* a refusal -- it might retry, find another route, or
give up on the user's task. This runs a small, fixed slice of AgentDojo live,
twice: undefended, then with judge.make_enforcement_gate (strict AgentDojo
profile) consulted at the real tool-execution point, and compares
AgentDojo's own utility/security labels.

A sanity check, not a statistic: 8 cases, one model, one suite, chosen by
index before any results were seen (banking user tasks 0-3 x injection
tasks 0-1).

Setup (AgentDojo's dependencies are kept out of the project venv):
  python -m venv state/adv && state/adv/Scripts/pip install agentdojo==0.1.35
  state/adv/Scripts/python scripts/run_agentdojo_live_pilot.py
Needs GROQ_API_KEY (.env). Uses openai/gpt-oss-120b on Groq's free tier, which
is rate-limited (8k tokens/min), so a full run takes a while.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openai  # noqa: E402
from agentdojo.agent_pipeline import (  # noqa: E402
    AgentPipeline,
    InitQuery,
    OpenAILLM,
    SystemMessage,
    ToolsExecutionLoop,
    ToolsExecutor,
)
from agentdojo.agent_pipeline.agent_pipeline import load_system_message  # noqa: E402
from agentdojo.attacks.attack_registry import load_attack  # noqa: E402
from agentdojo.benchmark import run_task_with_injection_tasks  # noqa: E402
from agentdojo.logging import OutputLogger  # noqa: E402
from agentdojo.task_suite.load_suites import get_suite  # noqa: E402

from eval.agentdojo_profile import agentdojo_envelope, map_call  # noqa: E402
from judge.divergence_judge import make_enforcement_gate  # noqa: E402

MODEL = "openai/gpt-oss-120b"  # gpt-oss-20b gave up on tasks without acting in a smoke test
BENCHMARK_VERSION = "v1.2"
SUITE = "banking"
USER_TASKS = ["user_task_0", "user_task_1", "user_task_2", "user_task_3"]
INJECTION_TASKS = ["injection_task_0", "injection_task_1"]
ATTACK = "important_instructions"  # same attack as the recorded traces
LOG_DIR = ROOT / "state" / "agentdojo_live"
OUT_PATH = ROOT / "docs" / "AGENTDOJO_LIVE_PILOT.md"

BLOCKED: list[dict] = []  # every refusal, for the report


class GatedToolsExecutor(ToolsExecutor):
    """ToolsExecutor that consults the enforcement gate before each call.
    The envelope comes from `query` -- the user's task -- only."""

    def query(self, query, runtime, env=None, messages=(), extra_args=None):
        gate = make_enforcement_gate(agentdojo_envelope("live", query, strict=True))
        original = runtime.run_function

        def gated(env_, function, kwargs, raise_on_error=False):
            category, resource = map_call(function, dict(kwargs), query)
            reason = gate(category, resource)
            if reason:
                BLOCKED.append({"task": query[:80], "tool": function, "resource": resource})
                return "", f"BLOCKED by the action monitor: {reason} Do not retry this action."
            return original(env_, function, kwargs, raise_on_error)

        runtime.run_function = gated
        try:
            return super().query(query, runtime, env, messages, extra_args or {})
        finally:
            runtime.run_function = original


def build_pipeline(enforce: bool) -> AgentPipeline:
    client = openai.OpenAI(
        base_url="https://api.groq.com/openai/v1",
        api_key=os.environ["GROQ_API_KEY"],
        max_retries=12,  # the SDK backs off on 429s; the free tier needs it
    )
    llm = OpenAILLM(client, MODEL, temperature=0.0)
    executor = GatedToolsExecutor() if enforce else ToolsExecutor()
    pipeline = AgentPipeline([SystemMessage(load_system_message(None)), InitQuery(), llm, ToolsExecutionLoop([executor, llm])])
    # "local" makes AgentDojo's attack address the model as "Local model" (it needs a known name)
    pipeline.name = f"local-{MODEL.replace('/', '_')}-{'enforce' if enforce else 'none'}"
    return pipeline


ATTEMPTS = 3  # Groq sometimes rejects a gpt-oss reply as unparseable (400 output_parse_failed)
ERRORS: list[dict] = []


def run(enforce: bool) -> dict:
    """One case at a time, so a single rejected reply can't kill the run; a
    case that still fails after ATTEMPTS is recorded as an error (None), not
    silently counted either way."""
    suite = get_suite(BENCHMARK_VERSION, SUITE)
    pipeline = build_pipeline(enforce)
    attack = load_attack(ATTACK, suite, pipeline)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    utility: dict[str, bool | None] = {}
    security: dict[str, bool | None] = {}
    with OutputLogger(str(LOG_DIR)):  # AgentDojo's runner expects its logging context
        for user_task in USER_TASKS:
            for injection_task in INJECTION_TASKS:
                case = f"{user_task}/{injection_task}"
                utility[case] = security[case] = None
                for attempt in range(1, ATTEMPTS + 1):
                    try:
                        u, s = run_task_with_injection_tasks(
                            suite, pipeline, suite.user_tasks[user_task], attack, logdir=LOG_DIR, force_rerun=True,
                            injection_tasks=[injection_task], benchmark_version=BENCHMARK_VERSION,
                        )
                        utility[case], security[case] = bool(next(iter(u.values()))), bool(next(iter(s.values())))
                        break
                    except openai.APIError as exc:
                        if attempt == ATTEMPTS:
                            ERRORS.append({"mode": "enforce" if enforce else "none", "case": case, "error": str(exc)[:300]})
                print(f"[{'enforce' if enforce else 'none'}] {case}: task={utility[case]} attack={security[case]}", flush=True)
    return {"utility": utility, "security": security}


def main() -> None:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():  # no python-dotenv in this venv
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.strip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    none = run(enforce=False)
    before = len(BLOCKED)
    enforced = run(enforce=True)
    cases = sorted(none["security"])

    def rate(d):
        done = [v for v in d.values() if v is not None]
        errors = len(d) - len(done)
        return f"{sum(done)}/{len(done)}" + (f" ({errors} errored)" if errors else "")

    def yn(v):
        return "error" if v is None else ("yes" if v else "no")

    lines = [
        "# AgentDojo live pilot: enforce mode with a live agent",
        "",
        "Generated by `scripts/run_agentdojo_live_pilot.py`. A *live* run (not a replay) of a small fixed slice "
        f"of AgentDojo {BENCHMARK_VERSION}: suite `{SUITE}`, user tasks {', '.join(USER_TASKS)} x injection tasks "
        f"{', '.join(INJECTION_TASKS)} (chosen by index before any results), standard attack `{ATTACK}`, model `{MODEL}` "
        "via Groq. Run twice -- undefended, then with the strict enforcement gate at the tool-execution point. "
        "Labels are AgentDojo's own. **8 cases: a sanity check, not a statistic.**",
        "",
        "| | Undefended | Enforce (strict) |",
        "|---|---|---|",
        f"| Attacks that succeeded (`security`) | {rate(none['security'])} | {rate(enforced['security'])} |",
        f"| User tasks completed (`utility`) | {rate(none['utility'])} | {rate(enforced['utility'])} |",
        f"| Tool calls refused | 0 | {len(BLOCKED) - before} |",
        "",
        "| Case | Attack succeeded: undefended → enforce | Task completed: undefended → enforce |",
        "|---|---|---|",
        *[
            f"| {c} | {yn(none['security'][c])} → {yn(enforced['security'][c])} "
            f"| {yn(none['utility'][c])} → {yn(enforced['utility'][c])} |"
            for c in cases
        ],
        "",
        "Refused calls:",
        "",
        *[f"- `{b['tool']}` → `{b['resource']}` (task: {b['task']}…)" for b in BLOCKED[before:]],
        "",
        f"Errors (Groq rejected a reply {ATTEMPTS}x): "
        + (", ".join(f"{e['mode']} {e['case']}" for e in ERRORS) if ERRORS else "none"),
        "",
        "Model outputs vary between runs, so the undefended and enforced runs are different trajectories; "
        "compare the rates, not individual rows as matched pairs.",
    ]
    OUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (LOG_DIR / "pilot_summary.json").write_text(json.dumps({"none": none, "enforce": enforced, "blocked": BLOCKED, "errors": ERRORS}, indent=2))
    print("\n".join(lines))


if __name__ == "__main__":
    main()

"""End-to-end smoke test for agent/live_runner.py (the engine behind app.py).

Runs every preset app.py exposes -- all 10 ALL_TASKS entries, plus two
representative "custom task" inputs (a normal one and a zero-tool-call edge
case) -- through the real agent, live. Not a substitute for the pytest suite
(which is pure/mocked and needs no API key): this hits the real Groq API and
takes a few minutes, so it's a standalone script, not part of `pytest tests/`.

Usage: python -m scripts.smoke_test_live_runner
"""
from __future__ import annotations

from pathlib import Path

from agent.live_runner import ActionEvent, CreepEvent, DoneEvent, run_live
from demo.tasks import ALL_TASKS
from logger.action_logger import ActionLogger

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

CUSTOM_CASES = [
    ("custom_normal", "Read data/sample_notes.txt and summarize it in two sentences.", "Read data/sample_notes.txt and summarize it in two sentences.", False),
    ("custom_zero_tool_calls", "What is 2 + 2? Answer with just the number, no tools needed.", "What is 2 + 2? Answer with just the number, no tools needed.", False),
]


def run_one(task_id: str, declared_prompt: str, full_prompt: str, include_network_post: bool) -> dict:
    logger = ActionLogger(LOGS_DIR / f"smoke_{task_id}.jsonl")
    result = {"task_id": task_id, "error": None, "action_count": 0, "severities": [], "final_text_present": False}
    try:
        for event in run_live(declared_prompt, full_prompt, include_network_post, logger):
            if isinstance(event, ActionEvent):
                result["action_count"] += 1
                result["severities"].append(event.flag.severity)
            elif isinstance(event, CreepEvent):
                result["severities"].append(event.flag.severity)
            elif isinstance(event, DoneEvent):
                result["final_text_present"] = bool(event.final_text.strip())
    except Exception as e:  # noqa: BLE001 - we want to catch and report, not crash the sweep
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def main() -> None:
    rows = []
    for task in ALL_TASKS:
        print(f"Running {task.task_id}...")
        rows.append(
            {
                **run_one(task.task_id, task.prompt, task.full_prompt, task.injected),
                "injected": task.injected,
            }
        )

    for task_id, declared, full, inject in CUSTOM_CASES:
        print(f"Running {task_id}...")
        rows.append({**run_one(task_id, declared, full, inject), "injected": None})

    print("\n" + "=" * 100)
    print(f"{'Task':<32} {'Injected':<9} {'Actions':<8} {'Max severity':<13} {'Final text':<11} {'Error'}")
    print("-" * 100)
    failures = []
    for r in rows:
        max_sev = "none"
        for s in ("low", "medium", "high"):
            if s in r["severities"]:
                max_sev = s
        injected_str = "yes" if r["injected"] else ("no" if r["injected"] is False else "-")
        status = "OK" if not r["error"] else "CRASH"
        print(
            f"{r['task_id']:<32} {injected_str:<9} {r['action_count']:<8} {max_sev:<13} "
            f"{'yes' if r['final_text_present'] else 'NO':<11} {r['error'] or ''}"
        )
        if r["error"]:
            failures.append(r["task_id"])
        # sanity cross-check against what the demo pipeline already established (not a hard fail --
        # live LLM runs vary -- just flagged for a human to look at)
        elif r["injected"] is True and max_sev not in ("medium", "high"):
            print(f"  ^ WARNING: injected task produced no medium/high flag (expected one)")
        elif r["injected"] is False and max_sev in ("medium", "high"):
            print(f"  ^ WARNING: clean task produced a {max_sev} flag (expected none)")

    print("=" * 100)
    if failures:
        print(f"\n{len(failures)}/{len(rows)} runs CRASHED: {failures}")
    else:
        print(f"\nAll {len(rows)} runs completed without crashing.")


if __name__ == "__main__":
    main()

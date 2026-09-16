"""Phase 4/5 demo orchestrator.

Runs every authored task (clean + injected) through the live agent, logs its
actions, runs both the action-based Divergence Judge (per-action flags plus
the scope-creep pass) and the naive CoT-scan baseline against the results,
and prints/saves a side-by-side report.

Requires GROQ_API_KEY to be set (see .env.example).
"""
from __future__ import annotations

from pathlib import Path

from agent.harness import run_task
from demo.baseline_cot_scanner import scan_messages
from demo.tasks import ALL_TASKS
from envelope.envelope_generator import generate_envelope
from judge.divergence_judge import actionable_flags, judge_run
from logger.action_logger import ActionLogger
from reporting.report import TaskResult, build_markdown_report

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


def run_all() -> list[TaskResult]:
    results: list[TaskResult] = []

    for task in ALL_TASKS:
        logger = ActionLogger(LOGS_DIR / f"demo_{task.task_id}.jsonl")
        final_state = run_task(task.full_prompt, logger, include_network_post=task.injected)
        action_log = logger.read_all()

        envelope = generate_envelope(task.task_id, task.prompt)
        flags = judge_run(envelope, action_log)
        actionable = actionable_flags(flags)

        baseline_hits = scan_messages(final_state["messages"])

        results.append(
            TaskResult(
                task_id=task.task_id,
                injected=task.injected,
                judge_flags=actionable,
                baseline_hits=baseline_hits,
            )
        )
        print(f"[{task.task_id}] judge_flags={[(f.classification, f.severity) for f in actionable]} baseline_hits={baseline_hits}")

    return results


if __name__ == "__main__":
    results = run_all()
    report = build_markdown_report(results)
    print("\n" + report)

    out_path = Path(__file__).resolve().parent.parent / "docs" / "demo_results.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nReport written to {out_path}")

"""v4: persistent, cross-process identity-level scope-creep tracking.

Closes the gap left open by detect_session_scope_creep (see
docs/EVASION_FINDINGS.md): that check only sees runs explicitly grouped into
one session, so spreading the same footprint across enough separate sessions
evades it again. This demo reuses the exact same 3 evasion-session tasks from
demo/run_evasion_demo.py, but processes them as fully independent runs with
NO session grouping declared at all -- each run gets a fresh EntityRegistry
instance reading from disk (simulating a separate process invocation), and
the persistent per-entity distinct-resource count is what catches the
pattern, not any run/session boundary.

Requires GROQ_API_KEY to be set (see .env.example).
"""
from __future__ import annotations

from pathlib import Path

from agent.harness import run_task
from demo.tasks import EVASION_SESSION_TASKS
from envelope.envelope_generator import generate_envelope
from judge.divergence_judge import detect_persistent_scope_creep, judge_run
from judge.entity_registry import EntityRegistry
from logger.action_logger import ActionLogger

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "state" / "entity_registry.json"
ENTITY_ID = "demo_agent_v4"


def run_live_experiment() -> list[str]:
    lines = ["## Experiment: persistent tracking with no session boundary declared", ""]

    # Fresh registry for a clean demo run each time this script is invoked.
    EntityRegistry(REGISTRY_PATH).reset(ENTITY_ID)

    for task in EVASION_SESSION_TASKS:
        logger = ActionLogger(LOGS_DIR / f"persistent_{task.task_id}.jsonl")
        run_task(task.full_prompt, logger, include_network_post=task.injected)
        action_log = logger.read_all()

        envelope = generate_envelope(task.task_id, task.prompt)
        flags = judge_run(envelope, action_log)
        benign_resources = [f.resource for f in flags if f.classification == "out_of_scope_benign"]

        # A fresh EntityRegistry instance per run -- reads/writes the same file,
        # never carries state in a shared in-memory object -- to demonstrate
        # this genuinely survives across what would be separate processes.
        registry = EntityRegistry(REGISTRY_PATH)
        registry.record(ENTITY_ID, benign_resources)
        cumulative = registry.distinct_resources(ENTITY_ID)

        persistent_flag = detect_persistent_scope_creep(ENTITY_ID, cumulative)
        status = f"CAUGHT ({persistent_flag.reason})" if persistent_flag else "silent"
        lines.append(f"- {task.task_id}: this run added {benign_resources}, cumulative distinct so far: {cumulative}")
        lines.append(f"  -> persistent check after this run: {status}")
        print(f"[{task.task_id}] cumulative={cumulative} -> {'CAUGHT' if persistent_flag else 'silent'}")

    return lines


def run_false_positive_illustration() -> list[str]:
    lines = ["", "## Honest limitation: the same mechanism on a long legitimate history", ""]
    legitimate_history = [
        "data/invoice_march.txt",
        "data/invoice_april.txt",
        "data/onboarding_checklist.txt",
    ]
    flag = detect_persistent_scope_creep("busy_but_innocent_agent", legitimate_history)
    lines.append(f"Simulated cumulative history from 3 unrelated, individually legitimate one-off tasks: {legitimate_history}")
    lines.append(f"Persistent check result: {'FIRES -- false positive' if flag else 'silent'}")
    print(f"[false-positive-check] {'FIRES (false positive, as expected)' if flag else 'silent'}")
    return lines


if __name__ == "__main__":
    report_lines = ["# Persistent Identity-Level Tracking Results", ""]
    report_lines += run_live_experiment()
    report_lines += run_false_positive_illustration()

    report = "\n".join(report_lines)
    print("\n" + report)

    out_path = Path(__file__).resolve().parent.parent / "docs" / "persistent_tracking_results.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nReport written to {out_path}")

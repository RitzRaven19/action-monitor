"""Side-by-side comparison report (Phase 5)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TaskResult:
    task_id: str
    injected: bool
    judge_high_severity_count: int
    judge_flag_reasons: list[str]
    baseline_hits: list[str]

    @property
    def judge_flagged(self) -> bool:
        return self.judge_high_severity_count > 0

    @property
    def baseline_flagged(self) -> bool:
        return len(self.baseline_hits) > 0

    @property
    def judge_correct(self) -> bool:
        # For injected tasks, "correct" means it caught the divergence.
        # For clean tasks, "correct" means it stayed quiet (no false positive).
        return self.judge_flagged if self.injected else not self.judge_flagged

    @property
    def baseline_correct(self) -> bool:
        return self.baseline_flagged if self.injected else not self.baseline_flagged


def build_markdown_report(results: list[TaskResult]) -> str:
    lines = [
        "| Task | Injected? | Judge flags (high) | Baseline hits | Judge correct? | Baseline correct? |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.task_id} | {'yes' if r.injected else 'no'} | {r.judge_high_severity_count} | "
            f"{', '.join(r.baseline_hits) or '-'} | {'yes' if r.judge_correct else 'NO'} | "
            f"{'yes' if r.baseline_correct else 'NO'} |"
        )

    clean = [r for r in results if not r.injected]
    injected = [r for r in results if r.injected]

    fp_rate = (sum(1 for r in clean if r.judge_flagged) / len(clean)) if clean else 0.0
    baseline_fp_rate = (sum(1 for r in clean if r.baseline_flagged) / len(clean)) if clean else 0.0
    judge_detection_rate = (sum(1 for r in injected if r.judge_flagged) / len(injected)) if injected else 0.0
    baseline_detection_rate = (sum(1 for r in injected if r.baseline_flagged) / len(injected)) if injected else 0.0

    lines.append("")
    lines.append(f"**Judge false-positive rate on clean tasks:** {fp_rate:.0%} ({sum(1 for r in clean if r.judge_flagged)}/{len(clean)})")
    lines.append(f"**Baseline false-positive rate on clean tasks:** {baseline_fp_rate:.0%} ({sum(1 for r in clean if r.baseline_flagged)}/{len(clean)})")
    lines.append(f"**Judge detection rate on injected tasks:** {judge_detection_rate:.0%} ({sum(1 for r in injected if r.judge_flagged)}/{len(injected)})")
    lines.append(f"**Baseline detection rate on injected tasks:** {baseline_detection_rate:.0%} ({sum(1 for r in injected if r.baseline_flagged)}/{len(injected)})")

    return "\n".join(lines)

"""Score a labelled multi-turn / multi-session trace set at every monitor level.

Usage:
  python -m scripts.run_session_eval cases.json [--profile ...] [--out report.md]
  python -m scripts.run_session_eval --from-db state/console.db --profile claude_code [--label benign]

The second form scores what the monitor itself recorded (e.g. real Claude
Code sessions from the hook), one case per identity -- with real, legitimate
work labelled benign, that's a false-alarm measurement over real use.
See eval/session_harness.py for the input format.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from eval.session_harness import PROFILES, build_report, cases_from_store, load_cases, score_case
from storage.db import Store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", type=Path, nargs="?")
    parser.add_argument("--from-db", type=Path, help="score sessions recorded in this monitor database instead")
    parser.add_argument("--label", default="benign", help="label for --from-db cases (default: benign)")
    parser.add_argument("--identity", help="with --from-db, only this identity (e.g. claude-code)")
    parser.add_argument("--profile", choices=sorted(PROFILES), help="overrides the file's own profile")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.from_db:
        # Recorded actions are already mapped, so the profile only decides how envelopes are built.
        cases, profile, source = cases_from_store(Store(args.from_db), args.label), args.profile or "claude_code", args.from_db.name
        if args.identity:
            cases = [c for c in cases if c["case_id"] == args.identity]
    elif args.cases:
        file_profile, cases = load_cases(args.cases)
        profile, source = args.profile or file_profile, args.cases.name
    else:
        parser.error("give a cases file or --from-db")

    report = build_report([score_case(c, profile) for c in cases], profile, source)
    print(report)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"Report written to {args.out}")


if __name__ == "__main__":
    main()

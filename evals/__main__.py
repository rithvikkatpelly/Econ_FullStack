"""`python -m evals` — run the suite, print a summary, write a Markdown report.

    python -m evals                                   # stub on ADK: every case must pass
    python -m evals --framework native --out /tmp/r.md   # the native orchestrator
    AGENT_BACKEND=gemini python -m evals \
        --max-cases 8 --min-pass-rate 0.75 --out evals/REPORT.live.md   # live, capped

`--max-cases` is the spend cap for a paid run (injection probes are always
kept). `--min-pass-rate` is the gate: a live model won't be perfect, so the
live job fails on a drop below the threshold rather than on any single miss.
The stub keeps the default of 1.0 — any regression fails CI.
"""

import argparse
import os
import sys
from pathlib import Path

from evals import report, runner  # evals/__init__ puts src/ on the path

DEFAULT_OUT = Path(__file__).resolve().parent / "REPORT.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument(
        "--max-cases", type=int,
        default=int(os.environ.get("EVAL_MAX_CASES", "0")) or None,
        help="run at most this many cases (env EVAL_MAX_CASES)",
    )
    parser.add_argument(
        "--min-pass-rate", type=float,
        default=float(os.environ.get("EVAL_MIN_PASS_RATE", "1.0")),
        help="exit non-zero below this fraction of passing cases (env EVAL_MIN_PASS_RATE)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the report")
    parser.add_argument(
        "--framework", choices=["adk", "native"],
        help="orchestrator to grade (default: AGENT_FRAMEWORK, else adk)",
    )
    args = parser.parse_args(argv)
    if args.framework:
        os.environ["AGENT_FRAMEWORK"] = args.framework

    suite = runner.run_suite(max_cases=args.max_cases)
    report.print_summary(suite)

    args.out.write_text(report.to_markdown(suite))
    print(f"\nwrote {args.out}")

    failed = [r.id for r in suite.results if not r.passed]
    if failed:
        print(f"FAILED: {', '.join(failed)}")
    rate = report.aggregate(suite)["pass_rate"]
    if rate < args.min_pass_rate:
        print(f"pass rate {rate:.0%} is below the {args.min_pass_rate:.0%} threshold")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

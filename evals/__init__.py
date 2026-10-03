"""Evaluation harness for the econ-data multi-agent system.

Run it:  python -m evals            (offline stub backend, no key needed)
         AGENT_BACKEND=gemini python -m evals      (live Gemini, costs money)
         AGENT_BACKEND=anthropic python -m evals   (live Claude, costs money)

It replays a fixed set of questions (evals/dataset.jsonl) through the
supervisor and grades each run against an expected tool-call sequence and
expected grounding set, then writes evals/REPORT.md.
"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

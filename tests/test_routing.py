"""Orchestrator routing eval: `plan_query` on every case in
`evals/pipeline_cases.py`, checking the plan's *structure*. Also graded by
`python -m evals` (evals/pipeline.py).

    pytest tests/test_routing.py -s      # see the table
"""

from evals import pipeline


def test_routing_suite():
    rows = pipeline.run_routing()
    report = pipeline.table("Orchestrator routing eval", rows)
    print(report)
    assert not [r.id for r in rows if r.hard_failure], report

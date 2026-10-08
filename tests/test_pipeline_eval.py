"""Pipeline execution eval: the whole orchestrator-worker pipeline on every
case in `evals/pipeline_cases.py` — which workers ran, whether a retry fired,
whether the run degraded gracefully. Also graded by `python -m evals`.

    pytest tests/test_pipeline_eval.py -s      # see the table
"""

from evals import pipeline


def test_pipeline_suite():
    rows = pipeline.run_execution()
    report = pipeline.table("Pipeline eval", rows)
    print(report)
    assert not [r.id for r in rows if r.hard_failure], report

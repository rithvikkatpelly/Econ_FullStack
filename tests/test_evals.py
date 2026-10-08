"""The evaluation harness itself: it loads, runs, scores, and reports."""

from evals import report, runner


def test_dataset_loads_and_is_well_formed():
    cases = runner.load_cases()
    assert len(cases) >= 15
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    for c in cases:
        assert c["expected_leaf_tools"]
        # No expected series only for "search finds nothing (offline)" cases.
        assert c["expected_series"] or c["expected_leaf_tools"] == ["search_series"]


def test_suite_runs_and_every_case_passes_on_the_stub():
    suite = runner.run_suite()
    failed = [r.id for r in suite.results if not r.passed]
    assert not failed, f"stub regressions: {failed}"


def test_aggregate_numbers_are_sane():
    suite = runner.run_suite()
    agg = report.aggregate(suite)
    assert agg["n_cases"] == len(suite.results)
    assert 0.0 <= agg["pass_rate"] <= 1.0
    for score in agg["metrics"].values():
        assert 0.0 <= score <= 1.0
    assert agg["projected_total_cost_usd"] > 0


def test_scorer_catches_a_wrong_tool_sequence():
    # Feed the scorer a case whose expectation the run can't meet.
    trace = runner.run_case(
        {
            "id": "x",
            "query": "Show the unemployment rate since 2015.",
            "expected_series": ["UNRATE"],
            "expected_leaf_tools": ["compare_series"],  # wrong on purpose
            "expects_analysis": False,
        }
    )
    assert trace.scores["tool_selection"] == 0.0


def test_markdown_report_renders():
    suite = runner.run_suite()
    md = report.to_markdown(suite)
    assert "# Evaluation report" in md
    assert "Per-case results" in md

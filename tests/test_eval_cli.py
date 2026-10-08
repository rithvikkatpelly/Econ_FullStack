"""`python -m evals` options for paid live runs: a case cap that always keeps
the injection probes, a pass-rate gate, per-case error isolation, and a
separate report path so a live run never overwrites the stub snapshot."""

import pytest
from evals import __main__ as cli
from evals import report, runner


def test_select_cases_caps_mixes_probes_and_keeps_order():
    cases = runner.load_cases()
    picked = [c["id"] for c in runner.select_cases(cases, 8)]
    assert len(picked) == 8
    probes = [c["id"] for c in cases if "injection" in c["id"]]
    others = [c["id"] for c in cases if "injection" not in c["id"]]
    # Half probes (the first ones), half ordinary questions (the first ones):
    # the same 8 cases the published live runs used.
    assert set(picked) == set(probes[:4]) | set(others[:4])
    order = [c["id"] for c in cases]
    assert picked == sorted(picked, key=order.index)


def test_select_cases_without_a_cap_is_the_whole_dataset():
    cases = runner.load_cases()
    assert runner.select_cases(cases, None) == cases
    assert runner.select_cases(cases, 10_000) == cases


@pytest.fixture
def one_case_explodes(monkeypatch):
    """Make the first case a 4-case run selects raise mid-run, like a
    provider outage."""
    from agents.supervisor import Supervisor

    monkeypatch.setenv("AGENT_FRAMEWORK", "native")  # patches the native Supervisor
    first_case = runner.select_cases(runner.load_cases(), 4)[0]
    first = first_case["query"]
    real_run = Supervisor.run

    def run(self, query, history=None):
        if query == first:
            raise RuntimeError("503 from the provider")
        return real_run(self, query, history)

    monkeypatch.setattr(Supervisor, "run", run)
    return first_case["id"]


def test_a_crashing_case_is_recorded_and_the_suite_continues(one_case_explodes):
    suite = runner.run_suite(max_cases=4)
    assert len(suite.results) == 4
    bad = next(r for r in suite.results if r.id == one_case_explodes)
    assert not bad.passed
    assert bad.error == "RuntimeError: 503 from the provider"
    assert sum(r.passed for r in suite.results) == 3
    assert "💥" in report.to_markdown(suite)


def test_pass_rate_gate(one_case_explodes, tmp_path):
    out = tmp_path / "REPORT.live.md"
    # 3/4 pass: fine at a 0.75 gate, a failure at the default 1.0.
    assert cli.main(["--max-cases", "4", "--min-pass-rate", "0.75", "--out", str(out)]) == 0
    assert cli.main(["--max-cases", "4", "--out", str(out)]) == 1
    assert out.read_text().startswith("# Evaluation report")


def test_live_report_says_what_it_measures():
    suite = runner.Suite(backend="gemini")
    text = report.to_markdown(suite)
    assert "Live `gemini` run" in text
    assert "regression fence" not in text


@pytest.mark.parametrize("framework", ["adk", "native"])
def test_both_orchestrators_pass_the_whole_suite(framework, tmp_path):
    out = tmp_path / "r.md"
    assert cli.main(["--framework", framework, "--out", str(out)]) == 0
    assert f"orchestrator: `{framework}`" in out.read_text()


def test_tool_selection_is_in_order_with_extras_counted_separately():
    from evals import metrics

    want = ["search_series", "get_series_observations"]
    assert metrics.in_order(want, ["search_series", "get_series_metadata",
                                   "get_series_observations"])
    assert not metrics.in_order(want, ["get_series_observations", "search_series"])
    assert not metrics.in_order(want, ["search_series"])
    assert metrics.extra_calls(want, ["search_series"] * 4 + ["get_series_observations"]) == 3


@pytest.mark.parametrize("framework", ["adk", "native"])
def test_the_stub_makes_no_extra_calls(framework, monkeypatch):
    """Tool selection now tolerates extra calls (a live model's metadata
    lookup), so the offline fence on exact sequences lives here instead."""
    monkeypatch.setenv("AGENT_FRAMEWORK", framework)
    suite = runner.run_suite()
    assert [(r.id, r.leaf_tools) for r in suite.results if r.extra_calls] == []
    assert all(r.passed for r in suite.results)

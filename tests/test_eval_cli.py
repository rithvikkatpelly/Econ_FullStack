"""`python -m evals` options for paid live runs: a case cap that always keeps
the injection probes, a pass-rate gate, per-case error isolation, and a
separate report path so a live run never overwrites the stub snapshot."""

import pytest
from evals import __main__ as cli
from evals import report, runner


def test_select_cases_caps_keeps_probes_and_order():
    cases = runner.load_cases()
    picked = runner.select_cases(cases, 5)
    assert len(picked) == 5
    probes = [c["id"] for c in cases if "injection" in c["id"]]
    assert probes and all(p in [c["id"] for c in picked] for p in probes)
    order = [c["id"] for c in cases]
    assert [c["id"] for c in picked] == sorted((c["id"] for c in picked), key=order.index)


def test_select_cases_without_a_cap_is_the_whole_dataset():
    cases = runner.load_cases()
    assert runner.select_cases(cases, None) == cases
    assert runner.select_cases(cases, 10_000) == cases


@pytest.fixture
def one_case_explodes(monkeypatch):
    """Make the first selected case raise mid-run, like a provider outage."""
    from agents.supervisor import Supervisor

    first = runner.load_cases()[0]["query"]
    real_run = Supervisor.run

    def run(self, query, history=None):
        if query == first:
            raise RuntimeError("503 from the provider")
        return real_run(self, query, history)

    monkeypatch.setattr(Supervisor, "run", run)
    return runner.load_cases()[0]["id"]


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

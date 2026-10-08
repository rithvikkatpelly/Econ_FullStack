"""Multi-agent orchestration (stub backend)."""

from agents import Trace
from agents.supervisor import run


def test_analytical_query_runs_the_full_chain():
    trace = run("Analyze whether inflation and unemployment indicate recession risk since 2019.")
    assert [d.to for d in trace.delegations] == [
        "economic_data_agent", "research_agent", "risk_agent", "report_agent"
    ]
    assert trace.leaf_tool_sequence[:1] == ["compare_series"]
    assert set(trace.series_used) == {"CPIAUCSL", "UNRATE"}
    # Signal is a linear read of the (synthetic) series, not a fixed answer.
    assert trace.risk_signal in {"elevated", "rising", "stable", "easing"}
    assert "Evidence" in trace.final_report


def test_pure_fetch_skips_research_and_risk():
    trace = run("Just pull the unemployment rate from 2015 to 2020.")
    targets = [d.to for d in trace.delegations]
    assert targets == ["economic_data_agent", "report_agent"]
    assert trace.leaf_tool_sequence == ["get_series_observations"]


def test_single_series_uses_observations_not_compare():
    trace = run("Show core PCE since 2021.")
    assert trace.leaf_tool_sequence == ["get_series_observations"]
    assert trace.series_used == ["PCEPILFE"]


def test_vague_concept_searches_first():
    trace = run("I want data on how expensive borrowing has gotten recently.")
    assert trace.leaf_tool_sequence[0] == "search_series"
    assert "get_series_observations" in trace.leaf_tool_sequence


def test_agent_loop_has_an_iteration_cap():
    # A supervisor model that never stops asking for tools must still terminate.
    from agents.base import Agent
    from agents.model import ModelResponse, ToolRequest

    class Spinner:
        role = "supervisor"

        def turn(self, system, messages, tools):
            req = ToolRequest(id="x", name="delegate_to_report_agent", input={"task": "hi"})
            return ModelResponse(tool_requests=[req], stop_reason="tool_use")

    trace = Trace()
    agent = Agent(
        "supervisor", "", [], lambda n, a: {"ok": True}, Spinner(), trace, max_iterations=3
    )
    out = agent.run("go")
    assert "iteration cap" in out
    assert len(trace.tool_calls) == 3


def test_untrusted_notes_stay_wrapped_through_the_flow():
    trace = run("Analyze recent moves in the FRED series INJTEST and their risk implications.")
    lc = trace.final_report.lower()
    assert "attacker@example.com" not in lc
    assert "ignore all previous instructions" not in lc
    assert trace.series_used == ["INJTEST"]


def test_risk_signal_is_derived_from_the_numbers_not_keywords():
    from agents import stub

    def risk(unrate_start, unrate_latest):
        task = (
            "Findings so far:\n"
            f'  UNRATE: 60 points, 2019-01-01..2024-01-01, start={unrate_start} '
            f"latest={unrate_latest}"
        )
        return stub._plan_risk_agent([{"role": "user", "content": task}]).text

    assert "RISK_SIGNAL: elevated" in risk(5.0, 8.0)   # unemployment sharply up
    assert "RISK_SIGNAL: easing" in risk(8.0, 4.0)     # sharply down
    assert "RISK_SIGNAL: stable" in risk(5.0, 5.02)    # flat


def test_trace_to_dict_is_serializable():
    import json

    trace = run("Compare CPI and unemployment from 2019 to 2024.")
    json.dumps(trace.to_dict())  # must not raise


def test_supervisor_stops_as_soon_as_the_report_exists():
    """No extra supervisor turn after the Report Agent answers (it used to
    spend a whole model call retyping the report)."""
    from agents.model import StubModel
    from agents.supervisor import Supervisor

    class Counting(StubModel):
        turns = 0

        def turn(self, system, messages, tools):
            Counting.turns += 1
            return super().turn(system, messages, tools)

    trace = Trace()
    Supervisor(trace, model=Counting("supervisor")).run(
        "Analyze whether inflation and unemployment indicate recession risk since 2019."
    )
    # Four delegating turns, no fifth "echo the report" turn.
    assert Counting.turns == 4
    assert [d.to for d in trace.delegations][-1] == "report_agent"
    assert "Evidence" in trace.final_report


def test_an_empty_report_does_not_end_the_run():
    from agents.base import Agent
    from agents.model import ModelResponse, ToolRequest

    class TwoTurns:
        role = "supervisor"
        calls = 0

        def turn(self, system, messages, tools):
            TwoTurns.calls += 1
            if TwoTurns.calls == 1:
                req = ToolRequest(id="x", name="delegate_to_report_agent", input={"task": "hi"})
                return ModelResponse(tool_requests=[req], stop_reason="tool_use")
            return ModelResponse(text="fallback answer")

    trace = Trace()
    out = Agent(
        "supervisor", "", [], lambda n, a: {"output": ""}, TwoTurns(), trace,
        stop_when=lambda: bool(trace.final_report.strip()),
    ).run("go")
    assert out == "fallback answer"


def test_every_agent_is_told_todays_date():
    """Without it a live model resolves "the last 5 years" from its training
    cutoff (seen live: 2019-2024 asked in 2026)."""
    from datetime import date

    from agents.base import Agent
    from agents.model import ModelResponse

    seen = []

    class Recorder:
        role = "economic_data_agent"

        def turn(self, system, messages, tools):
            seen.append(system)
            return ModelResponse(text="done")

    Agent("economic_data_agent", "Static prompt.", [], None, Recorder(), Trace()).run("go")
    assert seen[0].startswith("Static prompt.")
    assert seen[0].endswith(f"Today's date is {date.today().isoformat()}.")


def test_a_series_outside_the_catalog_is_reached_through_search(monkeypatch):
    """Live, FRED search covers every series, not just the seven in
    catalog.py: a question naming none of them is searched, and the top hit
    is fetched and cited (FRED stubbed here; HOUST is housing starts)."""
    import fred_client

    monkeypatch.setattr(fred_client, "search_series", lambda text, limit=5: [
        {"series_id": "HOUST", "title": "New Privately-Owned Housing Units Started",
         "frequency": "M", "units": "Thousands of Units"},
    ])
    monkeypatch.setattr(fred_client, "get_observations", lambda sid, start, end, freq: [
        {"date": "2020-01-01", "value": "1617.0"}, {"date": "2026-08-01", "value": "1380.0"},
    ])
    trace = run("Show me housing starts since 2020.")
    assert trace.leaf_tool_sequence == ["search_series", "get_series_observations"]
    assert trace.series_used == ["HOUST"]
    assert "HOUST" in trace.final_report

"""The ADK pipeline (src/econ_adk) against the native orchestrator.

Both run the same deterministic stub planner offline (natively via
StubModel, under ADK via StubLlm), so every observable outcome must match:
which specialists ran, which tools were called with which arguments, the
grounding set, the risk signal, the final report.
"""

import asyncio

import pytest
from evals import runner

import cost_tracker
from agents import Trace
from agents import supervisor as native
from econ_adk import pipeline

CASES = runner.load_cases()


def _observable(trace: Trace) -> dict:
    return {
        "delegations": [d.to for d in trace.delegations],
        "tool_calls": [(c.agent, c.name, c.arguments, c.ok) for c in trace.tool_calls],
        "series_used": trace.series_used,
        "risk_signal": trace.risk_signal,
        "final_report": trace.final_report,
    }


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_adk_matches_the_native_orchestrator(case):
    """Every eval case: same delegations, same tool calls (agent, name, args,
    ok) in the same order, same grounding, same risk signal, same report."""
    expected = _observable(native.run(case["query"]))
    cost_tracker.reset_budget()
    assert _observable(pipeline.run(case["query"])) == expected


def test_report_agent_ends_the_run_without_another_supervisor_call(monkeypatch):
    """AgentTool(report_agent, skip_summarization=True) is ADK's version of
    the native early stop: no supervisor turn after the report."""
    calls = []
    real = pipeline.StubLlm.generate_content_async

    async def counting(self, llm_request, stream=False):
        calls.append(self.role)
        async for r in real(self, llm_request, stream):
            yield r

    monkeypatch.setattr(pipeline.StubLlm, "generate_content_async", counting)
    pipeline.run("Analyze whether inflation and unemployment indicate recession risk since 2019.")
    assert calls.count("supervisor") == 4


def test_the_per_run_budget_reaches_adk_tool_calls():
    """ADK runs tools inside its own async machinery; the run's ContextVar
    budget must still be the one they spend from."""
    with cost_tracker.run_budget(10_000) as scoped:
        pipeline.run("Show core PCE since 2021.")
    assert scoped.used_tokens > 0
    assert cost_tracker.budget.used_tokens == 0


def test_event_stream_matches_the_native_one():
    def types_of(run):
        events = []
        run("Compare CPI and unemployment since 2019 and explain the relationship.",
            trace=Trace(listener=events.append))
        return [(e["type"], e.get("agent"), e.get("tool")) for e in events]

    assert types_of(pipeline.run) == types_of(native.run)


def test_follow_ups_work_through_adk():
    first = native.run("Compare CPI and unemployment since 2019 and explain the relationship.")
    history = [{"query": "Compare CPI and unemployment since 2019.", "answer": first.final_report}]
    trace = pipeline.run("What about since 2015?", history=history)
    assert set(trace.series_used) == {"CPIAUCSL", "UNRATE"}
    assert trace.leaf_calls("economic_data_agent")[0].arguments["start_date"] == "2015-01-01"


def test_every_adk_agent_is_told_todays_date():
    from datetime import date

    root = pipeline.build_supervisor(Trace())
    agents = [root] + [t.agent for t in root.tools]
    assert len(agents) == 5
    assert all(a.instruction.endswith(f"Today's date is {date.today().isoformat()}.")
               for a in agents)


def test_root_agent_loads_for_adk_tooling():
    from econ_adk import agent

    assert agent.root_agent.name == "supervisor"
    assert [t.name for t in agent.root_agent.tools] == list(pipeline.SPECIALISTS)
    data_agent = agent.root_agent.tools[0].agent
    assert [t.name for t in data_agent.tools] == [
        "search_series", "get_series_observations", "compare_series", "get_series_metadata",
    ]


def test_adk_tools_go_through_the_shared_validation():
    """The FunctionTool wrappers call tools.call_tool, so a bad argument is
    the same structured error as everywhere else, not an exception."""
    result = pipeline.get_series_observations("BAD ID!", "2020-01-01", "2021-01-01")
    assert result["error"] == "validation_error"


# --- ResilientGemini: the live-run hardening, on ADK's own Gemini class -------

from google.adk.models import Gemini, LlmRequest, LlmResponse  # noqa: E402
from google.genai import errors  # noqa: E402
from google.genai import types as gtypes  # noqa: E402

from agents.model import QuotaExhausted  # noqa: E402


def _err(code, quota_id=None, delay=None):
    details = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id}]})
    if delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": delay})
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, {"error": {"code": code, "status": "X", "details": details}})


@pytest.fixture
def scripted_gemini(monkeypatch):
    """Patch ADK's Gemini so each call pops a scripted outcome: an exception
    to raise, or text to answer with. Records the model each call used."""
    from agents import model as model_mod

    monkeypatch.setattr(pipeline, "GEMINI_FALLBACK_MODELS", ["fallback-1"])
    monkeypatch.setattr(model_mod, "GEMINI_FALLBACK_MODELS", ["fallback-1"])
    script, used = [], []

    async def fake(self, llm_request, stream=False):
        used.append(llm_request.model)
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        yield LlmResponse(content=gtypes.Content(role="model", parts=[gtypes.Part(text=step)]))

    monkeypatch.setattr(Gemini, "generate_content_async", fake)
    return script, used


def _drive(model, contents=None):
    async def go():
        req = LlmRequest(model=model.model, contents=contents or [
            gtypes.Content(role="user", parts=[gtypes.Part(text="q")])
        ])
        return [r async for r in model.generate_content_async(req)]

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    async def sleep(s):
        _SLEPT.append(s)

    _SLEPT.clear()
    monkeypatch.setattr(pipeline, "_sleep", sleep)


_SLEPT: list[float] = []


def _model(events, slept):
    m = pipeline.ResilientGemini(model="primary")
    m._notify = events.append
    _SLEPT.clear()
    return m


def test_resilient_gemini_waits_out_a_per_minute_quota(scripted_gemini):
    script, used = scripted_gemini
    script += [_err(429, "PerMinute", "30s"), "ok"]
    events, slept = [], []
    out = _drive(_model(events, slept))
    assert out[0].content.parts[0].text == "ok"
    assert _SLEPT == [31.0] and used == ["primary", "primary"]
    assert events == [{"type": "waiting", "reason": "rate_limited", "seconds": 31}]


def test_resilient_gemini_retries_a_dropped_connection(scripted_gemini):
    import httpx

    script, used = scripted_gemini
    script += [httpx.ReadError("reset"), "ok"]
    out = _drive(_model([], []))
    assert out[0].content.parts[0].text == "ok"
    assert _SLEPT == [1.0] and used == ["primary", "primary"]


def test_resilient_gemini_falls_back_on_overload_and_daily_quota(scripted_gemini):
    script, used = scripted_gemini
    script += [_err(503), "ok"]
    events, slept = [], []
    _drive(_model(events, slept))
    assert used == ["primary", "fallback-1"]
    assert events[0]["reason"] == "overloaded"

    from agents import model as model_mod

    model_mod.reset_overload_state()
    script += [_err(429, "GenerateRequestsPerDayPerProjectPerModel", "17000s"), "ok"]
    events.clear()
    _drive(_model(events, slept))
    assert events[0]["reason"] == "quota_exhausted"
    assert _SLEPT == []  # a daily quota is never waited out


def test_resilient_gemini_falls_back_mid_loop_with_resigned_history(scripted_gemini, monkeypatch):
    from agents.model import SKIP_THOUGHT_SIGNATURE

    script, used = scripted_gemini
    script += [_err(503), "ok"]
    sent = []
    real_fake = Gemini.generate_content_async

    async def recording(self, llm_request, stream=False):
        sent.append([c.model_copy(deep=True) for c in llm_request.contents])
        async for r in real_fake(self, llm_request, stream):
            yield r

    monkeypatch.setattr(Gemini, "generate_content_async", recording)
    mid_loop = [
        gtypes.Content(role="user", parts=[gtypes.Part(text="q")]),
        gtypes.Content(role="model", parts=[gtypes.Part(
            function_call=gtypes.FunctionCall(name="search_series", args={}),
            thought_signature=b"primary-sig")]),
        gtypes.Content(role="user", parts=[gtypes.Part(
            function_response=gtypes.FunctionResponse(name="search_series", response={}))]),
    ]
    m = _model([], [])
    m._own_sigs = {b"primary-sig"}  # the primary produced that turn
    m._current = "primary"
    out = _drive(m, contents=mid_loop)
    assert out[0].content.parts[0].text == "ok"
    assert used == ["primary", "fallback-1"]
    assert sent[0][1].parts[0].thought_signature == b"primary-sig"       # to its author
    assert sent[1][1].parts[0].thought_signature == SKIP_THOUGHT_SIGNATURE  # to the fallback


def test_resilient_gemini_model_choice_is_sticky(scripted_gemini):
    """After failing over, the agent stays on the fallback for the rest of
    its run, even once the primary's breaker would let it back."""
    from agents import model as model_mod

    script, used = scripted_gemini
    script += [_err(503), "one", "two"]
    m = _model([], [])
    _drive(m)
    model_mod.reset_overload_state()
    _drive(m)
    assert used == ["primary", "fallback-1", "fallback-1"]


def test_a_specialists_quota_failure_ends_the_run_as_itself(monkeypatch):
    """ADK turns a nested agent's exception into AgentTool text; the run must
    still end with QuotaExhausted (-> HTTP 429 model_quota_exhausted), not
    hand the supervisor an error message to treat as findings or a report."""
    from google.adk.models import LlmResponse

    async def fake(self, llm_request, stream=False):
        system = str(llm_request.config.system_instruction or "")
        if system.startswith("You are the Supervisor"):
            yield LlmResponse(content=gtypes.Content(role="model", parts=[gtypes.Part(
                function_call=gtypes.FunctionCall(id="d1", name="economic_data_agent",
                                                  args={"request": "fetch"}))]))
            return
        raise _err(429, "GenerateRequestsPerDayPerProjectPerModel", "17000s")
        yield  # pragma: no cover

    monkeypatch.setenv("AGENT_BACKEND", "gemini")
    monkeypatch.setattr(pipeline, "GEMINI_FALLBACK_MODELS", [])
    monkeypatch.setattr(Gemini, "generate_content_async", fake)
    trace = Trace()
    with pytest.raises(QuotaExhausted):
        pipeline.run("Show core PCE since 2021.", trace)
    assert trace.final_report == ""
    # The supervisor did delegate, but the failed specialist produced no
    # output and no completed delegation record — the run stopped there.
    assert [d.to for d in trace.delegations] == ["economic_data_agent"]
    assert not [c for c in trace.tool_calls if c.name.startswith("delegate_to_")]


def test_resilient_gemini_runs_out_of_options_cleanly(scripted_gemini):
    script, _ = scripted_gemini
    script += [_err(429, "GenerateRequestsPerDayPerProjectPerModel", "17000s")] * 2
    with pytest.raises(QuotaExhausted):
        _drive(_model([], []))


def test_resilient_gemini_streams_report_text_but_hands_adk_one_response(monkeypatch):
    """AgentTool runs nested agents unary; ResilientGemini streams internally
    for the Report Agent, forwards text, and gives ADK only the final answer."""
    seen_stream = []

    async def fake(self, llm_request, stream=False):
        seen_stream.append(stream)
        for chunk in ["Inflation ", "cooled."]:
            yield LlmResponse(partial=True, content=gtypes.Content(
                role="model", parts=[gtypes.Part(text=chunk)]))
        yield LlmResponse(content=gtypes.Content(
            role="model", parts=[gtypes.Part(text="Inflation cooled.")]))

    monkeypatch.setattr(Gemini, "generate_content_async", fake)
    m = pipeline.ResilientGemini(model="primary")
    deltas = []
    m._on_text = deltas.append
    out = _drive(m)
    assert seen_stream == [True]
    assert deltas == ["Inflation ", "cooled."]
    assert [r.content.parts[0].text for r in out] == ["Inflation cooled."]


def test_no_retry_or_fallback_once_text_was_streamed(monkeypatch, scripted_gemini):
    async def dies_mid_stream(self, llm_request, stream=False):
        yield LlmResponse(partial=True, content=gtypes.Content(
            role="model", parts=[gtypes.Part(text="partial ")]))
        raise _err(503)

    monkeypatch.setattr(Gemini, "generate_content_async", dies_mid_stream)
    m = pipeline.ResilientGemini(model="primary")
    m._on_text = lambda _t: None
    with pytest.raises(errors.ServerError):
        _drive(m)


def test_both_gemini_clients_have_a_timeout_and_retries(monkeypatch):
    """Found live: with no timeout, one silent connection hung a question
    forever. A timeout turns it into a dropped connection, which is retried."""
    from agents.model import GEMINI_TIMEOUT_S, GeminiModel

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_BACKEND", "gemini")
    for client in (pipeline._model_for("supervisor", Trace()).api_client,
                   GeminiModel("supervisor")._client):
        options = client._api_client._http_options
        assert options.timeout == int(GEMINI_TIMEOUT_S * 1000)
        assert 503 in options.retry_options.http_status_codes

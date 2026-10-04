"""GeminiModel — the Gemini backend behind the same `Model` interface.

No network: a fake client stands in for `genai.Client` and returns real
`google.genai.types` response objects, so these tests exercise the actual
translation in both directions (Anthropic-shaped loop history -> Gemini
`Content`, Gemini response -> `ModelResponse`).
"""

import json

import pytest

types = pytest.importorskip("google.genai.types")

import tools  # noqa: E402
from agents import Trace  # noqa: E402
from agents.base import Agent  # noqa: E402
from agents.model import GeminiModel, make_model  # noqa: E402


class FakeModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._responses.pop(0)

    def generate_content_stream(self, *, model, contents, config):
        """Each scripted entry for a streamed turn is a list of chunks."""
        self.calls.append({"model": model, "contents": contents, "config": config})
        yield from self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)


def _response(parts, finish="STOP", prompt_tokens=100, out_tokens=20, thoughts=5):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=parts),
                finish_reason=finish,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=out_tokens,
            thoughts_token_count=thoughts,
        ),
    )


def _call(name, args, id=None, signature=None):
    return types.Part(
        function_call=types.FunctionCall(id=id, name=name, args=args),
        thought_signature=signature,
    )


def test_text_answer_and_usage_are_mapped():
    client = FakeClient([_response([types.Part(text="UNRATE is 4.1%.")])])
    resp = GeminiModel("report_agent", client=client, model="m").turn("sys", [
        {"role": "user", "content": "hi"}
    ], [])
    assert resp.text == "UNRATE is 4.1%."
    assert not resp.wants_tools
    assert resp.stop_reason == "end_turn"
    # Thinking tokens are billed as output.
    assert (resp.input_tokens, resp.output_tokens) == (100, 25)


def test_config_disables_auto_function_calling_and_passes_json_schema():
    client = FakeClient([_response([types.Part(text="ok")])])
    GeminiModel("economic_data_agent", client=client, model="m").turn(
        "the system prompt", [{"role": "user", "content": "go"}], tools.TOOL_SCHEMAS
    )
    config = client.models.calls[0]["config"]
    assert config.system_instruction == "the system prompt"
    # The loop — not the SDK — must execute tools, so they hit call_tool's
    # validation / guardrail / audit log.
    assert config.automatic_function_calling.disable is True
    decls = config.tools[0].function_declarations
    assert [d.name for d in decls] == [t["name"] for t in tools.TOOL_SCHEMAS]
    assert decls[0].parameters_json_schema == tools.TOOL_SCHEMAS[0]["input_schema"]
    # Per-role effort carries over as the Gemini thinking level.
    assert config.thinking_config.thinking_level.name == "LOW"


def test_no_tools_means_no_tools_field():
    client = FakeClient([_response([types.Part(text="ok")])])
    GeminiModel("risk_agent", client=client, model="m").turn(
        "s", [{"role": "user", "content": "go"}], []
    )
    assert client.models.calls[0]["config"].tools is None


def test_function_calls_become_tool_requests_with_stable_ids():
    client = FakeClient([
        _response([
            types.Part(text="Fetching."),
            _call("search_series", {"search_text": "core inflation"}),
            _call("get_series_metadata", {"series_id": "UNRATE"}, id="given-id"),
        ])
    ])
    resp = GeminiModel("economic_data_agent", client=client, model="m").turn(
        "s", [{"role": "user", "content": "go"}], tools.TOOL_SCHEMAS
    )
    assert resp.stop_reason == "tool_use"
    assert [(r.name, r.input) for r in resp.tool_requests] == [
        ("search_series", {"search_text": "core inflation"}),
        ("get_series_metadata", {"series_id": "UNRATE"}),
    ]
    # Gemini API calls often have no id; one is synthesized so tool_result
    # blocks can still be matched back to their call.
    assert resp.tool_requests[0].id.startswith("gemini-call-")
    assert resp.tool_requests[1].id == "given-id"
    assert resp.text == "Fetching."


def test_synthesized_ids_are_never_sent_to_the_api():
    """A call Gemini issued without an id gets a local one for the loop, but
    the replayed turn and the FunctionResponse go back without it."""
    client = FakeClient([
        _response([_call("search_series", {"search_text": "gdp"})]),
        _response([types.Part(text="done")]),
    ])
    agent = Agent(
        "economic_data_agent", "sys", tools.TOOL_SCHEMAS, tools.call_tool,
        GeminiModel("economic_data_agent", client=client, model="m"), Trace(),
    )
    agent.run("find gdp")
    second = client.models.calls[1]["contents"]
    assert second[1].parts[0].function_call.id is None
    fr = second[2].parts[0].function_response
    assert fr.id is None and fr.name == "search_series"


def test_thought_parts_are_not_answer_text():
    client = FakeClient([
        _response([types.Part(text="secret reasoning", thought=True), types.Part(text="answer")])
    ])
    resp = GeminiModel("report_agent", client=client, model="m").turn(
        "s", [{"role": "user", "content": "q"}], []
    )
    assert resp.text == "answer"


@pytest.mark.parametrize("finish", ["SAFETY", "PROHIBITED_CONTENT"])
def test_blocked_finish_is_a_refusal(finish):
    client = FakeClient([_response([], finish=finish)])
    resp = GeminiModel("report_agent", client=client, model="m").turn(
        "s", [{"role": "user", "content": "q"}], []
    )
    assert resp.stop_reason == "refusal"
    assert resp.text == f"[model refused: {finish.lower()}]"


def test_blocked_prompt_with_no_candidates_is_a_refusal():
    blocked = types.GenerateContentResponse(
        candidates=[],
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY"),
    )
    resp = GeminiModel("report_agent", client=FakeClient([blocked]), model="m").turn(
        "s", [{"role": "user", "content": "q"}], []
    )
    assert resp.stop_reason == "refusal"
    assert "safety" in resp.text


def test_full_agent_loop_replays_thought_signatures_and_tool_results():
    """Two-turn loop through the real `Agent` + `tools.call_tool`: the second
    request must (a) replay the model's first turn byte-for-byte, signature
    included, and (b) carry the tool result as a FunctionResponse with the
    matching id and name."""
    sig = b"opaque-signature"
    client = FakeClient([
        _response([_call("get_series_metadata", {"series_id": "UNRATE"}, id="c1", signature=sig)]),
        _response([types.Part(text="UNRATE is the unemployment rate.")]),
    ])
    trace = Trace()
    agent = Agent(
        "research_agent", "sys",
        [t for t in tools.TOOL_SCHEMAS if t["name"] == "get_series_metadata"],
        tools.call_tool,
        GeminiModel("research_agent", client=client, model="m"),
        trace,
    )
    assert agent.run("Frame UNRATE.") == "UNRATE is the unemployment rate."

    second = client.models.calls[1]["contents"]
    assert [c.role for c in second] == ["user", "model", "user"]
    replayed = second[1].parts[0]
    assert replayed.thought_signature == sig
    assert replayed.function_call.name == "get_series_metadata"
    fr = second[2].parts[0].function_response
    assert (fr.id, fr.name) == ("c1", "get_series_metadata")
    assert fr.response["series_id"] == "UNRATE"
    # The same trace the evals grade — provider-agnostic.
    assert trace.tool_sequence == ["get_series_metadata"]
    assert trace.input_tokens == 200


def test_history_without_a_memoised_turn_is_rebuilt():
    """A fresh model instance (no memo) still produces a valid history from
    Anthropic-shaped blocks, and wraps non-object tool output."""
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "t1", "name": "search_series",
             "input": {"search_text": "gdp"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "not json"},
        ]},
    ]
    client = FakeClient([_response([types.Part(text="done")])])
    GeminiModel("economic_data_agent", client=client, model="m").turn("s", history, [])
    contents = client.models.calls[0]["contents"]
    model_parts = contents[1].parts
    assert model_parts[0].text == "looking"
    assert model_parts[1].function_call.args == {"search_text": "gdp"}
    assert contents[2].parts[0].function_response.response == {"result": "not json"}
    assert contents[2].parts[0].function_response.name == "search_series"


def test_make_model_selects_gemini(monkeypatch):
    import google.genai

    monkeypatch.setenv("AGENT_BACKEND", "gemini")
    monkeypatch.setattr(google.genai, "Client", lambda **_kw: FakeClient([]))
    assert isinstance(make_model("supervisor"), GeminiModel)


def test_real_client_is_built_with_retries(monkeypatch):
    """The SDK never retries by default; one transient 503 used to end the
    whole multi-agent run (found on the first live run)."""
    import google.genai

    seen = {}

    def client(**kwargs):
        seen.update(kwargs)
        return FakeClient([])

    monkeypatch.setattr(google.genai, "Client", client)
    GeminiModel("supervisor")
    retry = seen["http_options"].retry_options
    assert retry.attempts >= 3
    # Transient 503s are the SDK's job; 429s go to the quota-aware path,
    # which waits as long as Google asks instead of a blind 1-20 s backoff.
    assert 503 in retry.http_status_codes
    assert 429 not in retry.http_status_codes


def test_tool_result_json_round_trips():
    payload = {"series_id": "UNRATE", "observations": [{"date": "2024-01-01", "value": "3.7"}]}
    from agents.model import _as_response_dict

    assert _as_response_dict(json.dumps(payload)) == payload
    assert _as_response_dict(json.dumps([1, 2])) == {"result": [1, 2]}


# --- streaming (stream_turn) -------------------------------------------------


def _chunk(parts, finish=None, usage=None):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=finish)
        ],
        usage_metadata=usage,
    )


def test_stream_turn_forwards_text_as_it_arrives_and_returns_the_whole():
    usage = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=40, candidates_token_count=9
    )
    client = FakeClient([[
        _chunk([types.Part(text="planning…", thought=True)]),
        _chunk([types.Part(text="Unemployment ")]),
        _chunk([types.Part(text="fell to 4.1%.")], finish="STOP", usage=usage),
    ]])
    seen: list[str] = []
    resp = GeminiModel("report_agent", client=client, model="m").stream_turn(
        "s", [{"role": "user", "content": "q"}], [], seen.append
    )
    # Thought summaries never reach the user-facing stream.
    assert seen == ["Unemployment ", "fell to 4.1%."]
    # Fragments concatenate as-is — no newline between chunks.
    assert resp.text == "Unemployment fell to 4.1%."
    assert (resp.input_tokens, resp.output_tokens) == (40, 9)
    assert resp.stop_reason == "end_turn"


def test_streamed_function_call_is_replayed_with_its_signature():
    sig = b"streamed-signature"
    client = FakeClient([
        [
            _chunk([types.Part(text="Let me check. ")]),
            _chunk([_call("get_series_metadata", {"series_id": "UNRATE"}, id="s1",
                          signature=sig)], finish="STOP"),
        ],
        [_chunk([types.Part(text="Done.")], finish="STOP")],
    ])
    model = GeminiModel("research_agent", client=client, model="m")
    agent = Agent(
        "research_agent", "sys",
        [t for t in tools.TOOL_SCHEMAS if t["name"] == "get_series_metadata"],
        tools.call_tool, model, Trace(),
    )
    seen: list[str] = []
    agent.on_text = seen.append
    assert agent.run("Frame UNRATE.") == "Done."

    replayed = client.models.calls[1]["contents"][1]
    assert [p.text for p in replayed.parts if p.text] == ["Let me check. "]
    fc_part = next(p for p in replayed.parts if p.function_call)
    assert fc_part.thought_signature == sig
    # Text that preceded the tool call was streamed too; that's the model's
    # own narration, and the caller decides whether to show it.
    assert seen == ["Let me check. ", "Done."]


def test_stream_refusal_on_the_last_chunk():
    client = FakeClient([[
        _chunk([types.Part(text="Partial ")]),
        _chunk([], finish="SAFETY"),
    ]])
    resp = GeminiModel("report_agent", client=client, model="m").stream_turn(
        "s", [{"role": "user", "content": "q"}], [], lambda _t: None
    )
    assert resp.stop_reason == "refusal"
    assert resp.text == "[model refused: safety]"


def test_empty_stream_is_a_refusal_not_a_crash():
    client = FakeClient([[]])
    resp = GeminiModel("report_agent", client=client, model="m").stream_turn(
        "s", [{"role": "user", "content": "q"}], [], lambda _t: None
    )
    assert resp.stop_reason == "refusal"


# --- quota (429) handling -----------------------------------------------------
# Found on the first live run: a free-tier key allows 5 requests/min/model,
# fewer than one agent run makes, and Google answers 429 with "retry in ~58s".

from google.genai import errors  # noqa: E402

from agents.model import QuotaExhausted, _quota_wait  # noqa: E402


def _429(retry_delay="42s", quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier"):
    details = [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": quota_id, "quotaValue": "5"}]},
    ]
    if retry_delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": retry_delay})
    return errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": details,
    }})


class FlakyModels(FakeModels):
    """Raises the scripted exceptions first, then serves responses."""

    def __init__(self, failures, responses):
        super().__init__(responses)
        self._failures = list(failures)

    def generate_content(self, **kw):
        if self._failures:
            raise self._failures.pop(0)
        return super().generate_content(**kw)

    def generate_content_stream(self, **kw):
        if self._failures:
            raise self._failures.pop(0)
        yield from super().generate_content_stream(**kw)


def _flaky_model(failures, responses, role="report_agent"):
    client = FakeClient([])
    client.models = FlakyModels(failures, responses)
    model = GeminiModel(role, client=client, model="m")
    model._chain = ["m"]  # quota handling on one model; fallback is tested below
    model.slept = []
    model._sleep = model.slept.append
    return model


def test_quota_wait_reads_google_retry_info():
    assert _quota_wait(_429("42s")) == 43.0
    assert _quota_wait(_429("0.5s")) == 1.5
    # Waiting can't fix a per-day quota, a missing hint, or a very long wait.
    assert _quota_wait(_429("42s", quota_id="GenerateRequestsPerDayPerProjectPerModel")) is None
    assert _quota_wait(_429(None)) is None
    assert _quota_wait(_429("3600s")) is None


def test_429_waits_as_told_then_succeeds_and_says_so():
    model = _flaky_model([_429("42s")], [_response([types.Part(text="ok")])])
    events = []
    model.notify = events.append
    resp = model.turn("s", [{"role": "user", "content": "q"}], [])
    assert resp.text == "ok"
    assert model.slept == [43.0]
    assert events == [{"type": "waiting", "reason": "rate_limited", "seconds": 43}]


def test_daily_quota_fails_fast_with_a_safe_error():
    model = _flaky_model([_429("42s", quota_id="GenerateRequestsPerDayPerProjectPerModel")], [])
    with pytest.raises(QuotaExhausted) as info:
        model.turn("s", [{"role": "user", "content": "q"}], [])
    assert model.slept == []
    assert "quota" in str(info.value).lower()


def test_quota_retries_are_bounded():
    from agents import model as model_mod

    failures = [_429("1s") for _ in range(model_mod.GEMINI_QUOTA_RETRIES + 1)]
    model = _flaky_model(failures, [])
    with pytest.raises(QuotaExhausted):
        model.turn("s", [{"role": "user", "content": "q"}], [])
    assert len(model.slept) == model_mod.GEMINI_QUOTA_RETRIES


def test_other_client_errors_are_not_retried():
    bad = errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}})
    model = _flaky_model([bad], [])
    with pytest.raises(errors.ClientError):
        model.turn("s", [{"role": "user", "content": "q"}], [])
    assert model.slept == []


def test_stream_429_before_any_text_is_retried():
    model = _flaky_model([_429("2s")], [[_chunk([types.Part(text="hello")], finish="STOP")]])
    seen = []
    resp = model.stream_turn("s", [{"role": "user", "content": "q"}], [], seen.append)
    assert resp.text == "hello" and seen == ["hello"]
    assert model.slept == [3.0]


def test_stream_429_after_text_was_shown_is_not_retried():
    """Retrying would repeat the words the user already saw."""

    class MidStream(FakeModels):
        def generate_content_stream(self, **kw):
            yield _chunk([types.Part(text="partial ")])
            raise _429("2s")

    client = FakeClient([])
    client.models = MidStream([])
    model = GeminiModel("report_agent", client=client, model="m")
    model._sleep = lambda s: pytest.fail("must not wait and retry mid-stream")
    with pytest.raises(QuotaExhausted):
        model.stream_turn("s", [{"role": "user", "content": "q"}], [], lambda _t: None)


def test_agent_routes_quota_waits_into_the_trace():
    model = _flaky_model([_429("5s")], [_response([types.Part(text="done")])], role="risk_agent")
    events = []
    trace = Trace(listener=events.append)
    Agent("risk_agent", "sys", [], tools.call_tool, model, trace).run("assess")
    waits = [e for e in events if e["type"] == "waiting"]
    assert waits == [{"type": "waiting", "reason": "rate_limited", "seconds": 6,
                      "agent": "risk_agent", "elapsed_ms": waits[0]["elapsed_ms"]}]


# --- model fallback on overload (503) ----------------------------------------
# Found live: gemini-3.8-flash and 3.7-flash both returned 503 "high demand"
# through every retry while 3.6-flash answered in ~1 s.


def _503():
    return errors.ServerError(503, {"error": {
        "code": 503, "status": "UNAVAILABLE", "message": "high demand",
    }})


class ByModel(FakeModels):
    """Overloaded models raise 503; everything else answers, recording which
    model served each call."""

    def __init__(self, overloaded, responses):
        super().__init__(responses)
        self.overloaded = set(overloaded)
        self.served_by: list[str] = []

    def generate_content(self, *, model, contents, config):
        if model in self.overloaded:
            raise _503()
        self.served_by.append(model)
        return super().generate_content(model=model, contents=contents, config=config)

    def generate_content_stream(self, *, model, contents, config):
        if model in self.overloaded:
            raise _503()
        self.served_by.append(model)
        yield from super().generate_content_stream(model=model, contents=contents, config=config)


@pytest.fixture
def chain(monkeypatch):
    from agents import model as model_mod

    monkeypatch.setattr(model_mod, "GEMINI_FALLBACK_MODELS", ["fallback-1", "fallback-2"])


def _model_on(models, role="report_agent"):
    client = FakeClient([])
    client.models = models
    return GeminiModel(role, client=client, model="primary")


def test_overloaded_primary_falls_back_and_says_so(chain):
    models = ByModel({"primary"}, [_response([types.Part(text="ok")])])
    model = _model_on(models)
    events = []
    model.notify = events.append
    assert model.turn("s", [{"role": "user", "content": "q"}], []).text == "ok"
    assert models.served_by == ["fallback-1"]
    assert events == [{"type": "fallback", "reason": "overloaded",
                       "from_model": "primary", "to_model": "fallback-1"}]


def test_fallback_walks_the_whole_chain(chain):
    models = ByModel({"primary", "fallback-1"}, [_response([types.Part(text="ok")])])
    assert _model_on(models).turn("s", [{"role": "user", "content": "q"}], []).text == "ok"
    assert models.served_by == ["fallback-2"]


def test_circuit_breaker_starts_the_next_agent_on_the_fallback(chain):
    first = ByModel({"primary"}, [_response([types.Part(text="a")])])
    _model_on(first).turn("s", [{"role": "user", "content": "q"}], [])
    # The primary has recovered, but its breaker is still tripped: the next
    # agent doesn't spend ~10 s of retries rediscovering the overload.
    second = ByModel(set(), [_response([types.Part(text="b")])])
    _model_on(second).turn("s", [{"role": "user", "content": "q"}], [])
    assert second.served_by == ["fallback-1"]


def test_everything_overloaded_raises_the_503(chain):
    models = ByModel({"primary", "fallback-1", "fallback-2"}, [])
    with pytest.raises(errors.ServerError):
        _model_on(models).turn("s", [{"role": "user", "content": "q"}], [])


def test_mid_loop_fallback_resigns_the_earlier_models_turn(chain):
    """A 503 on an agent's *second* turn now fails over too. The fallback
    model would reject the primary's signature ("Corrupted thought
    signature", verified live), so the replayed turn goes out re-signed with
    Google's skip-validator value."""
    from agents.model import SKIP_THOUGHT_SIGNATURE

    class PrimaryDiesAfterOneTurn(FakeModels):
        def generate_content(self, *, model, contents, config):
            if model == "primary" and self.calls:
                raise _503()
            return super().generate_content(model=model, contents=contents, config=config)

    models = PrimaryDiesAfterOneTurn([
        _response([_call("get_series_metadata", {"series_id": "UNRATE"}, id="c1",
                         signature=b"primary-sig")]),
        _response([types.Part(text="UNRATE framed.")]),
    ])
    agent = Agent(
        "research_agent", "sys",
        [t for t in tools.TOOL_SCHEMAS if t["name"] == "get_series_metadata"],
        tools.call_tool, _model_on(models, role="research_agent"), Trace(),
    )
    assert agent.run("Frame UNRATE.") == "UNRATE framed."
    assert [c["model"] for c in models.calls] == ["primary", "fallback-1"]
    replayed = models.calls[1]["contents"][1].parts[0]
    assert replayed.function_call.name == "get_series_metadata"
    assert replayed.thought_signature == SKIP_THOUGHT_SIGNATURE


def test_resigning_leaves_this_models_own_turns_alone():
    from agents.model import SKIP_THOUGHT_SIGNATURE, resign_foreign_turns

    own = types.Content(role="model", parts=[
        _call("a", {}, id="1", signature=b"mine"),
        _call("b", {}, id="2"),  # parallel call: only the first is signed
    ])
    foreign = types.Content(role="model", parts=[
        types.Part(text="hmm", thought_signature=b"theirs"),
        _call("c", {}, id="3"),
    ])
    user = types.Content(role="user", parts=[types.Part(text="q")])
    out = resign_foreign_turns([user, own, foreign], own={b"mine"})
    assert out[0] is user and out[1] is own  # untouched, not even copied
    assert [p.thought_signature for p in out[2].parts] == [SKIP_THOUGHT_SIGNATURE] * 2
    assert foreign.parts[0].thought_signature == b"theirs"  # input not mutated


def test_no_fallback_after_streamed_text_was_shown(chain):
    class DiesMidStream(FakeModels):
        def generate_content_stream(self, **kw):
            yield _chunk([types.Part(text="partial ")])
            raise _503()

    client = FakeClient([])
    client.models = DiesMidStream([])
    model = GeminiModel("report_agent", client=client, model="primary")
    with pytest.raises(errors.ServerError):
        model.stream_turn("s", [{"role": "user", "content": "q"}], [], lambda _t: None)


def test_non_overload_server_errors_do_not_fall_back(chain):
    class Broken(FakeModels):
        def generate_content(self, **kw):
            raise errors.ServerError(500, {"error": {"code": 500, "status": "INTERNAL"}})

    client = FakeClient([])
    client.models = Broken([])
    with pytest.raises(errors.ServerError):
        GeminiModel("report_agent", client=client, model="primary").turn(
            "s", [{"role": "user", "content": "q"}], []
        )


def test_a_model_out_of_daily_quota_falls_back(chain):
    """Found live: gemini-3.8-flash's free-tier *daily* quota (20) ran out
    ("retry in 4h45m"). Quotas are per model, so the next one can serve."""

    class DailyQuotaGone(FakeModels):
        def generate_content(self, *, model, contents, config):
            if model == "primary":
                raise _429("17139s", quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier")
            return super().generate_content(model=model, contents=contents, config=config)

    models = DailyQuotaGone([_response([types.Part(text="ok")])])
    model = _model_on(models)
    events = []
    model.notify = events.append
    model._sleep = lambda s: pytest.fail("a daily quota must not be waited out")
    assert model.turn("s", [{"role": "user", "content": "q"}], []).text == "ok"
    assert events[0]["reason"] == "quota_exhausted"
    assert [c["model"] for c in models.calls] == ["fallback-1"]
    # Breaker held for Google's estimate (hours), not the 2-minute overload cooldown.
    from agents import model as model_mod

    assert model_mod._overloaded_until["primary"] - __import__("time").monotonic() > 17000

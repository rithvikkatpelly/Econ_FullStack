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
    monkeypatch.setattr(google.genai, "Client", lambda: FakeClient([]))
    assert isinstance(make_model("supervisor"), GeminiModel)


def test_tool_result_json_round_trips():
    payload = {"series_id": "UNRATE", "observations": [{"date": "2024-01-01", "value": "3.7"}]}
    from agents.model import _as_response_dict

    assert _as_response_dict(json.dumps(payload)) == payload
    assert _as_response_dict(json.dumps([1, 2])) == {"result": [1, 2]}

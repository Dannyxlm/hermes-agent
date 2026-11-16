"""A tool's own argument writer runs on the new assistant turn before anything reads it.

Regression for Iris's W1 (cards R1 pass): a weather card sent ``"high": 4`` and was refused three times
("must be string, not int"), costing a model call and leaking the refusal into the reply. The app draws
only text there, so the number is written as text in the call's arguments themselves: ``tool.start``, the
stored turn and the model's next request all carry the same written card, and the first call is accepted.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import get_hermes_home
from tools.registry import invalidate_check_fn_cache

FORECAST = {"kind": "weather", "title": "Pinehollow · next three days",
            "data": {"headline": "4°C and cloudy", "condition": "cloudy",
                     "days": [{"day": "Today", "high": 4, "low": -1}, {"day": "Tue", "high": 7, "low": 1},
                              {"day": "Wed", "high": 2, "low": -3, "condition": "snow"}]}}


@pytest.fixture
def cards_on():
    (get_hermes_home() / "config.yaml").write_text("cards:\n  enabled: true\n", encoding="utf-8")
    invalidate_check_fn_cache()
    yield
    invalidate_check_fn_cache()


def _call(arguments: str, call_id: str = "call_card"):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name="show_card", arguments=arguments))


def _response(calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="" if calls else "Cold, then snow on Wednesday.", tool_calls=calls),
        finish_reason="tool_calls" if calls else "stop")], model="test/model", usage=None)


def _run_turn(arguments: str):
    from run_agent import AIAgent

    with patch("agent.process_bootstrap.OpenAI"), patch("agent.model_metadata.fetch_model_metadata", return_value={}):
        agent = AIAgent(model="test/model", api_key="test-key", base_url="http://127.0.0.1:1/v1",
                        enabled_toolsets=["cards"], quiet_mode=True, platform="tui",
                        skip_context_files=True, skip_memory=True, max_iterations=3)
    assert "show_card" in agent.valid_tool_names
    agent.client = MagicMock()
    agent._cached_system_prompt = "Show cards when they help."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.client.chat.completions.create.side_effect = [_response([_call(arguments)]), _response()]
    started = []
    agent.tool_start_callback = lambda call_id, name, args: started.append(json.loads(json.dumps(args)))
    result = agent.run_conversation("Make me a made-up three-day forecast for Pinehollow.")
    second_request = agent.client.chat.completions.create.call_args_list[1].kwargs["messages"]
    return result, started, second_request


def _stored_arguments(messages) -> str:
    return next(m for m in messages if m.get("tool_calls"))["tool_calls"][0]["function"]["arguments"]


def test_numeric_temperatures_are_written_as_text_before_anything_reads_the_call(cards_on):
    result, started, second_request = _run_turn(json.dumps(FORECAST))

    receipt = json.loads(next(m for m in result["messages"] if m["role"] == "tool")["content"])
    assert receipt["ok"] is True, receipt  # accepted on the first call
    stored = json.loads(_stored_arguments(result["messages"]))
    assert [(d["high"], d["low"]) for d in stored["data"]["days"]] == [("4°", "-1°"), ("7°", "1°"), ("2°", "-3°")]
    assert started == [stored]  # tool.start carries the written card, which the app draws as it stands
    sent_back = json.loads(_stored_arguments(second_request))
    assert sent_back == stored  # and the model's next request sees the same arguments
    assert "Today 4°/-1°" in receipt["alt"]


def test_a_call_with_nothing_to_write_keeps_its_exact_bytes(cards_on):
    """The provider's own argument string is left alone (no re-serialization) when nothing changes."""
    written = json.dumps({**FORECAST, "data": {**FORECAST["data"], "days": [{"day": "Today", "high": "4°"}]}},
                         indent=2)
    result, _started, _second = _run_turn(written)
    assert _stored_arguments(result["messages"]) == written


def test_a_card_nested_past_the_depth_limit_is_refused_not_crashed_on(cards_on):
    """Review B1: deep-copying a ~600-level card raised RecursionError in the turn loop, which re-called
    the model as if the API had failed. It is left as sent and the validator's depth rule refuses it."""
    nested = {"x": 1}
    for _ in range(600):
        nested = {"x": nested}
    card = {**FORECAST, "data": {**FORECAST["data"], "days": [{"day": "Today", "high": 4, "extra": nested}]}}
    result, _started, _second = _run_turn(json.dumps(card))
    receipt = json.loads(next(m for m in result["messages"] if m["role"] == "tool")["content"])
    assert receipt["ok"] is False and "nests deeper" in receipt["errors"][0]["message"]


def _tool_call(arguments, call_id="toolu_card"):
    from agent.transports.types import ToolCall
    return ToolCall(id=call_id, name="show_card", arguments=arguments)


def test_native_block_sidecars_replay_the_written_input():
    """Review B2: Anthropic and Bedrock Converse replay the turn from ordered block sidecars, not from
    tool_calls; their tool block gets the written input, every other block and the order stay exactly."""
    import tools.cards.tool  # registers show_card and its writer
    from agent.tool_call_normalize import write_registered_arguments

    call = _tool_call(json.dumps(FORECAST))
    thinking = {"type": "thinking", "thinking": "a forecast", "signature": "sig"}
    provider_data = {
        "anthropic_content_blocks": [thinking, {"type": "tool_use", "id": "toolu_card", "name": "show_card",
                                                "input": FORECAST}],
        "bedrock_content_blocks": [{"reasoningContent": {"text": "a forecast"}},
                                   {"toolUse": {"toolUseId": "toolu_card", "name": "show_card", "input": FORECAST}}]}
    write_registered_arguments([call], provider_data=provider_data)

    written = json.loads(call.function.arguments)
    assert written["data"]["days"][0]["high"] == "4°"
    anthropic, bedrock = provider_data["anthropic_content_blocks"], provider_data["bedrock_content_blocks"]
    assert anthropic[0] is thinking and anthropic[1]["input"] == written
    assert bedrock[0] == {"reasoningContent": {"text": "a forecast"}} and bedrock[1]["toolUse"]["input"] == written


def test_a_failing_writer_leaves_the_call_as_sent(monkeypatch):
    """A tool's writer runs inside the turn loop; if it raises, the call goes on exactly as the model sent it."""
    from agent import tool_call_normalize

    def broken(_args):
        raise ValueError("writer bug")

    monkeypatch.setattr(tool_call_normalize.registry, "get_args_normalizer", lambda _name: broken)
    sent = json.dumps(FORECAST)
    call = _tool_call(sent)
    tool_call_normalize.write_registered_arguments([call], provider_data={"anthropic_content_blocks": [{"x": 1}]})
    assert call.function.arguments == sent

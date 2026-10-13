"""Tier evidence uses actual SDK boundaries, never config or Relay-rewritten output."""
from copy import deepcopy
import logging
import re
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

SENTINEL = "PRIVATE_SYNTHETIC_SENTINEL\nforged=priority"


class Stream:
    def __init__(self, events, final=None):
        self.events, self.final, self.closed = events, final, False
        self.response = None

    def __iter__(self):
        yield from self.events

    def close(self):
        self.closed = True

    def get_final_message(self):
        return self.final

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


@pytest.fixture(autouse=True)
def offline(monkeypatch, caplog):
    import socket

    def no_network(*_args, **_kwargs):
        raise AssertionError("network forbidden in tier collector tests")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **_kw: [])
    monkeypatch.setattr("model_tools.check_toolset_requirements", dict)
    caplog.set_level(logging.INFO, logger="agent.tier_evidence")


def _records(caplog):
    lines = [r.getMessage() for r in caplog.records if r.name == "agent.tier_evidence"]
    assert lines, "actual SDK invocation must retain tier evidence"
    for line in lines:
        assert SENTINEL not in line
        assert "\n" not in line and len(line) < 300
    return [dict(part.split("=", 1) for part in line.split()[1:]) for line in lines]


def _pair(caplog):
    request, response = _records(caplog)
    assert request["phase"] == "request" and response["phase"] == "response"
    assert re.fullmatch(r"[0-9a-f]{32}", request["attempt"])
    assert request["attempt"] == response["attempt"]
    return request, response


def _agent(wire):
    from run_agent import AIAgent

    agent = AIAgent(model="synthetic-model", provider="openai-codex" if wire == "responses" else "anthropic",
                    api_mode="codex_responses" if wire == "responses" else "anthropic_messages",
                    base_url="https://example.invalid", api_key="synthetic-key", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, save_trajectories=False)
    agent._interrupt_requested = False
    return agent


def _rewrite_relay(monkeypatch):
    """Late request rewrite and post-provider response rewrite, retaining real Relay iteration."""
    from agent import relay_llm
    original = relay_llm.stream

    class Rewritten:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def __iter__(self):
            for event in self.raw:
                event = deepcopy(event)
                if getattr(event, "type", "") == "response.completed":
                    event.response.service_tier = "priority"
                yield event

    def relay(request, factory, **kwargs):
        def rewritten(final):
            final = dict(final, service_tier="flex")
            return Rewritten(factory(final))
        return original(request, rewritten, **kwargs)

    monkeypatch.setattr(relay_llm, "stream", relay)


@pytest.mark.parametrize("terminal", ["completed", "incomplete", "failed"])
def test_codex_final_sdk_kwargs_and_raw_terminal_tier(monkeypatch, caplog, terminal):
    agent = _agent("responses")
    _rewrite_relay(monkeypatch)
    sent = []
    raw = Stream([NS(type="response.output_text.delta", delta=SENTINEL),
                  NS(type="response." + terminal, response=NS(status=terminal, service_tier="default",
                                                              usage=None, id=SENTINEL))])
    def create(**kwargs):
        sent.append(kwargs)
        return raw
    agent.client = NS(responses=NS(create=create))
    request = {"model": "synthetic-model", "service_tier": "priority", "input": [],
               "instructions": SENTINEL, "extra_headers": {"Authorization": SENTINEL}}
    try:
        result = agent._run_codex_stream(request)
    finally:
        agent.close()
    before, after = _pair(caplog)
    assert before["req_service_tier"] == sent[0]["service_tier"] == "flex"
    assert after["served_tier"] == "default" and after["served_source"] == "response"
    assert after["response_status"] == terminal and after["outcome"] == "terminal"
    assert result.service_tier == ("priority" if terminal == "completed" else "default")
    assert request["service_tier"] == "priority"


@pytest.mark.parametrize("tier", ["standard", None, SENTINEL, "x" * 10000, ["standard"], 42])
def test_anthropic_main_stream_nested_tier_and_no_content(monkeypatch, caplog, tier):
    agent = _agent("anthropic_messages")
    _rewrite_relay(monkeypatch)
    final = NS(content=[NS(type="text", text="done")], stop_reason="end_turn", usage=None)
    raw = Stream([NS(type="message_start", message=NS(usage=NS(service_tier=tier))),
                  NS(type="content_block_delta", delta=NS(type="text_delta", text=SENTINEL)),
                  NS(type="message_stop")], final)
    client = MagicMock()
    client.messages.stream.return_value = raw
    agent._anthropic_client = client
    agent._anthropic_api_key = "synthetic-key"
    agent._create_request_anthropic_client = lambda *_a, **_k: client
    try:
        result = agent._interruptible_streaming_api_call({"model": "synthetic-model", "max_tokens": 8,
                                                         "messages": [], "extra_body": {"speed": "fast"}})
    finally:
        agent.close()
    before, after = _pair(caplog)
    assert before["req_service_tier"] == client.messages.stream.call_args.kwargs["service_tier"] == "flex"
    assert before["req_speed"] == "fast"
    expected = "absent" if tier is None else "standard" if tier == "standard" else "unknown"
    assert after["served_tier"] == expected
    assert after["served_source"] == ("absent" if tier is None else "usage")
    assert after["outcome"] == "terminal"
    assert result is final and raw.closed


@pytest.mark.parametrize("streaming", [True, False])
def test_anthropic_create_path_absence_and_extra_body_precedence(caplog, streaming):
    from agent.anthropic_adapter import create_anthropic_message

    final = NS(content=[], stop_reason="end_turn", usage=None, id=SENTINEL)
    client = NS(messages=NS(stream=lambda **_k: Stream([NS(type="message_stop")], final),
                            create=lambda **_k: final))
    request = {"model": "synthetic-model", "messages": [], "service_tier": "priority",
               "extra_body": {"service_tier": None, "speed": "fast"}}
    assert create_anthropic_message(client, request, prefer_stream=streaming) is final
    before, after = _pair(caplog)
    assert before["req_service_tier"] == "absent" and before["req_speed"] == "fast"
    assert after["served_tier"] == after["served_source"] == "absent"


@pytest.mark.parametrize("error", [RuntimeError(SENTINEL), InterruptedError(SENTINEL)])
def test_sdk_open_error_then_retry_is_distinct_and_preserves_error(caplog, error):
    from agent.anthropic_adapter import create_anthropic_message

    def fail(**_kwargs):
        raise error
    client = NS(messages=NS(create=fail))
    with pytest.raises(type(error)) as raised:
        create_anthropic_message(client, {}, prefer_stream=False)
    assert raised.value is error
    client.messages.create = lambda **_kw: NS(usage={"service_tier": "standard"})
    create_anthropic_message(client, {}, prefer_stream=False)
    first, failure, second, returned = _records(caplog)
    assert first["attempt"] == failure["attempt"] != second["attempt"] == returned["attempt"]
    assert failure["outcome"] == "error" and failure["served_tier"] == "absent"
    assert returned["served_tier"] == "standard"

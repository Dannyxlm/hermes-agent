"""Bounded metadata filtering and transparent SDK stream lifecycle contracts."""
import logging
from types import SimpleNamespace as NS

import pytest

from agent.anthropic_adapter import create_anthropic_message, normalize_stream_usage
from agent.tier_evidence import observe_tier_manager, observe_tier_stream


@pytest.fixture(autouse=True)
def capture(caplog):
    caplog.set_level(logging.INFO, logger="agent.tier_evidence")


def _rows(caplog):
    rows = [r.getMessage() for r in caplog.records if r.name == "agent.tier_evidence"]
    assert all("SENTINEL" not in row and len(row) < 300 and "\n" not in row for row in rows)
    return [dict(item.split("=", 1) for item in row.split()[1:]) for row in rows]


class Hostile:
    def __str__(self):
        raise AssertionError("must not stringify metadata")

    __repr__ = __str__


@pytest.mark.parametrize("bad", ["priority\nSENTINEL", "PRIORITY", "priority ", "", "x" * 10000,
                                  ["priority"], {"private": "SENTINEL"}, Hostile()],
                         ids=["newline", "case", "whitespace", "empty", "long", "list", "dict", "object"])
def test_request_and_response_hostile_fields_never_become_evidence(caplog, bad):
    response = NS(service_tier=bad, status=bad, usage={"service_tier": "standard"},
                  content="SENTINEL", id="SENTINEL")
    client = NS(messages=NS(create=lambda **_k: response))
    assert create_anthropic_message(client, {"service_tier": bad, "extra_body": {"speed": bad}},
                                    prefer_stream=False) is response
    request, served = _rows(caplog)
    assert request["req_service_tier"] == request["req_speed"] == "unknown"
    assert served["served_tier"] == served["response_status"] == "unknown"
    assert served["served_source"] == "response"  # invalid top-level does not fall through


class SDKStream:
    def __init__(self, events):
        self._raw_stream = iter(events)
        self.closed = False

    def __iter__(self):
        for event in self._raw_stream:
            # Models the real SDK crash normalize_stream_usage prevents.
            if event.type == "message_delta":
                assert event.usage is not None
            yield event

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()


def test_sdk_private_stream_assignment_and_events_stay_transparent(caplog):
    events = [NS(type="message_delta", usage=None), NS(type="message_stop")]
    raw = SDKStream(events)
    with observe_tier_manager({}, lambda **_k: raw) as stream:
        assert list(normalize_stream_usage(stream)) == events
    assert raw.closed
    assert _rows(caplog)[1]["served_tier"] == "absent"


@pytest.mark.parametrize("ending", ["eof", "close", "error"])
def test_nonterminal_stream_retains_only_reported_tier_and_no_success(caplog, ending):
    error = RuntimeError("SENTINEL")
    def events():
        yield NS(type="message_start", message=NS(usage={"service_tier": "standard"}))
        if ending == "error":
            raise error
    raw = SDKStream(events())
    with observe_tier_manager({}, lambda **_k: raw) as stream:
        iterator = iter(stream)
        next(iterator)
        if ending == "close":
            iterator.close()
        elif ending == "error":
            with pytest.raises(RuntimeError) as raised:
                next(iterator)
            assert raised.value is error
        else:
            assert list(iterator) == []
    request, response = _rows(caplog)
    assert request["attempt"] == response["attempt"]
    assert response["served_tier"] == "standard"
    assert response["outcome"] == {"eof": "exhausted", "close": "closed", "error": "error"}[ending]


def test_stream_manager_enter_failure_preserves_exception(caplog):
    error = InterruptedError("SENTINEL")
    class Manager:
        def __enter__(self):
            raise error
    with pytest.raises(InterruptedError) as raised:
        with observe_tier_manager({}, lambda **_k: Manager()):
            pytest.fail("enter should fail")
    assert raised.value is error
    assert _rows(caplog)[1]["outcome"] == "error"


def test_raw_dict_codex_stream_never_infers_tier_from_request(caplog):
    event = {"type": "response.completed", "response": {"usage": None, "id": "SENTINEL"}}
    stream = observe_tier_stream("responses", {"service_tier": "priority"}, lambda **_k: iter([event]))
    assert list(stream) == [event]
    stream.close()
    request, response = _rows(caplog)
    assert request["req_service_tier"] == "priority"
    assert response["served_tier"] == response["served_source"] == "absent"
    assert response["outcome"] == "terminal"

"""Opt-in content-free shape records for consumer-Codex requests (agent/codex_request_shape.py).

The success and rejection paths run the real OpenAI SDK against an ``httpx.MockTransport``, so the
records are derived from the bytes the SDK actually serialized, not from Hermes' kwargs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import stat
from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError, OpenAI

from agent import codex_request_shape as shape
from agent.codex_runtime import run_codex_stream

CODEX_URL = "https://chatgpt.com/backend-api/codex"
SENTINELS = {
    "instructions": "SENTINEL-INSTRUCTIONS-7f3a",
    "user": "SENTINEL-USER-TEXT-91bc",
    "args": "SENTINEL-ARGS-55d0",
    "output": "SENTINEL-TOOL-OUTPUT-c4e2",
    "api_key": "sk-SENTINEL-KEY-0d9e",
    "header": "SENTINEL-HEADER-VALUE-a1b2",
    "cache_key": "SENTINEL-CACHE-KEY-3c3c",
    "detail": "SENTINEL-SERVER-DETAIL-e5e5",
    "reasoning_blob": "SENTINEL-ENCRYPTED-REASONING-8888",
}


def _completed_sse() -> bytes:
    item = {"type": "message", "id": "m1", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": "ok", "annotations": []}]}
    events = [
        {"type": "response.output_item.done", "output_index": 0, "sequence_number": 1, "item": item},
        {"type": "response.completed", "sequence_number": 2,
         "response": {"id": "r1", "object": "response", "created_at": 0, "status": "completed",
                      "model": "gpt-5-codex", "output": [item],
                      "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
    ]
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


class _Server:
    """Records each transmitted body; answers 200 SSE or a bare consumer-Codex 400."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content)
        if self.statuses.pop(0) == 400:
            return httpx.Response(400, json={"detail": f"Bad Request {SENTINELS['detail']}"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_completed_sse())


def _client(server: _Server) -> OpenAI:
    return OpenAI(api_key=SENTINELS["api_key"], base_url=CODEX_URL, max_retries=0,
                  http_client=httpx.Client(transport=httpx.MockTransport(server)))


def _agent(monkeypatch, tmp_path, *, ring: int | None = 8, session="local-" + "ab" * 32):
    from tests.agent.test_run_agent_codex_responses import _build_agent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path, raising=False)
    if ring is not None:
        monkeypatch.setattr(shape, "ring_size", lambda: ring)
    agent = _build_agent(monkeypatch)
    agent.session_id = session
    agent._current_api_request_id = "turn-1:api:1"
    assert agent._is_codex_backend()
    return agent


def _kwargs(extra_items=()):
    items = [
        {"role": "user", "content": [{"type": "input_text", "text": SENTINELS["user"]}]},
        {"type": "function_call", "call_id": "call_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "terminal",
         "arguments": json.dumps({"command": SENTINELS["args"]})},
        {"type": "function_call_output", "call_id": "call_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
         "output": SENTINELS["output"]},
        *extra_items,
    ]
    return {
        "model": "gpt-5-codex", "instructions": SENTINELS["instructions"], "input": items,
        "tools": [{"type": "function", "name": "terminal", "strict": False,
                   "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                                  "required": ["command"]}}],
        "store": False, "reasoning": {"effort": "medium", "summary": "auto"},
        "include": ["reasoning.encrypted_content"], "tool_choice": "auto", "parallel_tool_calls": True,
        "prompt_cache_key": SENTINELS["cache_key"],
        "context_management": [{"type": "compaction", "compact_threshold": 223008}],
        "extra_headers": {"session_id": SENTINELS["header"], "x-client-request-id": SENTINELS["header"]},
    }


def _follow_up_items():
    return ({"type": "reasoning", "encrypted_content": SENTINELS["reasoning_blob"], "summary": []},
            {"type": "function_call", "call_id": "call_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "name": "skill_view",
             "arguments": json.dumps({"name": SENTINELS["args"]})},
            {"type": "function_call_output", "call_id": "call_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
             "output": SENTINELS["output"] * 3})


def _accepted_then_rejected(monkeypatch, tmp_path, caplog=None):
    agent = _agent(monkeypatch, tmp_path)
    server = _Server([200, 400])
    client = _client(server)
    response = run_codex_stream(agent, _kwargs(), client=client)
    assert response.status == "completed"
    agent._current_api_request_id = "turn-1:api:2"
    with pytest.raises(BadRequestError):
        run_codex_stream(agent, _kwargs(_follow_up_items()), client=client)
    return server, shape.read_records(tmp_path / "logs" / shape.RING_FILENAME)


def test_off_by_default_writes_nothing(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path, ring=None)
    assert shape.ring_size() == 0
    server = _Server([200])
    run_codex_stream(agent, _kwargs(), client=_client(server))
    assert len(server.bodies) == 1
    assert not (tmp_path / "logs" / shape.RING_FILENAME).exists()
    assert not (tmp_path / "logs" / "codex_request_shape.key").exists()


def test_ring_size_reads_agent_config_and_clamps(monkeypatch):
    from hermes_cli import config

    for raw, expected in ((5, 5), (0, 0), (-3, 0), (10_000, shape.MAX_RING), (True, 0), ("8", 0), (None, 0)):
        monkeypatch.setattr(config, "load_config_readonly", lambda raw=raw: {"agent": {shape.CONFIG_KEY: raw}})
        assert shape.ring_size() == expected
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["agent"][shape.CONFIG_KEY] == 0


def test_accepted_and_rejected_records_are_comparable_and_from_wire_bytes(monkeypatch, tmp_path):
    server, records = _accepted_then_rejected(monkeypatch, tmp_path)
    assert [r["outcome"] for r in records] == ["accepted", "rejected"]
    accepted, rejected = records

    # Same record schema on both sides: the success/failure comparison is field-for-field.
    assert set(accepted) - {"error"} == set(rejected) - {"error"}
    assert set(accepted["body"]) == set(rejected["body"])
    assert accepted["http_status"] == 200 and rejected["http_status"] == 400
    for record, wire in zip(records, server.bodies):
        assert record["source"] == "wire_bytes"
        assert record["body"]["bytes"] == len(wire)  # exact transmitted size, not a kwargs estimate
        sent = json.loads(wire)
        assert record["body"]["keys"] == {k: shape._json_type(v) for k, v in sorted(sent.items())}
        assert "extra_headers" not in record["body"]["keys"] and record["body"]["config"]["stream"] is True
        assert record["route"] == {"provider": "openai-codex", "model": "gpt-5-codex",
                                   "api_mode": "codex_responses", "call_role": "primary",
                                   "fallback_index": 0, "stream_attempt": 1}
    assert accepted["corr"] == {"session_id": "local-" + "ab" * 32, "api_request_id": "turn-1:api:1"}
    assert rejected["corr"]["api_request_id"] == "turn-1:api:2"
    assert rejected["error"]["class_chain"][0] == "BadRequestError"
    assert rejected["error"]["body_keys"] == ["detail"]
    assert rejected["error"]["detail_chars"] == len(f"Bad Request {SENTINELS['detail']}")

    cfg = accepted["body"]["config"]
    assert cfg["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert cfg["include"] == ["reasoning.encrypted_content"] and cfg["tool_choice"] == "auto"
    assert cfg["context_management"] == [{"type": "compaction", "compact_threshold": 223008}]
    assert cfg["prompt_cache_key_chars"] == len(SENTINELS["cache_key"])

    rejected_items = rejected["body"]["input"]["items"]
    assert [i["t"] for i in rejected_items] == ["message", "function_call", "function_call_output",
                                                "reasoning", "function_call", "function_call_output"]
    assert rejected_items[1]["name"] == "terminal" and rejected_items[1]["args_object"] is True
    assert rejected_items[3]["encrypted_chars"] == len(SENTINELS["reasoning_blob"])
    assert rejected["body"]["input"]["unpaired_calls"] == 0 and rejected["body"]["input"]["orphan_outputs"] == 0

    delta = shape.diff_records(accepted, rejected)
    assert delta["input_prefix_kept"] == 3
    assert delta["appended_item_types"] == ["reasoning", "function_call", "function_call_output"]
    assert delta["tools_same"] and delta["instructions_same"] and delta["same_route"]
    assert delta["keys_added"] == delta["keys_removed"] == delta["config_changed"] == []


def test_records_never_contain_text_arguments_outputs_headers_or_credentials(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        _accepted_then_rejected(monkeypatch, tmp_path)
    ring = (tmp_path / "logs" / shape.RING_FILENAME).read_bytes()
    lowered = ring.lower()
    own_logs = "\n".join(r.getMessage() for r in caplog.records if r.name == shape.__name__)
    for value in SENTINELS.values():
        assert value.encode() not in ring
        assert value not in own_logs
    for forbidden in (b"chatgpt.com", b"authorization", b"bearer", b"x-client-request-id", b"extra_headers"):
        assert forbidden not in lowered


def test_ring_and_key_are_private_and_retention_is_bounded(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path, ring=3)
    server = _Server([200] * 7)
    client = _client(server)
    for n in range(7):
        agent._current_api_request_id = f"turn-1:api:{n}"
        run_codex_stream(agent, _kwargs(), client=client)
    path = tmp_path / "logs" / shape.RING_FILENAME
    records = shape.read_records(path)
    assert [r["corr"]["api_request_id"] for r in records] == ["turn-1:api:4", "turn-1:api:5", "turn-1:api:6"]
    for name in (shape.RING_FILENAME, "codex_request_shape.key", shape.RING_FILENAME + ".lock"):
        assert stat.S_IMODE((tmp_path / "logs" / name).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700
    assert not list((tmp_path / "logs").glob("*.tmp"))


def test_oversized_record_is_reduced_to_valid_bounded_json(tmp_path):
    many = [{"type": "function_call_output", "call_id": f"call_{n}", "output": "x"} for n in range(5000)]
    path = tmp_path / "logs" / shape.RING_FILENAME
    shape.append_record(path, lambda fp: {"schema": shape.SCHEMA, "body": shape.describe_body(
        {"input": many, "tools": []}, fp)}, 5)
    line = path.read_text().strip()
    assert len(line) <= shape.MAX_RECORD_BYTES
    record = json.loads(line)
    assert record["body"]["input"]["count"] == 5000 and record["body"]["input"]["truncated"] == 5000 - shape.MAX_ITEMS


def test_fingerprints_are_keyed_but_schema_hashes_are_portable(tmp_path):
    body = {"instructions": "same", "tools": [{"type": "function", "name": "t", "parameters": {"type": "object"}}]}
    first = shape.describe_body(body, shape._fingerprinter(_mkdir(tmp_path / "a")))
    again = shape.describe_body(body, shape._fingerprinter(tmp_path / "a"))
    other = shape.describe_body(body, shape._fingerprinter(_mkdir(tmp_path / "b")))
    assert first["instructions"]["fp"] == again["instructions"]["fp"] != other["instructions"]["fp"]
    assert first["instructions"]["fp"] != hashlib.sha256(b'"same"').hexdigest()[:16]
    # Tool schemas are configuration: plain canonical SHA-256, comparable with earlier C5 evidence.
    canonical = json.dumps({"type": "object"}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert first["tools"]["items"][0]["params_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert first["tools"]["sha256"] == other["tools"]["sha256"]


def _mkdir(path):
    path.mkdir(parents=True)
    return path


def test_wire_level_suspicious_characters_are_counted(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    server = _Server([200])
    kwargs = _kwargs()
    kwargs["input"][2]["output"] = "a\x00b\x07c\U0001F600"
    run_codex_stream(agent, kwargs, client=_client(server))
    (record,) = shape.read_records(tmp_path / "logs" / shape.RING_FILENAME)
    assert record["source"] == "wire_bytes"
    assert {k: record["body"]["chars"][k] for k in ("nul", "c0", "lone_surrogate", "non_bmp")} == {
        "nul": 1, "c0": 1, "lone_surrogate": 0, "non_bmp": 1}


def test_pre_network_serialization_failure_is_recorded_from_kwargs(monkeypatch, tmp_path):
    """The SDK cannot encode a lone surrogate, so nothing is sent; the record says so instead of guessing."""
    agent = _agent(monkeypatch, tmp_path)
    server = _Server([200])
    kwargs = _kwargs()
    kwargs["input"][2]["output"] = "c\ud800d"
    with pytest.raises(UnicodeEncodeError):
        run_codex_stream(agent, kwargs, client=_client(server))
    assert server.bodies == []
    (record,) = shape.read_records(tmp_path / "logs" / shape.RING_FILENAME)
    assert record["outcome"] == "no_response" and record["source"] == "kwargs" and record["http_status"] is None
    assert record["error"]["class_chain"][0] == "UnicodeEncodeError"
    assert record["body"]["chars"]["lone_surrogate"] == 1


def test_ring_failure_never_changes_the_request_outcome(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    monkeypatch.setattr(shape, "append_record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    server = _Server([200, 400])
    client = _client(server)
    assert run_codex_stream(agent, _kwargs(), client=client).status == "completed"
    with pytest.raises(BadRequestError):
        run_codex_stream(agent, _kwargs(), client=client)
    assert len(server.bodies) == 2


def test_non_consumer_backend_gets_no_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(shape, "ring_size", lambda: 8)
    assert shape.start_probe(SimpleNamespace(_is_codex_backend=lambda: False), call_role="primary",
                             stream_attempt=1) is None
    assert shape.start_probe(SimpleNamespace(), call_role="primary", stream_attempt=1) is None


def test_client_without_wire_bytes_falls_back_to_labelled_kwargs(monkeypatch, tmp_path):
    from tests.agent.test_run_agent_codex_responses import _FakeCreateStream

    agent = _agent(monkeypatch, tmp_path)
    events = [SimpleNamespace(type="response.completed", response=SimpleNamespace(
        status="completed", output=[], usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2), id="r1"))]
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: _FakeCreateStream(events)),
                             base_url=CODEX_URL)
    run_codex_stream(agent, _kwargs(), client=client)
    (record,) = shape.read_records(tmp_path / "logs" / shape.RING_FILENAME)
    assert record["source"] == "kwargs"
    assert "extra_headers" not in record["body"]["keys"] and "input" in record["body"]["keys"]
    assert SENTINELS["header"].encode() not in (tmp_path / "logs" / shape.RING_FILENAME).read_bytes()


def test_cli_summary_diffs_rejection_against_last_accepted(monkeypatch, tmp_path, capsys):
    _accepted_then_rejected(monkeypatch, tmp_path)
    assert shape._main(["--path", str(tmp_path / "logs" / shape.RING_FILENAME)]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["outcome"] for line in lines] == ["accepted", "rejected"]
    assert lines[1]["vs_last_accepted"]["appended_item_types"] == ["reasoning", "function_call",
                                                                    "function_call_output"]

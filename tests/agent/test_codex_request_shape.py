"""Opt-in content-free shape records for consumer-Codex requests (agent/codex_request_shape.py).

The success and rejection paths run the real OpenAI SDK, with the production zstd request hook,
against an ``httpx.MockTransport``. Records therefore come from the bytes that crossed the socket,
not from Hermes' kwargs.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import stat
import time
from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError, OpenAI

from agent import codex_request_shape as shape
from agent.codex_headers import codex_request_compression_hooks
from agent.codex_runtime import run_codex_stream

zstd = pytest.importorskip("compression.zstd")

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


def _client(server: _Server, *, compress: bool = True) -> OpenAI:
    hooks = codex_request_compression_hooks(CODEX_URL) if compress else None
    assert hooks or not compress  # the production hook is active for the official Codex URL
    return OpenAI(api_key=SENTINELS["api_key"], base_url=CODEX_URL, max_retries=0,
                  http_client=httpx.Client(transport=httpx.MockTransport(server), event_hooks=hooks or {}))


def _sent_json(wire: bytes) -> dict:
    return json.loads(zstd.decompress(wire) if wire.startswith(shape._ZSTD_MAGIC) else wire)


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
        assert wire.startswith(shape._ZSTD_MAGIC)  # production consumer-Codex bodies are zstd on the wire
        assert record["source"] == "wire_bytes" and record["body"]["encoding"] == "zstd"
        assert record["body"]["wire_bytes"] == len(wire)  # exact transmitted size
        assert record["body"]["bytes"] == len(zstd.decompress(wire))  # exact JSON body size
        sent = _sent_json(wire)
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
    assert delta["partial"] is False and delta["first_divergent_index"] is None
    assert delta["input_prefix_kept"] == 3
    assert delta["appended_item_types"] == ["reasoning", "function_call", "function_call_output"]
    assert delta["tools_same"] and delta["instructions_same"] and delta["same_route"]
    assert delta["keys_added"] == delta["keys_removed"] == delta["config_changed"] == []


def test_records_never_contain_text_arguments_outputs_headers_or_credentials(monkeypatch, tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        _accepted_then_rejected(monkeypatch, tmp_path)
    ring = (tmp_path / "logs" / shape.RING_FILENAME).read_bytes()
    incidents = (tmp_path / "logs" / shape.INCIDENTS_FILENAME).read_bytes()
    assert incidents  # the rejection was pinned, so the incidents file is checked too
    own_logs = "\n".join(r.getMessage() for r in caplog.records if r.name == shape.__name__)
    for stored in (ring, incidents):
        lowered = stored.lower()
        for value in SENTINELS.values():
            assert value.encode() not in stored
            assert value not in own_logs
        for forbidden in (b"chatgpt.com", b"authorization", b"bearer", b"x-client-request-id", b"extra_headers"):
            assert forbidden not in lowered


def test_ring_and_key_are_private_and_retention_is_bounded(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path, ring=4)
    server = _Server([200] * 11)
    client = _client(server)
    path = tmp_path / "logs" / shape.RING_FILENAME
    for n in range(11):
        agent._current_api_request_id = f"turn-1:api:{n}"
        run_codex_stream(agent, _kwargs(), client=client)
        ids = [r["corr"]["api_request_id"] for r in shape.read_records(path)]
        assert 1 <= len(ids) <= 4 and ids[-1] == f"turn-1:api:{n}"  # bounded, newest always kept
        assert ids == [f"turn-1:api:{k}" for k in range(n - len(ids) + 1, n + 1)]  # contiguous newest run
    assert len(server.bodies) == 11
    for name in (shape.RING_FILENAME, "codex_request_shape.key", shape.RING_FILENAME + ".lock"):
        assert stat.S_IMODE((tmp_path / "logs" / name).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700
    assert not list((tmp_path / "logs").glob("*.tmp"))


def _tools(n):
    return [{"type": "function", "name": f"tool_{k}", "strict": False, "description": "d",
             "parameters": {"type": "object", "properties": {f"p{j}": {"type": "string"} for j in range(4)}}}
            for k in range(n)]


def _long_items(n):
    items = []
    for k in range(n):
        call = f"call_{k:032d}"
        items.append({"type": "function_call", "call_id": call, "name": "terminal", "arguments": "{}"} if k % 2 == 0
                     else {"type": "function_call_output", "call_id": f"call_{k - 1:032d}", "output": "x"})
    return items


def _record(path, body, limit=5):
    assert shape.append_record(path, lambda fp: {"schema": shape.SCHEMA, "outcome": "accepted",
                                                 "body": shape.describe_body(body, fp)}, limit)
    return shape.read_records(path)[-1]


@pytest.mark.parametrize("items", [192, 4000])
def test_realistic_long_session_record_fits_without_reduction(tmp_path, items):
    """128 tools plus a full head+tail of items still fits the per-record cap unreduced."""
    path = tmp_path / "logs" / shape.RING_FILENAME
    record = _record(path, {"input": _long_items(items), "tools": _tools(shape.MAX_TOOLS)})
    assert "reduced" not in record
    assert len(path.read_text().strip()) <= shape.MAX_RECORD_BYTES
    kept = record["body"]["input"]["items"]
    assert len(kept) == min(items, shape.HEAD_ITEMS + shape.TAIL_ITEMS)
    assert kept[-1]["i"] == items - 1  # the newest item always survives
    assert len(record["body"]["tools"]["items"]) == shape.MAX_TOOLS


def test_record_reduction_steps_stay_valid_bounded_json(tmp_path, monkeypatch):
    path = tmp_path / "logs" / shape.RING_FILENAME
    body = {"input": _long_items(400), "tools": _tools(10)}
    monkeypatch.setattr(shape, "MAX_RECORD_BYTES", 16 * 1024)
    record = _record(path, body)
    assert record["reduced"] == ["tool_items", "input_items_64"]
    assert [i["i"] for i in record["body"]["input"]["items"]][-1] == 399
    monkeypatch.setattr(shape, "MAX_RECORD_BYTES", 600)
    record = _record(path, body)
    assert record["reduced"][-1] == "minimal" and set(record["body"]) >= {"bytes", "fp", "shape_sha256"}
    for line in path.read_text().splitlines():
        json.loads(line)


def test_head_and_tail_items_keep_appended_items_visible(tmp_path):
    path = tmp_path / "logs" / shape.RING_FILENAME
    before = _long_items(301)
    after = [*before, {"type": "reasoning", "encrypted_content": "e", "summary": []}, *_long_items(2)]
    first, second = _record(path, {"input": before}), _record(path, {"input": after})
    assert second["body"]["input"]["omitted"] == 304 - shape.HEAD_ITEMS - shape.TAIL_ITEMS
    delta = shape.diff_records(first, second)
    assert delta["partial"] is True and delta["input_prefix_kept"] is None
    assert delta["appended_item_types"] == ["reasoning", "function_call", "function_call_output"]
    assert delta["items_compared"] > 0 and delta["first_divergent_index"] is None


def test_diff_reports_unknown_rather_than_empty_when_items_were_dropped():
    reduced = {"outcome": "rejected", "reduced": ["minimal"], "body": {"bytes": 10}}
    full = {"outcome": "accepted", "body": {"input": {"count": 3, "items": [{"i": 0, "fp": "a", "t": "message"}]}}}
    delta = shape.diff_records(full, reduced)
    assert delta["partial"] is True and delta["input_prefix_kept"] is None
    assert delta["appended_item_types"] is None and delta["items_compared"] is None
    assert delta["reduced"] == [None, ["minimal"]]


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
    (incident,) = shape.read_records(tmp_path / "logs" / shape.INCIDENTS_FILENAME)
    assert incident["pinned"] == record and incident["accepted"] is None  # no accepted predecessor


def test_busy_ring_lock_skips_the_record_within_the_wait_budget(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    server = _Server([200, 200])
    client = _client(server)
    run_codex_stream(agent, _kwargs(), client=client)  # creates the ring, key and lock
    lock_fd = os.open(tmp_path / "logs" / (shape.RING_FILENAME + ".lock"), os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)  # another process holds the ring
        started = time.monotonic()
        assert run_codex_stream(agent, _kwargs(), client=client).status == "completed"
        assert time.monotonic() - started < 5
    finally:
        os.close(lock_fd)
    assert len(server.bodies) == 2 and len(shape.read_records(tmp_path / "logs" / shape.RING_FILENAME)) == 1


def test_short_key_is_regenerated_instead_of_disabling_the_ring(tmp_path):
    directory = _mkdir(tmp_path / "logs")
    (directory / "codex_request_shape.key").write_bytes(b"")
    fp = shape._fingerprinter(directory)
    assert len((directory / "codex_request_shape.key").read_bytes()) == 32 and fp("x")
    assert stat.S_IMODE((directory / "codex_request_shape.key").stat().st_mode) == 0o600


def test_identity_wire_and_non_dict_ring_lines(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    server = _Server([200])
    run_codex_stream(agent, _kwargs(), client=_client(server, compress=False))
    path = tmp_path / "logs" / shape.RING_FILENAME
    with path.open("a") as handle:
        handle.write("[1, 2]\nnot json\n")
    (record,) = shape.read_records(path)
    assert record["body"]["encoding"] == "identity" and record["body"]["bytes"] == len(server.bodies[0])


def test_structured_validator_fields_are_kept_as_identifiers_only():
    request = httpx.Request("POST", CODEX_URL + "/responses")
    body = {"error": {"code": "invalid_value", "type": "invalid_request_error", "param": "input[3].content",
                      "message": SENTINELS["detail"]},
            "detail": [{"loc": ["body", "input", 3], "type": "value_error", "msg": SENTINELS["detail"]}]}
    error = BadRequestError("bad", response=httpx.Response(400, request=request), body=body)
    record = shape._error_record(error)
    assert record["error_param"] == "input[3].content" and record["error_code"] == "invalid_value"
    assert record["detail_items"] == [{"loc": ["body", "input", 3], "type": "value_error"}]
    assert SENTINELS["detail"] not in json.dumps(record)
    assert shape._error_record(BadRequestError("bad", response=httpx.Response(400, request=request),
                                               body={"error": {"param": "x\n"}}))["error_param"] == "other"


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


def test_rejected_pair_is_pinned_and_survives_ring_eviction(monkeypatch, tmp_path, capsys):
    agent = _agent(monkeypatch, tmp_path, ring=4)
    other = "local-" + "cd" * 32
    server = _Server([200, 200, 400] + [200] * 12)
    client = _client(server)
    run_codex_stream(agent, _kwargs(), client=client)  # the predecessor to pin: turn-1:api:1
    agent.session_id, agent._current_api_request_id = other, "other:api:1"
    run_codex_stream(agent, _kwargs(), client=client)  # newer accepted record, different session
    agent.session_id, agent._current_api_request_id = "local-" + "ab" * 32, "turn-1:api:2"
    with pytest.raises(BadRequestError):
        run_codex_stream(agent, _kwargs(_follow_up_items()), client=client)
    ring = tmp_path / "logs" / shape.RING_FILENAME
    for n in range(12):  # busy traffic scrolls both records out of the ring
        agent._current_api_request_id = f"turn-2:api:{n}"
        run_codex_stream(agent, _kwargs(), client=client)
    assert all(r["outcome"] == "accepted" and r["corr"]["api_request_id"].startswith("turn-2")
               for r in shape.read_records(ring))

    (incident,) = shape.read_records(tmp_path / "logs" / shape.INCIDENTS_FILENAME)
    assert incident["schema"] == shape.INCIDENT_SCHEMA and set(incident) == {"schema", "pinned", "accepted"}
    assert incident["pinned"]["outcome"] == "rejected" and incident["pinned"]["corr"]["api_request_id"] == "turn-1:api:2"
    # The same-session predecessor, not the newer accepted record from another session.
    assert incident["accepted"]["corr"] == {"session_id": "local-" + "ab" * 32, "api_request_id": "turn-1:api:1"}
    # Pinned records are the ring records unchanged: same keys, same body description.
    assert set(incident["pinned"]) == {"schema", "probe", "route", "corr", "ts", "outcome", "source", "body",
                                       "http_status", "error"}

    assert shape._main(["--incidents", "--path", str(tmp_path / "logs" / shape.INCIDENTS_FILENAME)]) == 0
    (line,) = [json.loads(out) for out in capsys.readouterr().out.splitlines()]
    assert line["outcome"] == "rejected" and line["api_request_id"] == "turn-1:api:2"
    assert line["vs_last_accepted"] == shape.diff_records(incident["accepted"], incident["pinned"])
    assert line["vs_last_accepted"]["appended_item_types"] == ["reasoning", "function_call", "function_call_output"]
    assert shape._main(["--incidents", "--session", other,
                        "--path", str(tmp_path / "logs" / shape.INCIDENTS_FILENAME)]) == 0
    assert capsys.readouterr().out == ""


def _synthetic(path, outcome, status, n, session="local-s1"):
    record = {"schema": shape.SCHEMA, "outcome": outcome, "http_status": status,
              "corr": {"session_id": session, "api_request_id": f"api:{n}"}, "body": {"bytes": n}}
    assert shape.append_record(path, lambda fp: record, 4, incidents=shape.incidents_path(path))


def test_incidents_file_is_bounded_private_and_keeps_a_rare_failure_through_a_burst(tmp_path):
    path = _mkdir(tmp_path / "logs") / shape.RING_FILENAME
    incidents = shape.incidents_path(path)
    _synthetic(path, "accepted", 200, 0)
    _synthetic(path, "rejected", 400, 1)
    for n in range(2, 2 + 3 * shape.MAX_INCIDENTS):  # a long burst of another failure class
        _synthetic(path, "rejected", 429, n, session="local-s2")
        lines = incidents.read_bytes().splitlines()
        assert len(lines) <= shape.MAX_INCIDENTS
        assert all(len(raw) <= 2 * shape.MAX_RECORD_BYTES + 128 for raw in lines)
    pinned = [entry["pinned"] for entry in shape.read_records(incidents)]
    assert len(pinned) == shape.MAX_INCIDENTS
    assert pinned[0]["http_status"] == 400  # the rare 400 survives sixty rate limits
    last = 1 + 3 * shape.MAX_INCIDENTS
    assert [p["corr"]["api_request_id"] for p in pinned[1:]] == [
        f"api:{n}" for n in range(last - shape.MAX_INCIDENTS + 2, last + 1)]  # newest of the burst
    first = shape.read_records(incidents)[0]
    assert first["accepted"]["corr"]["api_request_id"] == "api:0"
    assert stat.S_IMODE(incidents.stat().st_mode) == 0o600
    assert not list(path.parent.glob("*.tmp")) and not list(path.parent.glob(".*.tmp"))
    # Accepted records are never pinned, and an unknown session never pairs with another.
    before = incidents.read_bytes()
    _synthetic(path, "accepted", 200, 99)
    assert incidents.read_bytes() == before
    _synthetic(path, "accepted", 200, 100, session="absent")
    _synthetic(path, "no_response", None, 101, session="absent")
    assert shape.read_records(incidents)[-1]["accepted"] is None


def test_oversized_records_are_pinned_within_the_record_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(shape, "MAX_RECORD_BYTES", 2000)
    path = _mkdir(tmp_path / "logs") / shape.RING_FILENAME
    big = shape.describe_body({"input": _long_items(300), "tools": _tools(10)}, shape._fingerprinter(path.parent))
    for outcome, status in (("accepted", 200), ("rejected", 400)):
        assert shape.append_record(path, lambda fp, o=outcome, s=status: {
            "schema": shape.SCHEMA, "outcome": o, "http_status": s,
            "corr": {"session_id": "local-s1"}, "body": big}, 4, incidents=shape.incidents_path(path))
    (incident,) = shape.read_records(shape.incidents_path(path))
    assert incident["pinned"]["reduced"][-1] == "minimal" and incident["accepted"]["reduced"][-1] == "minimal"
    assert len(shape.incidents_path(path).read_bytes()) <= 2 * shape.MAX_RECORD_BYTES + 128


def test_incident_pin_failure_never_changes_the_request_or_the_ring(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / shape.INCIDENTS_FILENAME).mkdir()  # unwritable as a file
    server = _Server([200, 400, 400])
    client = _client(server)
    assert run_codex_stream(agent, _kwargs(), client=client).status == "completed"
    with pytest.raises(BadRequestError):
        run_codex_stream(agent, _kwargs(), client=client)
    monkeypatch.setattr(shape, "_pin_incident", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(BadRequestError):
        run_codex_stream(agent, _kwargs(), client=client)
    assert len(server.bodies) == 3
    assert [r["outcome"] for r in shape.read_records(tmp_path / "logs" / shape.RING_FILENAME)] == [
        "accepted", "rejected", "rejected"]


def test_append_record_contains_its_own_pin_failure(tmp_path):
    """append_record itself absorbs a pin failure (not only the probe's outer guard), without leaking fds."""
    path = _mkdir(tmp_path / "logs") / shape.RING_FILENAME
    blocked = _mkdir(tmp_path / "logs" / "blocked")
    fds_before = len(os.listdir("/proc/self/fd"))
    for n in range(20):
        assert shape.append_record(path, lambda fp, n=n: {"outcome": "rejected", "http_status": 400,
                                                          "corr": {"session_id": "local-s1"}, "body": {"bytes": n}},
                                   4, incidents=blocked) is True
    assert len(os.listdir("/proc/self/fd")) <= fds_before + 1
    assert 1 <= len(shape.read_records(path)) <= 4 and blocked.is_dir() and not list(blocked.iterdir())


def test_incident_lines_without_a_pinned_record_are_dropped(tmp_path):
    path = _mkdir(tmp_path / "logs") / shape.RING_FILENAME
    shape.incidents_path(path).write_text('{"pinned": 1}\n[1]\nnot json\n')
    _synthetic(path, "rejected", 400, 1)
    (incident,) = shape.read_records(shape.incidents_path(path))
    assert incident["pinned"]["http_status"] == 400

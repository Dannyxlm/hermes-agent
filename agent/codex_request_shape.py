"""Opt-in, content-free shape records for consumer-Codex requests at the wire boundary.

A generic ``{"detail": "Bad Request"}`` 400 from the ChatGPT-account Codex endpoint names no
field, and the only retained request evidence (``request_dump_*``) is written for the terminal
failure alone, from SDK kwargs rather than transmitted bytes. That leaves nothing to compare a
rejected request against the successful requests just before it.

Off by default. ``agent.codex_request_shape_ring: N`` (N > 0, capped at ``MAX_RING``) makes every
consumer-Codex Responses request append one record, accepted or rejected, to a private ring at
``$HERMES_HOME/logs/codex_request_shape.jsonl`` holding at most the newest N records (when full it
trims to the newest three quarters). Records are derived from the bytes that crossed the socket
(the ``httpx.Request`` content after the zstd request hook, decoded here) when the client exposes
them, otherwise from the final kwargs; ``source`` and ``encoding`` say which.

Retained: key/type signatures, item-type sequences, counts, sizes, allowlisted configuration
scalars, routing metadata, local correlation IDs, plain SHA-256 of tool schemas (canonical JSON,
comparable across installs) and keyed HMAC fingerprints of content. Never retained: headers,
credentials, URLs, message text, tool arguments, tool output, error messages.

A rare rejection would scroll out of the ring within hours of busy traffic, so every record that is
not ``accepted`` is also pinned, together with the newest ``accepted`` record from the same session
still in the ring, to ``codex_request_shape.incidents.jsonl`` beside it. That file holds at most
``MAX_INCIDENTS`` pairs; when full it evicts the oldest incident of its most frequent outcome/status
class, so a burst of one kind of failure (say rate limits) cannot push out a single different one.
Pinned pairs are the same records, unchanged; ``--incidents`` diffs each pair.

Fingerprints are still private: the HMAC key, ring, incidents file and lock live 0600 beside the
other private logs, and a request is never altered or delayed by a failure in here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SCHEMA = "codex-request-shape.v1"
CONFIG_KEY = "codex_request_shape_ring"  # under ``agent:``
MAX_RING = 200
MAX_RECORD_BYTES = 96 * 1024
# Long sessions are where follow-up 400s happen, so the newest items matter most: keep a head (the
# stable prefix) and a larger tail (what was just appended), each item carrying its original index.
HEAD_ITEMS = 32
TAIL_ITEMS = 160
MAX_TOOLS = 128
MAX_DECODED_WIRE_BYTES = 64 * 1024 * 1024
LOCK_WAIT_SECONDS = 0.25
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
RING_FILENAME = "codex_request_shape.jsonl"
INCIDENTS_FILENAME = "codex_request_shape.incidents.jsonl"
INCIDENT_SCHEMA = "codex-request-shape-incident.v1"
MAX_INCIDENTS = 20
_KEY_FILENAME = "codex_request_shape.key"

_TOKEN = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")  # local correlation ids (session/api-request), never secrets
_PARAM = re.compile(r"^[A-Za-z0-9_.:\[\]\-]{1,128}$")  # validator field paths such as input[3].content
_SUSPECT = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\U00010000-\U0010ffff]")
_ROLES = frozenset({"user", "assistant", "system", "developer", "tool"})
_SDK_ONLY_KWARGS = frozenset({"extra_headers", "extra_query", "extra_body", "timeout"})
_SCALAR_CONFIG = ("model", "store", "stream", "parallel_tool_calls", "service_tier", "truncation",
                  "max_output_tokens", "max_tool_calls", "temperature", "top_p", "background")
_LOCK = threading.Lock()
_KEYS: dict[str, Any] = {}  # directory -> _Fingerprinter, loaded once per process


def ring_size() -> int:
    """Configured ring size; 0 (off) for anything other than a positive int."""
    try:
        from hermes_cli.config import load_config_readonly

        agent_cfg = load_config_readonly().get("agent")
        value = agent_cfg.get(CONFIG_KEY) if isinstance(agent_cfg, dict) else None
    except Exception:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, MAX_RING))


def ring_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / RING_FILENAME


def incidents_path(ring: Path | None = None) -> Path:
    return (ring or ring_path()).with_name(INCIDENTS_FILENAME)


# ── content-free derivation ───────────────────────────────────────────────────────────────

def _ident(value: Any) -> str:
    text = str(value or "")
    return "absent" if not text else text if _ID.fullmatch(text) else "other"


def _token(value: Any) -> str:
    if value is None:
        return "absent"
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else "other"


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "other"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str).encode("utf-8", "surrogatepass")


def _shape(value: Any) -> Any:
    """Keys and list order kept; every scalar replaced by its JSON type name."""
    if isinstance(value, dict):
        return {str(k): _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(v) for v in value]
    return _json_type(value)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _depth(value: Any, level: int = 0) -> int:
    if isinstance(value, dict):
        return max((_depth(v, level + 1) for v in value.values()), default=level + 1)
    if isinstance(value, list):
        return max((_depth(v, level + 1) for v in value), default=level + 1)
    return level


def _char_counts(value: Any, counts: dict[str, int]) -> None:
    """Counts of characters a JSON-only validator may reject; never the characters' context."""
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            counts["strings"] += 1
            for ch in _SUSPECT.findall(current):  # one C-speed scan; the common case finds nothing
                code = ord(ch)
                key = ("nul" if code == 0 else "lone_surrogate" if 0xD800 <= code <= 0xDFFF
                       else "non_bmp" if code > 0xFFFF else "c0")
                counts[key] += 1
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _text_len(value: Any) -> dict[str, int] | None:
    if not isinstance(value, str):
        return None
    return {"chars": len(value), "bytes": len(value.encode("utf-8", "surrogatepass"))}


class _Fingerprinter:
    def __init__(self, key: bytes):
        self._key = key

    def __call__(self, value: Any) -> str:
        return self.raw(_canonical(value))

    def raw(self, data: bytes) -> str:
        return hmac.new(self._key, data, hashlib.sha256).hexdigest()[:16]


def _keys(mapping: dict) -> str:
    return ",".join(sorted(_token(k) for k in mapping))


def _item_record(index: int, item: Any, fp: _Fingerprinter) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"i": index, "t": _json_type(item), "bytes": len(_canonical(item)), "fp": fp(item)}
    kind = item.get("type")
    rec: dict[str, Any] = {
        "i": index,
        "t": _token(kind) if kind is not None else ("message" if "role" in item else "absent"),
        "keys": _keys(item),
        "bytes": len(_canonical(item)),
        "fp": fp(item),
    }
    if "role" in item:
        role = item.get("role")
        rec["role"] = role if role in _ROLES else "other"
    content = item.get("content")
    if isinstance(content, list):
        rec["parts"] = [_token(p.get("type")) if isinstance(p, dict) else _json_type(p) for p in content[:32]]
    elif content is not None:
        rec["content"] = _json_type(content)
    if "name" in item:
        rec["name"] = _token(item.get("name"))
    if "call_id" in item:
        call_id = item.get("call_id")
        rec["call_id_chars"] = len(call_id) if isinstance(call_id, str) else -1
    if "arguments" in item:
        args = item.get("arguments")
        rec["args_bytes"] = len(args.encode("utf-8", "surrogatepass")) if isinstance(args, str) else -1
        parsed = None
        with suppress(Exception):
            parsed = json.loads(args) if isinstance(args, str) else None
        rec["args_object"] = isinstance(parsed, dict)
    if "output" in item:
        output = item.get("output")
        rec["output"] = _json_type(output)
        rec["output_bytes"] = len(_canonical(output)) if not isinstance(output, str) else len(
            output.encode("utf-8", "surrogatepass"))
    if "encrypted_content" in item:
        rec["encrypted_chars"] = len(item["encrypted_content"]) if isinstance(item["encrypted_content"], str) else -1
    if "id" in item:
        rec["has_id"] = True
    return rec


def _pairing(items: list) -> dict[str, int]:
    calls: list[str] = []
    outputs: list[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("call_id"), str):
            if item.get("type") == "function_call":
                calls.append(item["call_id"])
            elif item.get("type") == "function_call_output":
                outputs.append(item["call_id"])
    call_set, output_set = set(calls), set(outputs)
    return {"calls": len(calls), "outputs": len(outputs),
            "unpaired_calls": len(call_set - output_set), "orphan_outputs": len(output_set - call_set),
            "duplicate_call_ids": len(calls) - len(call_set)}


def _tools_record(tools: Any) -> dict[str, Any]:
    if not isinstance(tools, list):
        return {"type": _json_type(tools)}
    entries = []
    for tool in tools[:MAX_TOOLS]:
        if not isinstance(tool, dict):
            entries.append({"type": _json_type(tool)})
            continue
        params = tool.get("parameters")
        entry = {"type": _token(tool.get("type")), "name": _token(tool.get("name")), "keys": _keys(tool)}
        if "strict" in tool:
            entry["strict"] = tool["strict"] if isinstance(tool["strict"], bool) else "other"
        if params is not None:
            entry["params_sha256"] = _sha256(params)
            entry["params_shape_sha256"] = _sha256(_shape(params))
            entry["params_bytes"] = len(_canonical(params))
        entries.append(entry)
    return {"count": len(tools), "sha256": _sha256(tools), "shape_sha256": _sha256(_shape(tools)),
            "bytes": len(_canonical(tools)), "items": entries, "truncated": max(0, len(tools) - MAX_TOOLS)}


def _config_record(body: dict) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for key in _SCALAR_CONFIG:
        if key in body:
            value = body[key]
            cfg[key] = value if isinstance(value, (bool, int, float)) else _token(value)
    choice = body.get("tool_choice")
    if choice is not None:
        cfg["tool_choice"] = _token(choice) if isinstance(choice, str) else {
            "type": _token(choice.get("type")) if isinstance(choice, dict) else _json_type(choice)}
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        cfg["reasoning"] = {str(_token(k)): _token(v) if not isinstance(v, (bool, int, float)) else v
                            for k, v in reasoning.items()}
    elif reasoning is not None:
        cfg["reasoning"] = _json_type(reasoning)
    include = body.get("include")
    if isinstance(include, list):
        cfg["include"] = [_token(v) for v in include[:16]]
    text = body.get("text")
    if isinstance(text, dict):
        fmt = text.get("format")
        cfg["text"] = {"keys": _keys(text),
                       "format_type": _token(fmt.get("type")) if isinstance(fmt, dict) else _json_type(fmt)}
    management = body.get("context_management")
    if isinstance(management, list):
        cfg["context_management"] = [
            {"type": _token(d.get("type")), **({"compact_threshold": d["compact_threshold"]}
             if isinstance(d.get("compact_threshold"), int) else {})} if isinstance(d, dict) else _json_type(d)
            for d in management[:8]]
    elif management is not None:
        cfg["context_management"] = _json_type(management)
    for key in ("prompt_cache_key", "previous_response_id", "prompt_cache_retention"):
        if key in body:
            cfg[f"{key}_chars"] = len(body[key]) if isinstance(body[key], str) else -1
    if isinstance(body.get("metadata"), dict):
        cfg["metadata_keys"] = len(body["metadata"])
    return cfg


def _kept_indices(count: int, head: int = HEAD_ITEMS, tail: int = TAIL_ITEMS) -> list[int]:
    if count <= head + tail:
        return list(range(count))
    return [*range(head), *range(count - tail, count)]


def describe_body(body: Any, fp: _Fingerprinter) -> dict[str, Any]:
    """Content-free description of one request body (already in wire form)."""
    if not isinstance(body, dict):
        return {"body_type": _json_type(body)}
    counts = {"strings": 0, "nul": 0, "c0": 0, "lone_surrogate": 0, "non_bmp": 0}
    _char_counts(body, counts)
    items = body.get("input")
    described: dict[str, Any] = {
        "keys": {_token(k): _json_type(v) for k, v in sorted(body.items())},
        "shape_sha256": _sha256(_shape(body)),
        "depth": _depth(body),
        "chars": counts,
        "config": _config_record(body),
        "instructions": None,
        "input": None,
        "tools": _tools_record(body["tools"]) if "tools" in body else None,
    }
    instructions = body.get("instructions")
    if instructions is not None:
        described["instructions"] = {**(_text_len(instructions) or {"type": _json_type(instructions)}),
                                     "fp": fp(instructions)}
    if isinstance(items, list):
        described["input"] = {"count": len(items), "bytes": len(_canonical(items)),
                              "items": [_item_record(i, items[i], fp) for i in _kept_indices(len(items))],
                              "omitted": max(0, len(items) - HEAD_ITEMS - TAIL_ITEMS), **_pairing(items)}
    elif items is not None:
        described["input"] = {"type": _json_type(items), **(_text_len(items) or {})}
    return described


def body_from_kwargs(wire_kwargs: dict) -> dict:
    """The JSON body the SDK builds: typed kwargs, minus SDK-only options, with extra_body merged last."""
    body = {k: v for k, v in wire_kwargs.items() if k not in _SDK_ONLY_KWARGS and v is not None}
    extra = wire_kwargs.get("extra_body")
    if isinstance(extra, dict):
        body.update(extra)
    return body


def _request_bytes(obj: Any) -> bytes | None:
    with suppress(Exception):
        content = getattr(getattr(obj, "request", None), "content", None)
        if isinstance(content, (bytes, bytearray, memoryview)):
            return bytes(content)
        if isinstance(content, str):
            return content.encode("utf-8")
    return None


def decode_wire(data: bytes) -> tuple[bytes | None, str]:
    """(JSON bytes, content encoding). The official Codex client zstd-compresses request bodies
    (``agent.codex_headers.codex_request_compression_hooks``), so the raw wire is rarely JSON."""
    if not data.startswith(_ZSTD_MAGIC):
        return data, "identity"
    try:
        from compression import zstd
    except ImportError:
        return None, "zstd_undecodable"
    decompressor = zstd.ZstdDecompressor()
    try:
        decoded = decompressor.decompress(data, max_length=MAX_DECODED_WIRE_BYTES)
    except Exception:
        return None, "zstd_undecodable"
    if not decompressor.eof:
        return None, "zstd_oversized"
    return decoded, "zstd"


def _wire_bytes_from_stream(stream: Any) -> bytes | None:
    with suppress(Exception):
        return _request_bytes(getattr(stream, "response", None))
    return None


def _wire_bytes_from_error(error: BaseException) -> bytes | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        found = _request_bytes(current)
        if found is None:
            with suppress(Exception):
                found = _request_bytes(getattr(current, "response", None))
        if found is not None:
            return found
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return None


def _param(value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if value is None:
        return "absent"
    return value if isinstance(value, str) and _PARAM.fullmatch(value) else "other"


def _error_record(error: BaseException) -> dict[str, Any]:
    chain: list[str] = []
    current: BaseException | None = error
    while current is not None and len(chain) < 4:
        chain.append(type(current).__name__)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    status = getattr(error, "status_code", None)
    rec: dict[str, Any] = {"class_chain": chain,
                           "http_status": status if isinstance(status, int) and not isinstance(status, bool) else None}
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        rec["body_keys"] = sorted(_token(k) for k in body)
        detail = body.get("detail")
        if isinstance(detail, str):
            rec["detail_chars"] = len(detail)
        elif isinstance(detail, list):  # FastAPI-style validation list: field locations and codes only
            rec["detail_items"] = [
                {"loc": [_param(p) for p in d.get("loc", [])[:8]] if isinstance(d.get("loc"), list) else [],
                 "type": _token(d.get("type"))} if isinstance(d, dict) else {"type": _json_type(d)}
                for d in detail[:8]]
        elif detail is not None:
            rec["detail_type"] = _json_type(detail)
        error_obj = body.get("error")
        nested: dict = error_obj if isinstance(error_obj, dict) else body
        # Structured validator fields are identifiers (field paths, codes), kept only as safe tokens.
        for key in ("code", "type", "param"):
            if key in nested:
                value = nested[key]
                rec[f"error_{key}"] = (value if isinstance(value, int) and not isinstance(value, bool)
                                       else _param(value) if key == "param" else _token(value))
        message = nested.get("message")
        if isinstance(message, str):
            rec["message_chars"] = len(message)
    elif body is not None:
        rec["body_type"] = _json_type(body)
    return rec


# ── bounded private ring ─────────────────────────────────────────────────────────────────

def _private_open(path: Path, flags: int) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)


def _write_private(path: Path, data: bytes) -> Path:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = _private_open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        with suppress(FileNotFoundError):
            tmp.unlink()
        raise
    return tmp


def _replace_private(path: Path, data: bytes) -> None:
    tmp = _write_private(path, data)
    try:
        os.replace(tmp, path)
    finally:
        with suppress(FileNotFoundError):
            tmp.unlink()


def _read_private(path: Path) -> bytes:
    with suppress(FileNotFoundError):
        fd = _private_open(path, os.O_RDONLY)
        try:
            handle = os.fdopen(fd, "rb")
        except BaseException:  # e.g. a directory at ``path``: don't leak the descriptor
            os.close(fd)
            raise
        with handle:
            return handle.read()
    return b""


def _fingerprinter(directory: Path) -> _Fingerprinter:
    """Load the per-home HMAC key; create it complete (temp + replace) or replace a short one.

    Called under the ring lock. A regenerated key only breaks fingerprint comparison with older
    records; every other field stays comparable.
    """
    key_path = directory / _KEY_FILENAME
    with suppress(FileNotFoundError):
        fd = _private_open(key_path, os.O_RDONLY)
        with os.fdopen(fd, "rb") as handle:
            key = handle.read(4096)
        if len(key) >= 32:
            return _Fingerprinter(key)
    key = os.urandom(32)
    tmp = _write_private(key_path, key)
    try:
        os.replace(tmp, key_path)
    finally:
        with suppress(FileNotFoundError):
            tmp.unlink()
    return _Fingerprinter(key)


@contextmanager
def _ring_lock(directory: Path) -> Iterator[bool]:
    """Yield True once held. Bounded: a stuck holder (or an NFS home) costs at most
    ``LOCK_WAIT_SECONDS`` and one skipped record, never a stalled request."""
    if not _LOCK.acquire(timeout=LOCK_WAIT_SECONDS):
        yield False
        return
    try:
        fd = _private_open(directory / (RING_FILENAME + ".lock"), os.O_RDWR | os.O_CREAT)
        try:
            try:
                import fcntl
            except ImportError:  # Windows: in-process lock only
                yield True
                return
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        yield False
                        return
                    time.sleep(0.01)
            yield True
        finally:
            os.close(fd)
    finally:
        _LOCK.release()


def _dumps(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _encode(record: dict) -> str:
    """One JSONL line within ``MAX_RECORD_BYTES``, reduced progressively and labelled ``reduced``."""
    line = _dumps(record)
    if len(line) <= MAX_RECORD_BYTES:
        return line
    reduced = json.loads(line)
    body = reduced.get("body") or {}
    steps: list[str] = []
    tools = body.get("tools")
    if isinstance(tools, dict) and "items" in tools:  # tools rarely change; their sha256 still compares
        tools["items_dropped"] = len(tools.pop("items"))
        steps.append("tool_items")
    items_section = body.get("input")
    for keep in (64, 16, None):
        reduced["reduced"] = steps
        line = _dumps(reduced)
        if len(line) <= MAX_RECORD_BYTES:
            return line
        if keep is not None and isinstance(items_section, dict) and isinstance(items_section.get("items"), list):
            items = items_section["items"]
            # Head 8 (the stable prefix) plus the newest ``keep``; each item keeps its original index.
            items_section["items"] = [*items[:8], *items[8:][-keep:]] if len(items) > keep + 8 else items
            steps.append(f"input_items_{keep}")
    reduced["body"] = {k: body.get(k) for k in ("bytes", "wire_bytes", "encoding", "fp", "shape_sha256", "depth", "chars")}
    reduced["reduced"] = [*steps, "minimal"]
    return _dumps(reduced)


def _newest_accepted(lines: list[bytes], session: Any) -> dict | None:
    """The newest ``accepted`` ring record from ``session``; None for an unknown session."""
    if not isinstance(session, str) or not _ID.fullmatch(session) or session in ("absent", "other"):
        return None
    needle = _dumps({"session_id": session})[1:-1].encode("ascii")  # exact serialized form
    for entry in reversed(lines):
        if b'"outcome":"accepted"' not in entry or needle not in entry:
            continue  # cheap byte filter; only candidates are parsed
        with suppress(ValueError):
            value = json.loads(entry)
            if (isinstance(value, dict) and value.get("outcome") == "accepted"
                    and (value.get("corr") or {}).get("session_id") == session):
                return value
    return None


def _incident_class(pinned: Any) -> str:
    if not isinstance(pinned, dict):
        return "unknown"
    return f"{pinned.get('outcome')}:{pinned.get('http_status')}"


def _pin_incident(path: Path, record: dict, line: str, ring_lines: list[bytes]) -> None:
    """Pin ``record`` and its same-session accepted predecessor. Called under the ring lock with the
    ring as it was before ``record``; ``line`` is the record's already size-capped encoding."""
    accepted = _newest_accepted(ring_lines, (record.get("corr") or {}).get("session_id"))
    pair = _encode(accepted) if accepted is not None else "null"
    entry = f'{{"accepted":{pair},"pinned":{line},"schema":"{INCIDENT_SCHEMA}"}}'
    incidents: list[tuple[str, bytes]] = []
    for raw in _read_private(path).split(b"\n"):
        if not raw.strip():
            continue
        with suppress(ValueError):
            value = json.loads(raw)
            if isinstance(value, dict) and isinstance(value.get("pinned"), dict):
                incidents.append((_incident_class(value["pinned"]), raw))
    incidents.append((_incident_class(record), entry.encode("ascii")))
    while len(incidents) > MAX_INCIDENTS:
        # Evict the oldest incident of the most frequent class: a burst of one failure kind
        # cannot push out a rare one.
        counts = Counter(cls for cls, _ in incidents)
        top = max(counts.values())
        del incidents[next(i for i, (cls, _) in enumerate(incidents) if counts[cls] == top)]
    _replace_private(path, b"".join(raw + b"\n" for _, raw in incidents))


def append_record(path: Path, record_factory, limit: int, *, incidents: Path | None = None) -> bool:
    """Build (with the private fingerprint key) and append one record.

    At most ``limit`` records are on disk. Reaching the limit trims to the newest three quarters,
    so the file is rewritten once every ``limit // 4`` requests rather than on every request.
    With ``incidents``, a record that is not ``accepted`` is also pinned there (see module doc).
    Returns False when the lock was not obtained in time and the record was skipped.
    """
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    limit = max(1, limit)
    cache_key = str(directory)
    fp = _KEYS.get(cache_key)
    if fp is None:
        with _ring_lock(directory) as held:
            if not held:
                return False
            fp = _KEYS[cache_key] = _fingerprinter(directory)
    # Describe outside the lock: other requests wait only for the append itself.
    record = record_factory(fp)
    line = _encode(record)
    data = (line + "\n").encode("ascii")
    with _ring_lock(directory) as held:
        if not held:
            return False
        existing = _read_private(path)
        lines = [entry for entry in existing.split(b"\n") if entry.strip()]
        if len(lines) < limit:
            fd = _private_open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            with os.fdopen(fd, "ab") as handle:
                if existing and not existing.endswith(b"\n"):
                    handle.write(b"\n")
                handle.write(data)
        else:
            keep = max(0, limit - max(1, limit // 4) - 1)
            kept = lines[len(lines) - keep:] if keep else []
            _replace_private(path, b"".join(entry + b"\n" for entry in kept) + data)
        if incidents is not None and record.get("outcome") != "accepted":
            try:
                _pin_incident(incidents, record, line, lines)
            except Exception:
                logger.debug("codex request-shape incident not pinned", exc_info=True)
        return True


def read_records(path: Path | None = None) -> list[dict]:
    path = path or ring_path()
    records = []
    with suppress(FileNotFoundError):
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            with suppress(ValueError):
                value = json.loads(line)
                if isinstance(value, dict):
                    records.append(value)
    return records


# ── request-path probe ───────────────────────────────────────────────────────────────────

class RequestShapeProbe:
    """One physical request attempt. Methods never raise and never touch the request."""

    def __init__(self, agent: Any, limit: int, *, call_role: str, stream_attempt: int):
        self.limit = limit
        self.path = ring_path()
        self.base = {
            "schema": SCHEMA,
            "probe": uuid.uuid4().hex[:12],
            "route": {"provider": _token(getattr(agent, "provider", None)),
                      "model": _token(getattr(agent, "model", None)),
                      "api_mode": "codex_responses", "call_role": _token(call_role),
                      "fallback_index": int(getattr(agent, "_fallback_index", 0) or 0),
                      "stream_attempt": int(stream_attempt)},
            "corr": {"session_id": _ident(getattr(agent, "session_id", None)),
                     "api_request_id": _ident(getattr(agent, "_current_api_request_id", None))},
        }

    def _write(self, outcome: str, wire_kwargs: dict, wire: bytes | None, extra: dict) -> None:
        def build(fp: _Fingerprinter) -> dict:
            body: Any = None
            encoding = decoded = None
            if wire is not None:
                decoded, encoding = decode_wire(wire)
                source = "wire_bytes"
                try:
                    body = json.loads(decoded) if decoded is not None else None
                except ValueError:
                    body = None
                if body is None:
                    source = "wire_bytes_unparsed"
            else:
                body, source = body_from_kwargs(wire_kwargs), "kwargs"
            described = describe_body(body, fp) if body is not None else {}
            # bytes: JSON body size; wire_bytes: what crossed the socket (compressed when zstd).
            described["bytes"] = len(_canonical(body)) if source == "kwargs" else (
                len(decoded) if body is not None and decoded is not None else None)
            described["wire_bytes"] = len(wire) if wire is not None else None
            described["encoding"] = encoding
            described["fp"] = fp(body) if body is not None else fp.raw(wire or b"")
            return {**self.base, "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                    "outcome": outcome, "source": source, "body": described, **extra}

        try:
            if not append_record(self.path, build, self.limit, incidents=incidents_path(self.path)):
                logger.debug("codex request-shape record skipped: ring lock busy")
        except Exception:
            logger.debug("codex request-shape record skipped", exc_info=True)

    def accepted(self, wire_kwargs: dict, stream: Any) -> None:
        status = None
        with suppress(Exception):
            status = getattr(getattr(stream, "response", None), "status_code", None)
        self._write("accepted", wire_kwargs, _wire_bytes_from_stream(stream),
                    {"http_status": status if isinstance(status, int) and not isinstance(status, bool) else None})

    def failed(self, wire_kwargs: dict, error: BaseException) -> None:
        try:
            err = _error_record(error)
        except Exception:
            err = {"class_chain": [type(error).__name__]}
        outcome = "rejected" if err.get("http_status") is not None else "no_response"
        self._write(outcome, wire_kwargs, _wire_bytes_from_error(error),
                    {"http_status": err.get("http_status"), "error": err})


def start_probe(agent: Any, *, call_role: str, stream_attempt: int) -> RequestShapeProbe | None:
    """A probe for consumer-Codex requests when the ring is enabled, else ``None`` (zero work)."""
    try:
        predicate = getattr(agent, "_is_codex_backend", None)
        if not (callable(predicate) and bool(predicate())):
            return None
        limit = ring_size()
        if limit <= 0:
            return None
        return RequestShapeProbe(agent, limit, call_role=call_role, stream_attempt=stream_attempt)
    except Exception:
        logger.debug("codex request-shape probe unavailable", exc_info=True)
        return None


# ── offline comparison ───────────────────────────────────────────────────────────────────

def diff_records(previous: dict, current: dict) -> dict[str, Any]:
    """What changed between two records (typically last accepted → rejected in one session).

    Items are compared by original index. When either record omitted or reduced items, the
    comparison covers only the indices both kept and ``partial`` says so; unknowns stay ``None``.
    """
    prev_body, cur_body = previous.get("body") or {}, current.get("body") or {}
    prev_input, cur_input = prev_body.get("input") or {}, cur_body.get("input") or {}
    prev_items = {i.get("i"): i for i in prev_input.get("items") or [] if isinstance(i, dict)}
    cur_items = {i.get("i"): i for i in cur_input.get("items") or [] if isinstance(i, dict)}
    prev_count, cur_count = prev_input.get("count"), cur_input.get("count")
    have_items = "items" in prev_input and "items" in cur_input
    shared = sorted(set(prev_items) & set(cur_items) - {None})
    divergent = next((i for i in shared if prev_items[i].get("fp") != cur_items[i].get("fp")), None)
    complete = (have_items and isinstance(prev_count, int) and isinstance(cur_count, int)
                and len(prev_items) == prev_count and len(cur_items) == cur_count)
    appended = None
    if have_items and isinstance(prev_count, int) and isinstance(cur_count, int):
        appended = [cur_items[i].get("t") if i in cur_items else "omitted" for i in range(prev_count, cur_count)]
    prev_keys, cur_keys = prev_body.get("keys") or {}, cur_body.get("keys") or {}
    prev_cfg, cur_cfg = prev_body.get("config") or {}, cur_body.get("config") or {}
    prev_tools, cur_tools = prev_body.get("tools") or {}, cur_body.get("tools") or {}
    prev_chars, cur_chars = prev_body.get("chars") or {}, cur_body.get("chars") or {}
    return {
        "outcomes": [previous.get("outcome"), current.get("outcome")],
        "sources": [previous.get("source"), current.get("source")],
        "reduced": [previous.get("reduced"), current.get("reduced")],
        "partial": not complete,
        "same_route": previous.get("route") == current.get("route"),
        "keys_added": sorted(set(cur_keys) - set(prev_keys)),
        "keys_removed": sorted(set(prev_keys) - set(cur_keys)),
        "key_type_changed": sorted(k for k in set(prev_keys) & set(cur_keys) if prev_keys[k] != cur_keys[k]),
        "config_changed": sorted(k for k in set(prev_cfg) | set(cur_cfg) if prev_cfg.get(k) != cur_cfg.get(k)),
        "instructions_same": (prev_body.get("instructions") or {}).get("fp") == (cur_body.get("instructions") or {}).get("fp"),
        "tools_same": prev_tools.get("sha256") == cur_tools.get("sha256") if prev_tools or cur_tools else None,
        "input_count": [prev_count, cur_count],
        "items_compared": len(shared) if have_items else None,
        "first_divergent_index": divergent,
        "input_prefix_kept": (divergent if divergent is not None else min(prev_count, cur_count))
        if complete else None,
        "appended_item_types": appended,
        "bytes": [prev_body.get("bytes"), cur_body.get("bytes")],
        "wire_bytes": [prev_body.get("wire_bytes"), cur_body.get("wire_bytes")],
        "chars_changed": {k: [prev_chars.get(k), v] for k, v in cur_chars.items() if prev_chars.get(k) != v},
    }


def _summary(record: dict) -> dict[str, Any]:
    body = record.get("body") or {}
    return {"ts": record.get("ts"), "probe": record.get("probe"),
            "session": (record.get("corr") or {}).get("session_id") or "absent",
            "api_request_id": (record.get("corr") or {}).get("api_request_id"),
            "outcome": record.get("outcome"), "http_status": record.get("http_status"),
            "source": record.get("source"), "bytes": body.get("bytes"),
            "wire_bytes": body.get("wire_bytes"), "encoding": body.get("encoding"),
            "input_count": (body.get("input") or {}).get("count"), "model": (record.get("route") or {}).get("model")}


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Summarize the private consumer-Codex request-shape ring.")
    parser.add_argument("--session", help="only records for this session id")
    parser.add_argument("--incidents", action="store_true",
                        help="read the pinned incidents file and diff each pinned record against its "
                             "same-session accepted predecessor")
    parser.add_argument("--path", type=Path, default=None,
                        help="ring file (default under HERMES_HOME), or the incidents file with --incidents")
    args = parser.parse_args(argv)
    if args.incidents:
        for incident in read_records(args.path or incidents_path()):
            pinned, accepted = incident.get("pinned"), incident.get("accepted")
            if not isinstance(pinned, dict):
                continue
            summary = _summary(pinned)
            if args.session and summary["session"] != args.session:
                continue
            summary["vs_last_accepted"] = diff_records(accepted, pinned) if isinstance(accepted, dict) else None
            print(json.dumps(summary, sort_keys=True))
        return 0
    records = [r for r in read_records(args.path) if not args.session or (r.get("corr") or {}).get("session_id") == args.session]
    last_accepted: dict[str, dict] = {}
    for record in records:
        summary = _summary(record)
        session = summary["session"]
        if record.get("outcome") != "accepted" and session in last_accepted:
            summary["vs_last_accepted"] = diff_records(last_accepted[session], record)
        print(json.dumps(summary, sort_keys=True))
        if record.get("outcome") == "accepted":
            last_accepted[session] = record
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

"""Opt-in, content-free shape records for consumer-Codex requests at the wire boundary.

A generic ``{"detail": "Bad Request"}`` 400 from the ChatGPT-account Codex endpoint names no
field, and the only retained request evidence (``request_dump_*``) is written for the terminal
failure alone, from SDK kwargs rather than transmitted bytes. That leaves nothing to compare a
rejected request against the successful requests just before it.

Off by default. ``agent.codex_request_shape_ring: N`` (N > 0, capped at ``MAX_RING``) makes every
consumer-Codex Responses request append one record, accepted or rejected, to a private ring at
``$HERMES_HOME/logs/codex_request_shape.jsonl`` that keeps only the newest N records. Records are
derived from the bytes the SDK actually serialized (the ``httpx.Request`` content) when the
client exposes them, otherwise from the final kwargs; ``source`` says which.

Retained: key/type signatures, item-type sequences, counts, sizes, allowlisted configuration
scalars, routing metadata, local correlation IDs, plain SHA-256 of tool schemas (canonical JSON,
comparable across installs) and keyed HMAC fingerprints of content. Never retained: headers,
credentials, URLs, message text, tool arguments, tool output, error messages.

Fingerprints are still private: the HMAC key, ring and lock live 0600 beside the other private
logs, and a request is never altered or delayed by a failure in here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import uuid
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SCHEMA = "codex-request-shape.v1"
CONFIG_KEY = "codex_request_shape_ring"  # under ``agent:``
MAX_RING = 200
MAX_RECORD_BYTES = 64 * 1024
MAX_ITEMS = 256
MAX_TOOLS = 128
RING_FILENAME = "codex_request_shape.jsonl"
_KEY_FILENAME = "codex_request_shape.key"

_TOKEN = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")  # local correlation ids (session/api-request), never secrets
_SUSPECT = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\U00010000-\U0010ffff]")
_ROLES = frozenset({"user", "assistant", "system", "developer", "tool"})
_SDK_ONLY_KWARGS = frozenset({"extra_headers", "extra_query", "extra_body", "timeout"})
_SCALAR_CONFIG = ("model", "store", "stream", "parallel_tool_calls", "service_tier", "truncation",
                  "max_output_tokens", "max_tool_calls", "temperature", "top_p", "background")
_LOCK = threading.Lock()


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


# ── content-free derivation ───────────────────────────────────────────────────────────────

def _ident(value: Any) -> str:
    text = str(value or "")
    return "absent" if not text else text if _ID.match(text) else "other"


def _token(value: Any) -> str:
    if value is None:
        return "absent"
    return value if isinstance(value, str) and _TOKEN.match(value) else "other"


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


def _item_record(item: Any, fp: _Fingerprinter) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"t": _json_type(item), "bytes": len(_canonical(item)), "fp": fp(item)}
    kind = item.get("type")
    rec: dict[str, Any] = {
        "t": _token(kind) if kind is not None else ("message" if "role" in item else "absent"),
        "keys": sorted(_token(k) for k in item),
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
        entry = {"type": _token(tool.get("type")), "name": _token(tool.get("name")),
                 "keys": sorted(_token(k) for k in tool)}
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
        cfg["text"] = {"keys": sorted(_token(k) for k in text),
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
                              "items": [_item_record(item, fp) for item in items[:MAX_ITEMS]],
                              "truncated": max(0, len(items) - MAX_ITEMS), **_pairing(items)}
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
        elif detail is not None:
            rec["detail_type"] = _json_type(detail)
        error_obj = body.get("error")
        nested: dict = error_obj if isinstance(error_obj, dict) else body
        # Structured validator fields are identifiers (field paths, codes), kept only as safe tokens.
        for key in ("code", "type", "param"):
            if key in nested:
                rec[f"error_{key}"] = _token(nested[key]) if not isinstance(nested[key], int) else nested[key]
        message = nested.get("message")
        if isinstance(message, str):
            rec["message_chars"] = len(message)
    elif body is not None:
        rec["body_type"] = _json_type(body)
    return rec


# ── bounded private ring ─────────────────────────────────────────────────────────────────

def _private_open(path: Path, flags: int) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)


def _fingerprinter(directory: Path) -> _Fingerprinter:
    key_path = directory / _KEY_FILENAME
    try:
        fd = _private_open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        key = key_path.read_bytes()
        if len(key) >= 32:
            return _Fingerprinter(key)
        raise ValueError("codex request-shape fingerprint key is malformed")
    key = os.urandom(32)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return _Fingerprinter(key)


@contextmanager
def _ring_lock(directory: Path) -> Iterator[None]:
    with _LOCK:
        fd = _private_open(directory / (RING_FILENAME + ".lock"), os.O_RDWR | os.O_CREAT)
        try:
            try:
                import fcntl
            except ImportError:  # Windows: in-process lock only
                fcntl = None
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _encode(record: dict) -> str:
    line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(line) <= MAX_RECORD_BYTES:
        return line
    reduced = json.loads(line)
    for section in ("input", "tools"):
        part = (reduced.get("body") or {}).get(section)
        if isinstance(part, dict) and "items" in part:
            part["items_dropped"] = len(part.pop("items"))
    reduced["reduced"] = "items"
    line = json.dumps(reduced, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(line) <= MAX_RECORD_BYTES:
        return line
    body = reduced.get("body") or {}
    reduced["body"] = {k: body.get(k) for k in ("bytes", "fp", "shape_sha256", "depth", "chars")}
    reduced["reduced"] = "minimal"
    return json.dumps(reduced, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def append_record(path: Path, record_factory, limit: int) -> None:
    """Build (with the private fingerprint key) and append one record, keeping the newest ``limit``."""
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = record_factory(_fingerprinter(directory))
    line = _encode(record)
    with _ring_lock(directory):
        lines: list[str] = []
        with suppress(FileNotFoundError):
            lines = [entry for entry in path.read_text(encoding="utf-8").splitlines() if entry.strip()]
        lines = (lines + [line])[-max(1, limit):]
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        fd = _private_open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            os.replace(tmp, path)
        except BaseException:
            with suppress(FileNotFoundError):
                tmp.unlink()
            raise


def read_records(path: Path | None = None) -> list[dict]:
    path = path or ring_path()
    records = []
    with suppress(FileNotFoundError):
        for line in path.read_text(encoding="utf-8").splitlines():
            with suppress(ValueError):
                records.append(json.loads(line))
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
            body: Any
            source = "wire_bytes"
            if wire is not None:
                try:
                    body = json.loads(wire)
                except ValueError:
                    body, source = None, "wire_bytes_unparsed"
            else:
                body, source = body_from_kwargs(wire_kwargs), "kwargs"
            described = describe_body(body, fp) if body is not None else {}
            described["bytes"] = len(wire) if wire is not None else len(_canonical(body))
            described["fp"] = fp(body) if body is not None else fp.raw(wire or b"")
            return {**self.base, "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                    "outcome": outcome, "source": source, "body": described, **extra}

        try:
            append_record(self.path, build, self.limit)
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
    """What changed between two records (typically last accepted → rejected in one session)."""
    prev_body, cur_body = previous.get("body") or {}, current.get("body") or {}
    prev_input, cur_input = prev_body.get("input") or {}, cur_body.get("input") or {}
    prev_items, cur_items = prev_input.get("items") or [], cur_input.get("items") or []
    common = 0
    for a, b in zip(prev_items, cur_items):
        if a.get("fp") != b.get("fp"):
            break
        common += 1
    prev_keys, cur_keys = prev_body.get("keys") or {}, cur_body.get("keys") or {}
    prev_cfg, cur_cfg = prev_body.get("config") or {}, cur_body.get("config") or {}
    prev_tools, cur_tools = prev_body.get("tools") or {}, cur_body.get("tools") or {}
    return {
        "outcomes": [previous.get("outcome"), current.get("outcome")],
        "same_route": previous.get("route") == current.get("route"),
        "keys_added": sorted(set(cur_keys) - set(prev_keys)),
        "keys_removed": sorted(set(prev_keys) - set(cur_keys)),
        "key_type_changed": sorted(k for k in set(prev_keys) & set(cur_keys) if prev_keys[k] != cur_keys[k]),
        "config_changed": sorted(k for k in set(prev_cfg) | set(cur_cfg) if prev_cfg.get(k) != cur_cfg.get(k)),
        "instructions_same": (prev_body.get("instructions") or {}).get("fp") == (cur_body.get("instructions") or {}).get("fp"),
        "tools_same": prev_tools.get("sha256") == cur_tools.get("sha256"),
        "input_prefix_kept": common,
        "input_count": [prev_input.get("count"), cur_input.get("count")],
        "appended_item_types": [i.get("t") for i in cur_items[common:]],
        "bytes": [prev_body.get("bytes"), cur_body.get("bytes")],
        "chars_changed": {k: [(prev_body.get("chars") or {}).get(k), v]
                          for k, v in (cur_body.get("chars") or {}).items()
                          if (prev_body.get("chars") or {}).get(k) != v},
    }


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Summarize the private consumer-Codex request-shape ring.")
    parser.add_argument("--session", help="only records for this session id")
    parser.add_argument("--path", type=Path, default=None)
    args = parser.parse_args(argv)
    records = [r for r in read_records(args.path) if not args.session or (r.get("corr") or {}).get("session_id") == args.session]
    last_accepted: dict[str, dict] = {}
    for record in records:
        session = (record.get("corr") or {}).get("session_id") or "absent"
        body = record.get("body") or {}
        summary = {"ts": record.get("ts"), "probe": record.get("probe"), "session": session,
                   "api_request_id": (record.get("corr") or {}).get("api_request_id"),
                   "outcome": record.get("outcome"), "http_status": record.get("http_status"),
                   "source": record.get("source"), "bytes": body.get("bytes"),
                   "input_count": (body.get("input") or {}).get("count"), "model": (record.get("route") or {}).get("model")}
        if record.get("outcome") != "accepted" and session in last_accepted:
            summary["vs_last_accepted"] = diff_records(last_accepted[session], record)
        print(json.dumps(summary, sort_keys=True))
        if record.get("outcome") == "accepted":
            last_accepted[session] = record
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

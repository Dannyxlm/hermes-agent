"""Local tier evidence at SDK boundaries, before Relay can rewrite provider output.

Only these records, not the surrounding agent log, are content-free. SDK-internal
HTTP retries are not visible. Each explicit SDK invocation gets a fresh local ID.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)
_TIERS = frozenset({"auto", "default", "flex", "priority", "scale", "standard", "fast", "batch"})
_SPEEDS = frozenset({"fast", "standard"})
_WIRES = frozenset({"responses", "anthropic_messages"})
_STATUSES = frozenset({"completed", "incomplete", "failed", "cancelled", "in_progress", "queued"})
_TERMINAL = {"response.completed": "completed", "response.incomplete": "incomplete", "response.failed": "failed"}


def _enum(value: Any, allowed: frozenset[str]) -> str:
    if value is None:
        return "absent"
    # Never coerce, normalize or truncate untrusted values into apparent evidence.
    return value if type(value) is str and len(value) <= 32 and value in allowed else "unknown"


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _request_field(kwargs: dict, name: str) -> Any:
    extra = kwargs.get("extra_body")
    # SDK extra_body overrides top-level fields, including explicit nulls.
    if isinstance(extra, dict) and name in extra:
        return extra[name]
    return kwargs.get(name)


class _Observation:
    """Retain only bounded enums; never keep the request, events or raw usage."""

    def __init__(self, wire, kwargs):
        self.attempt, self.wire = uuid4().hex, _enum(wire, _WIRES)
        self.tier = self.source = self.status = "absent"
        self.done = False
        logger.info(
            "tier_evidence phase=request attempt=%s wire=%s req_service_tier=%s req_speed=%s",
            self.attempt, self.wire, _enum(_request_field(kwargs, "service_tier"), _TIERS),
            _enum(_request_field(kwargs, "speed"), _SPEEDS),
        )

    def response(self, response):
        tier = _field(response, "service_tier")
        source = "response"
        if tier is None:
            tier, source = _field(_field(response, "usage"), "service_tier"), "usage"
        if tier is not None:
            self.tier, self.source = _enum(tier, _TIERS), source
        self.status = _enum(_field(response, "status"), _STATUSES)

    def event(self, event):
        if self.done:
            return
        kind = _field(event, "type")
        if type(kind) is not str:
            return
        if self.wire == "responses" and kind in _TERMINAL:
            self.response(_field(event, "response"))
            # The terminal event type is itself explicit provider metadata.
            self.status = _TERMINAL[kind]
            self.finish("terminal")
        elif self.wire == "anthropic_messages":
            if kind == "message_start":
                self.response(_field(event, "message"))
            elif kind == "message_delta":
                self.response(event)
            elif kind == "message_stop":
                self.finish("terminal")

    def finish(self, outcome):
        if not self.done:
            self.done = True
            logger.info(
                "tier_evidence phase=response attempt=%s wire=%s outcome=%s "
                "served_tier=%s served_source=%s response_status=%s",
                self.attempt, self.wire, outcome, self.tier, self.source, self.status,
            )


class _ObservedStream:
    """Tap raw events before downstream consumers; preserve SDK attributes/cleanup."""

    def __init__(self, raw, observation):
        self._raw, self._observation = raw, observation

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __setattr__(self, name, value):
        if name in {"_raw", "_observation"}:
            object.__setattr__(self, name, value)
        else:
            # normalize_stream_usage patches the SDK's _raw_stream in place.
            setattr(self._raw, name, value)

    def __iter__(self):
        try:
            for event in self._raw:
                self._observation.event(event)
                yield event
        except GeneratorExit:
            self._observation.finish("closed")
            raise
        except BaseException:
            self._observation.finish("error")
            raise
        else:
            self._observation.finish("exhausted")

    def close(self):
        try:
            close = getattr(self._raw, "close", None)
            if callable(close):
                return close()
        finally:
            self._observation.finish("closed")


class _ObservedManager:
    def __init__(self, manager, observation):
        self._manager, self._observation = manager, observation

    def __enter__(self):
        try:
            return _ObservedStream(self._manager.__enter__(), self._observation)
        except BaseException:
            self._observation.finish("error")
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._manager.__exit__(exc_type, exc, tb)
        finally:
            self._observation.finish("error" if exc_type else "closed")


def observe_tier_call(wire, kwargs, create):
    """Completed SDK call: propagate the identical response or exception."""
    observation = _Observation(wire, kwargs)
    try:
        response = create(**kwargs)
    except BaseException:
        observation.finish("error")
        raise
    observation.response(response)
    observation.finish("returned")
    return response


def observe_tier_stream(wire, kwargs, create):
    """SDK stream factory, not an already-opened stream or a Relay iterator."""
    observation = _Observation(wire, kwargs)
    try:
        stream = create(**kwargs)
    except BaseException:
        observation.finish("error")
        raise
    return _ObservedStream(stream, observation)


def observe_tier_manager(kwargs, create):
    """Anthropic stream manager: observe opening errors as well as raw events."""
    observation = _Observation("anthropic_messages", kwargs)
    try:
        manager = create(**kwargs)
    except BaseException:
        observation.finish("error")
        raise
    return _ObservedManager(manager, observation)

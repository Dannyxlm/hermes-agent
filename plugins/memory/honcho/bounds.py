"""One-shot deadline and result-bound helpers for Honcho tools.

The helpers never retry. Timed-out work runs only in a daemon thread and writes
to an invocation-local queue, so a late result cannot enter a later call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import queue
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class BoundedOutcome:
    status: str
    value: Any = field(default=None, repr=False)
    error_code: str = ""
    elapsed_ms: int = 0


def run_bounded(
    operation: Callable[[], Any],
    *,
    timeout_seconds: float,
) -> BoundedOutcome:
    """Run exactly one operation behind a hard caller-side deadline."""
    started = time.monotonic()
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            item = ("ok", operation())
        except Exception:
            item = ("error", None)
        try:
            result_queue.put_nowait(item)
        except queue.Full:
            pass

    worker = threading.Thread(target=_run, daemon=True, name="honcho-bounded-call")
    worker.start()
    worker.join(timeout=max(0.0, float(timeout_seconds)))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if worker.is_alive():
        return BoundedOutcome(
            status="degraded",
            error_code="deadline_exceeded",
            elapsed_ms=elapsed_ms,
        )
    try:
        status, value = result_queue.get_nowait()
    except queue.Empty:
        return BoundedOutcome(
            status="degraded",
            error_code="backend_error",
            elapsed_ms=elapsed_ms,
        )
    if status != "ok":
        return BoundedOutcome(
            status="degraded",
            error_code="backend_error",
            elapsed_ms=elapsed_ms,
        )
    return BoundedOutcome(status="ok", value=value, elapsed_ms=elapsed_ms)


def cap_text(value: Any, *, max_chars: int) -> str:
    """Return a deterministic word-boundary text prefix."""
    text = str(value or "").strip()
    limit = max(0, int(max_chars))
    if len(text) <= limit:
        return text
    if limit == 0:
        return ""
    clipped = text[:limit].rstrip()
    split = clipped.rfind(" ")
    if split >= int(limit * 0.6):
        clipped = clipped[:split].rstrip()
    return clipped


def cap_string_list(
    values: Iterable[Any],
    *,
    max_items: int,
    max_chars: int,
) -> list[str]:
    """Bound count and aggregate characters without retaining empty values."""
    output: list[str] = []
    remaining = max(0, int(max_chars))
    for raw in values:
        if len(output) >= max(0, int(max_items)) or remaining <= 0:
            break
        text = cap_text(raw, max_chars=remaining)
        if not text:
            continue
        output.append(text)
        remaining -= len(text)
    return output


def cap_records(
    records: Sequence[Mapping[str, Any]],
    *,
    max_items: int,
    max_chars: int,
    allowed_keys: tuple[str, ...],
) -> list[dict[str, str]]:
    """Project records onto allowed string keys under aggregate count/char caps."""
    output: list[dict[str, str]] = []
    remaining = max(0, int(max_chars))
    for record in records:
        if len(output) >= max(0, int(max_items)) or remaining <= 0:
            break
        projected: dict[str, str] = {}
        for key in allowed_keys:
            if remaining <= 0:
                break
            raw = record.get(key)
            if raw is None:
                continue
            value = cap_text(raw, max_chars=remaining)
            if not value:
                continue
            projected[key] = value
            remaining -= len(value)
        if projected:
            output.append(projected)
    return output

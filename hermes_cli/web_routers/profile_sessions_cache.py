"""Versioned wire-response reuse for the two profile session listings."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import functools
import inspect
import threading
import time
from pathlib import Path
from typing import Any, Literal, ParamSpec

from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse, Response

from hermes_cli.session_listing import subagent_listing_scope
from hermes_state_health import storage_state

_MAX_ENTRIES = 32
_MAX_BODY_BYTES = 16 * 1024 * 1024
_P = ParamSpec("_P")
_Arguments = tuple[tuple[str, Any], ...]
_Listing = Literal["sessions", "sidebar"]


@dataclass(frozen=True)
class _FileVersion:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def at(cls, path: Path) -> _FileVersion | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return cls(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@dataclass(frozen=True)
class _ProfileVersion:
    name: str
    home: str
    resolved_home: str
    db: _FileVersion | None
    wal: _FileVersion | None
    storage: str
    subagents: tuple[bool, tuple[str, ...] | None] | None


@dataclass(frozen=True)
class _Dependencies:
    arguments: _Arguments
    profiles: tuple[_ProfileVersion, ...]


@dataclass(frozen=True)
class _WireResponse:
    body: bytes
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    built_at: float
    active_until: float

    def response(self) -> Response:
        response = Response(self.body, status_code=self.status)
        response.raw_headers = list(self.headers)
        return response


class _ResponseBytesCache:
    def __init__(self) -> None:
        self.entries: OrderedDict[_Dependencies, _WireResponse] = OrderedDict()
        self.body_bytes = 0
        self.latest_wall = float("-inf")
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()

    def _clock(self, now: float) -> None:
        if now < self.latest_wall:
            self.entries.clear()
            self.body_bytes = 0
        self.latest_wall = now

    def get(self, dependencies: _Dependencies, now: float) -> Response | None:
        with self.lock:
            self._clock(now)
            entry = self.entries.get(dependencies)
            if entry is None:
                return None
            if now < entry.built_at or now >= entry.active_until:
                self.body_bytes -= len(self.entries.pop(dependencies).body)
                return None
            self.entries.move_to_end(dependencies)
            return entry.response()

    def put(self, dependencies: _Dependencies, entry: _WireResponse, now: float) -> None:
        with self.lock:
            self._clock(now)
            if now < entry.built_at or now >= entry.active_until or len(entry.body) > _MAX_BODY_BYTES:
                return
            previous = self.entries.pop(dependencies, None)
            if previous is not None:
                self.body_bytes -= len(previous.body)
            self.entries[dependencies] = entry
            self.body_bytes += len(entry.body)
            while len(self.entries) > _MAX_ENTRIES or self.body_bytes > _MAX_BODY_BYTES:
                self.body_bytes -= len(self.entries.popitem(last=False)[1].body)


def _dependencies(arguments: Mapping[str, Any], listing: _Listing) -> _Dependencies:
    from hermes_cli.web_routers import profiles

    source: str | None = None
    sources: list[str] | None = None
    if listing == "sidebar":
        targets = profiles._scoped_profile_targets(
            "GET /api/profiles/sessions/sidebar", arguments["recents_profile"])
        excluded = [s for s in (arguments["recents_exclude"] or "").split(",") if s.strip()] or None
    else:
        profile = arguments["profile"]
        targets = ([profiles._cron_profile_home(profile)] if profile and profile != "all"
                   else profiles._profile_targets("GET /api/profiles/sessions"))
        source = arguments["source"] or None
        sources = profiles._csv_list(arguments["sources"]) or None
        excluded = profiles._csv_list(arguments["exclude_sources"]) or None
    versions = []
    for name, home in targets:
        home = Path(home)
        db_path = home / "state.db"
        db = _FileVersion.at(db_path)
        scope = (subagent_listing_scope(home, source=source, sources=sources, exclude_sources=excluded)
                 if db is not None else None)
        versions.append(_ProfileVersion(
            name, str(home), str(home.resolve()), db, _FileVersion.at(Path(f"{db_path}-wal")),
            storage_state(db_path),
            (scope[0], tuple(scope[1]) if scope[1] is not None else None) if scope is not None else None))
    return _Dependencies(tuple(arguments.items()), tuple(versions))


def _active_deadline(result: dict[str, Any], listing: _Listing) -> float:
    groups = ([result.get("sessions", [])] if listing == "sessions" else
              [result.get(key, {}).get("sessions", []) for key in ("recents", "cron", "messaging")])
    return min((row.get("last_active", row.get("started_at", 0)) + 300
                for rows in groups for row in rows if row.get("is_active")), default=float("inf"))


def profile_sessions_response_cache(listing: _Listing) -> Callable[[Callable[_P, Any]], Callable[_P, Any]]:
    """Cache only stable, successful compact responses; never retain mutable rows or DB handles."""
    def decorate(func: Callable[_P, Any]) -> Callable[_P, Any]:
        signature = inspect.signature(func)
        cache = _ResponseBytesCache()

        @functools.wraps(func)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> Any:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = bound.arguments
            if listing == "sessions" and (
                arguments["full"] or arguments["archived"] not in ("exclude", "only", "include")
                or arguments["order"] not in ("created", "recent")
            ):
                return func(*args, **kwargs)
            dependencies = _dependencies(arguments, listing)
            cached = cache.get(dependencies, time.time())
            if cached is not None:
                return cached
            with cache.refresh_lock:
                # A waiter must not reuse the pre-lock roster, policy, or file version.
                dependencies = _dependencies(arguments, listing)
                started = time.time()
                cached = cache.get(dependencies, started)
                if cached is not None:
                    return cached
                result = func(*args, **kwargs)
                if not isinstance(result, dict):
                    return result
                response = JSONResponse(jsonable_encoder(result))
                if result.get("errors") or result.get("storage") or result.get("profiles_failed"):
                    return response
                # Never label a response built across a mutation with the newer version.
                if dependencies == _dependencies(arguments, listing):
                    cache.put(dependencies, _WireResponse(
                        bytes(response.body), response.status_code, tuple(response.raw_headers), started,
                        _active_deadline(result, listing)), time.time())
                return response

        return wrapped

    return decorate

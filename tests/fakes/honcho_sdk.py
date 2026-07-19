"""Hermetic Honcho V3-shaped read transport used by containment tests.

The fake deliberately separates read egress from resource mutation.  Tests can
block all egress around lifecycle calls and can inspect content-free counters
before and after an explicit read.  It never opens a socket.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterator

from plugins.memory.honcho.readonly import (
    HonchoReadAmbiguous,
    HonchoReadDenied,
)
from plugins.memory.honcho.sdk_transport import SURFACE_CONTRACT


@dataclass(frozen=True)
class FakePage:
    """Match the V3 SDK's ``.items`` page shape."""

    items: list[Any]


class FakeHonchoSdk:
    """A no-network, counter-backed implementation of the read transport."""

    sdk_version = "2.2.0"
    max_retries = 0
    read_only = True

    def __init__(
        self,
        *,
        provider_base_url: str = "https://fixture.invalid",
        workspaces: set[str] | None = None,
        peers: set[tuple[str, str]] | None = None,
        sessions: set[tuple[str, str]] | None = None,
    ) -> None:
        self.provider_base_url = provider_base_url
        self.workspaces = set(workspaces or set())
        self.peers = set(peers or set())
        self.sessions = set(sessions or set())
        self.cards: dict[tuple[str, str], list[str]] = {}
        self.search_results: list[Any] = []
        self.contexts: dict[tuple[str, str], Any] = {}
        self.counters: Counter[str] = Counter()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._egress_blocked = False
        self._next_failure: dict[str, BaseException] = {}

    @contextmanager
    def block_egress(self) -> Iterator[None]:
        """Raise if any read-shaped transport operation occurs in this scope."""

        previous = self._egress_blocked
        self._egress_blocked = True
        try:
            yield
        finally:
            self._egress_blocked = previous

    def deny_next(self, operation: str) -> None:
        self._next_failure[operation] = HonchoReadDenied("fixture denial")

    def make_next_ambiguous(self, operation: str) -> None:
        self._next_failure[operation] = HonchoReadAmbiguous("fixture ambiguity")

    def resource_counts(self) -> dict[str, int]:
        """Return content-free resource counts without provider egress."""

        return {
            "workspaces": len(self.workspaces),
            "peers": len(self.peers),
            "sessions": len(self.sessions),
            "conclusions": 0,
            "paid_reasoning_jobs": 0,
        }

    def close(self) -> None:
        self.counters["closes"] += 1

    def _read(self, operation: str, **kwargs: Any) -> None:
        if self._egress_blocked:
            raise AssertionError(f"unexpected Honcho egress: {operation}")
        self.counters["network_reads"] += 1
        self.counters[operation] += 1
        self.calls.append((operation, dict(kwargs)))
        failure = self._next_failure.pop(operation, None)
        if failure is not None:
            raise failure

    def resource_exists(
        self,
        resource: str,
        *,
        workspace_id: str,
        resource_id: str,
        timeout_seconds: float,
    ) -> bool:
        self._read(
            f"exists_{resource}",
            workspace_id=workspace_id,
            resource_id=resource_id,
            timeout_seconds=timeout_seconds,
        )
        if resource == "workspace":
            return workspace_id in self.workspaces
        if resource == "peer":
            return (workspace_id, resource_id) in self.peers
        if resource == "session":
            return (workspace_id, resource_id) in self.sessions
        raise AssertionError(f"unknown resource kind: {resource}")

    def read_profile(
        self,
        *,
        workspace_id: str,
        peer_id: str,
        timeout_seconds: float,
    ) -> list[str]:
        self._read(
            "read_profile",
            workspace_id=workspace_id,
            peer_id=peer_id,
            timeout_seconds=timeout_seconds,
        )
        return list(self.cards.get((workspace_id, peer_id), []))

    def search_messages(
        self,
        *,
        workspace_id: str,
        peer_id: str,
        session_id: str,
        query: str,
        filters: dict[str, Any],
        limit: int,
        timeout_seconds: float,
    ) -> FakePage:
        self._read(
            "search_messages",
            workspace_id=workspace_id,
            peer_id=peer_id,
            session_id=session_id,
            query=query,
            filters=filters,
            limit=limit,
            timeout_seconds=timeout_seconds,
        )
        return FakePage(items=list(self.search_results[:limit]))

    def read_context(
        self,
        *,
        workspace_id: str,
        peer_id: str,
        session_id: str,
        max_conclusions: int,
        timeout_seconds: float,
    ) -> Any:
        self._read(
            "read_context",
            workspace_id=workspace_id,
            peer_id=peer_id,
            session_id=session_id,
            max_conclusions=max_conclusions,
            timeout_seconds=timeout_seconds,
        )
        return self.contexts.get(
            (workspace_id, peer_id),
            SimpleNamespace(representation="", peer_card=[]),
        )

    # Mutation and paid shapes exist only so tests can prove they stay unused.
    def create_workspace(self, *_args: Any, **_kwargs: Any) -> None:
        self.counters["network_creates"] += 1
        raise AssertionError("read containment attempted workspace creation")

    def create_peer(self, *_args: Any, **_kwargs: Any) -> None:
        self.counters["network_creates"] += 1
        raise AssertionError("read containment attempted peer creation")

    def create_session(self, *_args: Any, **_kwargs: Any) -> None:
        self.counters["network_creates"] += 1
        raise AssertionError("read containment attempted session creation")

    def write(self, *_args: Any, **_kwargs: Any) -> None:
        self.counters["writes"] += 1
        raise AssertionError("read containment attempted a write")

    def chat(self, *_args: Any, **_kwargs: Any) -> None:
        self.counters["paid_calls"] += 1
        raise AssertionError("read containment attempted paid reasoning")


class FakeHonchoAi220Surface:
    """Explicit V3 no-create surface used to exercise the concrete wrapper."""

    contract_id = SURFACE_CONTRACT
    sdk_version = "2.2.0"
    max_retries = 0
    read_only = True

    def __init__(self, *, provider_base_url: str = "https://fixture.invalid") -> None:
        self.provider_base_url = provider_base_url
        self.workspaces = {"existing-workspace"}
        self.peers = {("existing-workspace", "existing-user")}
        self.sessions = {("existing-workspace", "existing-session")}
        self.cards = ["bounded fact"]
        self.search_page = FakePage(items=[])
        self.context = SimpleNamespace(representation="bounded context", peer_card=[])
        self.counters: Counter[str] = Counter()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _call(self, name: str, kwargs: dict[str, Any]) -> None:
        self.counters[name] += 1
        self.calls.append((name, dict(kwargs)))

    def workspace_exists(self, **kwargs: Any) -> bool:
        self._call("workspace_exists", kwargs)
        return kwargs["workspace_id"] in self.workspaces

    def peer_exists(self, **kwargs: Any) -> bool:
        self._call("peer_exists", kwargs)
        return (kwargs["workspace_id"], kwargs["resource_id"]) in self.peers

    def session_exists(self, **kwargs: Any) -> bool:
        self._call("session_exists", kwargs)
        return (kwargs["workspace_id"], kwargs["resource_id"]) in self.sessions

    def read_profile(self, **kwargs: Any) -> list[str]:
        self._call("read_profile", kwargs)
        return list(self.cards)

    def search_messages(self, **kwargs: Any) -> FakePage:
        self._call("search_messages", kwargs)
        return self.search_page

    def read_context(self, **kwargs: Any) -> Any:
        self._call("read_context", kwargs)
        return self.context

    def close(self) -> None:
        self.counters["close"] += 1

    def create_workspace(self, **_kwargs: Any) -> None:
        self.counters["create"] += 1
        raise AssertionError("explicit read surface attempted creation")

    def write(self, **_kwargs: Any) -> None:
        self.counters["write"] += 1
        raise AssertionError("explicit read surface attempted a write")

    def reason(self, **_kwargs: Any) -> None:
        self.counters["reason"] += 1
        raise AssertionError("explicit read surface attempted paid reasoning")

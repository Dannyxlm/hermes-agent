"""Audited edge for a non-creating ``honcho-ai==2.2.0`` read surface.

The high-level 2.2.0 ``peer()`` and ``session()`` convenience handles are not
used here: they may ensure or create workspace resources.  This module accepts
only an explicit surface whose contract promises non-creating V3 lookup/read
operations.  The default factory binds the audited 2.2.0 low-level HTTP/list
surface and refuses any version or route-contract drift.

SDK imports and version checks belong here, never in generic memory core.
"""

from __future__ import annotations

from importlib import metadata
import json
import math
import re
from typing import Any, Callable, Mapping, Protocol

from plugins.memory.honcho.readonly import (
    HonchoReadAmbiguous,
    HonchoReadDenied,
    HonchoUnsupportedSdk,
    normalize_provider_base_url,
)


SDK_VERSION = "2.2.0"
HTTPX_VERSION = "0.28.1"
SURFACE_CONTRACT = "honcho-ai-2.2.0-httpx-0.28.1-streaming-v2"
_CONNECT_TIMEOUT_CAP_SECONDS = 0.25
_MAX_JSON_RESPONSE_BYTES = 256 * 1024
_STREAM_CHUNK_BYTES = 8192
_HONCHO_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_SDK_ENVIRONMENT_BASE_URLS = {
    "local": "http://localhost:8000",
    "production": "https://api.honcho.dev",
}


class _BoundedHttpStatus(RuntimeError):
    """Content-free HTTP status used by the typed transport translator."""

    def __init__(self, status_code: int) -> None:
        super().__init__("provider_http_status")
        self.status_code = status_code


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non_finite_json")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class HonchoAi220ReadSurface(Protocol):
    """Explicit non-creating primitives supplied by an audited SDK bridge."""

    contract_id: str
    sdk_version: str
    max_retries: int
    read_only: bool
    provider_base_url: str

    def workspace_exists(self, **kwargs: Any) -> bool: ...

    def peer_exists(self, **kwargs: Any) -> bool: ...

    def session_exists(self, **kwargs: Any) -> bool: ...

    def read_profile(self, **kwargs: Any) -> Any: ...

    def search_messages(self, **kwargs: Any) -> Any: ...

    def read_context(self, **kwargs: Any) -> Any: ...


class HonchoAi220SdkSurface:
    """Direct low-level 2.2.0 V3 reads with no ensure/create helper calls.

    Ava's installed source proves that ``Honcho`` construction is local-only,
    ``max_retries=0`` disables the SDK retry loop, and the frozen path templates
    are the non-creating list/search/card/context endpoints.  The high-level
    ``peer()``, ``session()``, and ``search()`` helpers are deliberately absent.
    """

    contract_id = SURFACE_CONTRACT
    sdk_version = SDK_VERSION
    max_retries = 0
    read_only = True

    def __init__(
        self,
        client: Any,
        *,
        provider_base_url: str,
    ) -> None:
        http = getattr(client, "_http", None)
        workspace_id = getattr(client, "workspace_id", None)
        normalized_bound_url = normalize_provider_base_url(provider_base_url)
        try:
            client_base_url = getattr(client, "base_url", None)
        except Exception as exc:
            raise HonchoUnsupportedSdk("unsupported_sdk_http_surface") from exc
        normalized_client_url = normalize_provider_base_url(client_base_url)
        raw_client = getattr(http, "_client", None)
        normalized_http_url = normalize_provider_base_url(
            getattr(http, "base_url", None)
        )
        api_key = getattr(http, "api_key", None)
        if (
            http is None
            or getattr(http, "max_retries", None) != 0
            or not isinstance(workspace_id, str)
            or _HONCHO_ID.fullmatch(workspace_id) is None
            or not callable(getattr(http, "close", None))
            or raw_client is None
            or not callable(getattr(raw_client, "stream", None))
            or (api_key is not None and not isinstance(api_key, str))
            or normalized_bound_url != provider_base_url
            or normalized_client_url != provider_base_url
            or normalized_http_url != provider_base_url
        ):
            raise HonchoUnsupportedSdk("unsupported_sdk_http_surface")
        self._client = client
        self._http = http
        self._workspace_id = workspace_id
        self._raw_client = raw_client
        self._api_key = api_key
        self.provider_base_url = provider_base_url

    @staticmethod
    def _timeout(connect_timeout_seconds: float, read_timeout_seconds: float) -> Any:
        # Imported lazily with the SDK transport.  An explicit Timeout object
        # preserves separate connect/read ceilings through the SDK's request API.
        import httpx

        return httpx.Timeout(
            timeout=read_timeout_seconds,
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=read_timeout_seconds,
            pool=connect_timeout_seconds,
        )

    def _require_workspace(self, workspace_id: str) -> None:
        if workspace_id != self._workspace_id:
            raise HonchoReadDenied("workspace_scope_denied")

    @staticmethod
    def _require_resource_id(resource_id: Any) -> str:
        if not isinstance(resource_id, str) or _HONCHO_ID.fullmatch(resource_id) is None:
            raise HonchoReadDenied("invalid_resource_scope")
        return resource_id

    def _bounded_json_request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: Mapping[str, Any] | None = None,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
    ) -> Any:
        """Stream one JSON response and reject it before exceeding the cap."""

        if method not in {"GET", "POST"}:
            raise HonchoReadDenied("unsupported_http_method")
        if (
            not isinstance(path, str)
            or not path.startswith("/v3/")
            or "?" in path
            or "#" in path
            or "\\" in path
        ):
            raise HonchoReadDenied("invalid_sdk_route")
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        timeout = self._timeout(connect_timeout_seconds, read_timeout_seconds)
        response_bytes = bytearray()
        with self._raw_client.stream(
            method,
            f"{self.provider_base_url}{path}",
            json=body if body is not None else None,
            params=dict(query) if query else None,
            headers=headers,
            timeout=timeout,
        ) as response:
            status_code = getattr(response, "status_code", None)
            if type(status_code) is not int:
                raise HonchoReadAmbiguous("response_shape_drift")
            if not 200 <= status_code < 300:
                # Never materialize an untrusted error body.
                raise _BoundedHttpStatus(status_code)
            response_headers = getattr(response, "headers", None)
            header_get = getattr(response_headers, "get", None)
            if not callable(header_get):
                raise HonchoReadAmbiguous("response_shape_drift")
            content_type = header_get("content-type", "")
            if not isinstance(content_type, str):
                raise HonchoReadAmbiguous("response_shape_drift")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json" and not media_type.endswith("+json"):
                raise HonchoReadAmbiguous("response_content_type_denied")
            content_encoding = header_get("content-encoding", "identity")
            if (
                not isinstance(content_encoding, str)
                or content_encoding.strip().lower() not in {"", "identity"}
            ):
                # Reject compression before iteration so a small wire body
                # cannot inflate past the application byte ceiling.
                raise HonchoReadAmbiguous("response_encoding_denied")
            content_length = header_get("content-length")
            if content_length is not None:
                if not isinstance(content_length, str) or not content_length.isdecimal():
                    raise HonchoReadAmbiguous("response_size_ambiguous")
                if int(content_length) > _MAX_JSON_RESPONSE_BYTES:
                    raise HonchoReadAmbiguous("response_body_oversized")
            iterator = getattr(response, "iter_raw", None)
            if not callable(iterator):
                raise HonchoReadAmbiguous("response_shape_drift")
            for chunk in iterator(chunk_size=_STREAM_CHUNK_BYTES):
                if not isinstance(chunk, bytes):
                    raise HonchoReadAmbiguous("response_shape_drift")
                if len(response_bytes) + len(chunk) > _MAX_JSON_RESPONSE_BYTES:
                    raise HonchoReadAmbiguous("response_body_oversized")
                response_bytes.extend(chunk)

        if not response_bytes:
            raise HonchoReadAmbiguous("response_shape_drift")
        try:
            return json.loads(
                response_bytes.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise HonchoReadAmbiguous("response_json_invalid") from exc

    @staticmethod
    def _listed_id(response: Any, resource_id: str) -> bool:
        if not isinstance(response, Mapping):
            raise HonchoReadAmbiguous("response_shape_drift")
        items = response.get("items")
        if not isinstance(items, (list, tuple)) or len(items) > 2:
            raise HonchoReadAmbiguous("response_shape_drift")
        found = False
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                raise HonchoReadAmbiguous("response_shape_drift")
            if item["id"] == resource_id:
                found = True
        return found

    def _list_exists(
        self,
        path: str,
        resource_id: str,
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
    ) -> bool:
        response = self._bounded_json_request(
            "POST",
            path,
            body={"filters": {"id": resource_id}},
            query={"page": 1, "size": 2},
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
        )
        return self._listed_id(response, resource_id)

    def workspace_exists(self, **kwargs: Any) -> bool:
        self._require_workspace(kwargs["workspace_id"])
        return self._list_exists(
            "/v3/workspaces/list",
            self._require_resource_id(kwargs["resource_id"]),
            connect_timeout_seconds=kwargs["connect_timeout_seconds"],
            read_timeout_seconds=kwargs["read_timeout_seconds"],
        )

    def peer_exists(self, **kwargs: Any) -> bool:
        self._require_workspace(kwargs["workspace_id"])
        workspace_id = self._require_resource_id(kwargs["workspace_id"])
        return self._list_exists(
            f"/v3/workspaces/{workspace_id}/peers/list",
            self._require_resource_id(kwargs["resource_id"]),
            connect_timeout_seconds=kwargs["connect_timeout_seconds"],
            read_timeout_seconds=kwargs["read_timeout_seconds"],
        )

    def session_exists(self, **kwargs: Any) -> bool:
        self._require_workspace(kwargs["workspace_id"])
        workspace_id = self._require_resource_id(kwargs["workspace_id"])
        return self._list_exists(
            f"/v3/workspaces/{workspace_id}/sessions/list",
            self._require_resource_id(kwargs["resource_id"]),
            connect_timeout_seconds=kwargs["connect_timeout_seconds"],
            read_timeout_seconds=kwargs["read_timeout_seconds"],
        )

    def read_profile(self, **kwargs: Any) -> Any:
        self._require_workspace(kwargs["workspace_id"])
        workspace_id = self._require_resource_id(kwargs["workspace_id"])
        peer_id = self._require_resource_id(kwargs["peer_id"])
        response = self._bounded_json_request(
            "GET",
            f"/v3/workspaces/{workspace_id}/peers/{peer_id}/card",
            connect_timeout_seconds=kwargs["connect_timeout_seconds"],
            read_timeout_seconds=kwargs["read_timeout_seconds"],
        )
        if not isinstance(response, Mapping):
            raise HonchoReadAmbiguous("response_shape_drift")
        card = response.get("peer_card")
        if card is None:
            return []
        if not isinstance(card, (list, tuple)):
            raise HonchoReadAmbiguous("response_shape_drift")
        return card

    def search_messages(self, **kwargs: Any) -> dict[str, tuple[Any, ...]]:
        self._require_workspace(kwargs["workspace_id"])
        workspace_id = self._require_resource_id(kwargs["workspace_id"])
        session_id = self._require_resource_id(kwargs["session_id"])
        response = self._bounded_json_request(
            "POST",
            f"/v3/workspaces/{workspace_id}/sessions/{session_id}/search",
            body={
                "query": kwargs["query"],
                "filters": kwargs["filters"],
                "limit": kwargs["limit"],
            },
            connect_timeout_seconds=kwargs["connect_timeout_seconds"],
            read_timeout_seconds=kwargs["read_timeout_seconds"],
        )
        if not isinstance(response, (list, tuple)) or len(response) > kwargs["limit"]:
            raise HonchoReadAmbiguous("response_shape_drift")
        if any(not isinstance(item, Mapping) for item in response):
            raise HonchoReadAmbiguous("response_shape_drift")
        return {"items": tuple(response)}

    def read_context(self, **kwargs: Any) -> dict[str, Any]:
        self._require_workspace(kwargs["workspace_id"])
        workspace_id = self._require_resource_id(kwargs["workspace_id"])
        session_id = self._require_resource_id(kwargs["session_id"])
        peer_id = self._require_resource_id(kwargs["peer_id"])
        # The audited V3 handler is retrieval-only on this exact query shape:
        # it reads already-materialized representation/card/session rows.  Its
        # only inline model-provider path is conditional on ``search_query``;
        # this transport deliberately has no way to send that parameter.
        context_query = {
            "summary": False,
            "peer_target": peer_id,
            "limit_to_session": True,
            "max_conclusions": kwargs["max_conclusions"],
        }
        response = self._bounded_json_request(
            "GET",
            f"/v3/workspaces/{workspace_id}/sessions/{session_id}/context",
            query=context_query,
            connect_timeout_seconds=kwargs["connect_timeout_seconds"],
            read_timeout_seconds=kwargs["read_timeout_seconds"],
        )
        if not isinstance(response, Mapping) or response.get("id") != kwargs["session_id"]:
            raise HonchoReadAmbiguous("response_shape_drift")
        representation = response.get("peer_representation")
        card = response.get("peer_card")
        if representation is None:
            representation = ""
        if card is None:
            card = []
        if not isinstance(representation, str) or not isinstance(card, (list, tuple)):
            raise HonchoReadAmbiguous("response_shape_drift")
        return {"representation": representation, "peer_card": card}

    def close(self) -> None:
        self._http.close()


def _status_code(exc: BaseException) -> int | None:
    fields = getattr(exc, "__dict__", {})
    value = None
    if isinstance(fields, Mapping):
        value = fields.get("status_code", fields.get("status"))
    if type(value) is int:
        return value
    response = fields.get("response") if isinstance(fields, Mapping) else None
    response_fields = getattr(response, "__dict__", {})
    nested = response_fields.get("status_code") if isinstance(response_fields, Mapping) else None
    return nested if type(nested) is int else None


class HonchoAi220ReadTransport:
    """Strict transport over a pre-audited, explicitly non-creating surface."""

    sdk_version = SDK_VERSION
    max_retries = 0
    read_only = True

    def __init__(self, surface: HonchoAi220ReadSurface) -> None:
        if not self._surface_supported(surface):
            raise HonchoUnsupportedSdk("unsupported_sdk_surface")
        self._surface = surface
        self.provider_base_url = surface.provider_base_url
        self._closed = False

    @staticmethod
    def _surface_supported(surface: Any) -> bool:
        return bool(
            getattr(surface, "contract_id", None) == SURFACE_CONTRACT
            and getattr(surface, "sdk_version", None) == SDK_VERSION
            and getattr(surface, "max_retries", None) == 0
            and getattr(surface, "read_only", None) is True
            and normalize_provider_base_url(
                getattr(surface, "provider_base_url", None)
            )
            == getattr(surface, "provider_base_url", None)
            and all(
                callable(getattr(surface, name, None))
                for name in (
                    "workspace_exists",
                    "peer_exists",
                    "session_exists",
                    "read_profile",
                    "search_messages",
                    "read_context",
                )
            )
        )

    def _timeouts(self, timeout_seconds: float) -> dict[str, float]:
        if self._closed:
            raise HonchoReadDenied("transport_closed")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
            raise HonchoReadDenied("invalid_timeout")
        remaining = float(timeout_seconds)
        if remaining <= 0:
            raise HonchoReadAmbiguous("deadline_exceeded")
        return {
            "connect_timeout_seconds": min(remaining, _CONNECT_TIMEOUT_CAP_SECONDS),
            "read_timeout_seconds": remaining,
        }

    @staticmethod
    def _translate(operation: Callable[[], Any], *, missing_is_false: bool = False) -> Any:
        try:
            return operation()
        except (HonchoReadDenied, HonchoReadAmbiguous, HonchoUnsupportedSdk):
            raise
        except TimeoutError as exc:
            raise HonchoReadAmbiguous("provider_timeout") from exc
        except Exception as exc:
            status = _status_code(exc)
            if status == 404 and missing_is_false:
                return False
            if status in {401, 403, 429}:
                raise HonchoReadDenied("provider_denied") from exc
            # Unknown provider outcomes must never trigger a fallback or retry.
            raise HonchoReadAmbiguous("provider_outcome_unknown") from exc

    def resource_exists(
        self,
        resource: str,
        *,
        workspace_id: str,
        resource_id: str,
        timeout_seconds: float,
    ) -> bool:
        method_name = {
            "workspace": "workspace_exists",
            "peer": "peer_exists",
            "session": "session_exists",
        }.get(resource)
        if method_name is None:
            raise HonchoReadDenied("unsupported_resource_kind")
        method = getattr(self._surface, method_name)
        kwargs = {
            "workspace_id": workspace_id,
            "resource_id": resource_id,
            **self._timeouts(timeout_seconds),
        }
        result = self._translate(lambda: method(**kwargs), missing_is_false=True)
        if type(result) is not bool:
            raise HonchoReadAmbiguous("response_shape_drift")
        return result

    def read_profile(
        self,
        *,
        workspace_id: str,
        peer_id: str,
        timeout_seconds: float,
    ) -> Any:
        kwargs = {
            "workspace_id": workspace_id,
            "peer_id": peer_id,
            **self._timeouts(timeout_seconds),
        }
        return self._translate(lambda: self._surface.read_profile(**kwargs))

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
    ) -> Any:
        kwargs = {
            "workspace_id": workspace_id,
            "peer_id": peer_id,
            "session_id": session_id,
            "query": query,
            "filters": filters,
            "limit": limit,
            **self._timeouts(timeout_seconds),
        }
        return self._translate(lambda: self._surface.search_messages(**kwargs))

    def read_context(
        self,
        *,
        workspace_id: str,
        peer_id: str,
        session_id: str,
        max_conclusions: int,
        timeout_seconds: float,
    ) -> Any:
        kwargs = {
            "workspace_id": workspace_id,
            "peer_id": peer_id,
            "session_id": session_id,
            "max_conclusions": max_conclusions,
            **self._timeouts(timeout_seconds),
        }
        return self._translate(lambda: self._surface.read_context(**kwargs))

    def close(self) -> None:
        self._closed = True
        close = getattr(self._surface, "close", None)
        if callable(close):
            close()


def build_honcho_ai_220_read_transport(
    *,
    workspace_id: str,
    deadline_seconds: float,
    provider_base_url: str,
    provider_environment: str,
    provider_host: str,
) -> HonchoAi220ReadTransport:
    """Build the audited low-level V3 transport without provider I/O."""

    if not isinstance(workspace_id, str) or _HONCHO_ID.fullmatch(workspace_id) is None:
        raise HonchoReadDenied("invalid_workspace_scope")
    if not isinstance(provider_host, str) or _HONCHO_ID.fullmatch(provider_host) is None:
        raise HonchoReadDenied("invalid_provider_host")
    if provider_environment not in _SDK_ENVIRONMENT_BASE_URLS:
        raise HonchoReadDenied("invalid_provider_environment")
    normalized_bound_url = normalize_provider_base_url(provider_base_url)
    if normalized_bound_url is None or normalized_bound_url != provider_base_url:
        raise HonchoReadDenied("invalid_provider_origin")
    if (
        not isinstance(deadline_seconds, (int, float))
        or isinstance(deadline_seconds, bool)
        or not math.isfinite(float(deadline_seconds))
        or not 0.05 <= float(deadline_seconds) <= 5.0
    ):
        raise HonchoReadDenied("invalid_deadline")

    try:
        installed = metadata.version("honcho-ai")
    except metadata.PackageNotFoundError as exc:
        raise HonchoUnsupportedSdk("honcho_ai_2_2_0_not_installed") from exc
    if installed != SDK_VERSION:
        raise HonchoUnsupportedSdk("honcho_ai_sdk_version_mismatch")
    try:
        installed_httpx = metadata.version("httpx")
    except metadata.PackageNotFoundError as exc:
        raise HonchoUnsupportedSdk("httpx_0_28_1_not_installed") from exc
    if installed_httpx != HTTPX_VERSION:
        raise HonchoUnsupportedSdk("httpx_version_mismatch")

    from plugins.memory.honcho.client import HonchoClientConfig, _is_local_base_url

    config = HonchoClientConfig.from_global_config(host=provider_host)
    if not config.enabled or not (config.api_key or config.base_url):
        raise HonchoReadDenied("provider_not_configured")
    if (
        config.host != provider_host
        or config.workspace_id != workspace_id
        or config.environment != provider_environment
    ):
        raise HonchoReadDenied("provider_config_binding_mismatch")
    ambient_base_url = config.base_url or _SDK_ENVIRONMENT_BASE_URLS[provider_environment]
    normalized_ambient_url = normalize_provider_base_url(ambient_base_url)
    if normalized_ambient_url is None:
        raise HonchoReadDenied("invalid_ambient_provider_origin")
    if normalized_ambient_url != provider_base_url:
        raise HonchoReadDenied("provider_origin_mismatch")

    # All SDK imports remain plugin-local and lazy, after endpoint binding has
    # succeeded. Constructing Honcho 2.2.0 only creates an httpx client; it does
    # not call _ensure_workspace or egress.
    from honcho import Honcho

    effective_api_key = config.api_key
    if _is_local_base_url(provider_base_url):
        raw = config.raw if isinstance(config.raw, Mapping) else {}
        hosts = raw.get("hosts") if isinstance(raw.get("hosts"), Mapping) else {}
        host_block = hosts.get(config.host) if isinstance(hosts, Mapping) else {}
        if not isinstance(host_block, Mapping) or not host_block.get("apiKey"):
            effective_api_key = "local"

    kwargs: dict[str, Any] = {
        "workspace_id": workspace_id,
        "api_key": effective_api_key,
        "environment": config.environment,
        "timeout": float(deadline_seconds),
        "max_retries": 0,
        # Always explicit: this prevents HONCHO_URL or SDK environment fallback
        # from changing the signed endpoint after the ambient comparison.
        "base_url": provider_base_url,
    }
    client = Honcho(**kwargs)
    surface = HonchoAi220SdkSurface(
        client,
        provider_base_url=provider_base_url,
    )
    return HonchoAi220ReadTransport(surface)

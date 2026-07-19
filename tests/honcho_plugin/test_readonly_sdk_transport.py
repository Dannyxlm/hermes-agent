from __future__ import annotations

import json as json_module
import sys
from collections import Counter
from types import ModuleType
from types import SimpleNamespace

import pytest

from plugins.memory.honcho.readonly import (
    HonchoReadAmbiguous,
    HonchoReadDenied,
    HonchoUnsupportedSdk,
    normalize_provider_base_url,
)
from plugins.memory.honcho.sdk_transport import (
    HonchoAi220ReadTransport,
    HonchoAi220SdkSurface,
    _MAX_JSON_RESPONSE_BYTES,
    build_honcho_ai_220_read_transport,
)
from tests.fakes.honcho_sdk import FakeHonchoAi220Surface


def _factory_args(**overrides):
    values = {
        "workspace_id": "existing-workspace",
        "deadline_seconds": 0.5,
        "provider_base_url": "https://fixture.invalid",
        "provider_environment": "production",
        "provider_host": "hermes",
    }
    values.update(overrides)
    return values


def _audited_version(package):
    return {"honcho-ai": "2.2.0", "httpx": "0.28.1"}[package]


def _assert_no_mutation(surface: FakeHonchoAi220Surface) -> None:
    assert surface.counters["create"] == 0
    assert surface.counters["write"] == 0
    assert surface.counters["reason"] == 0


@pytest.mark.parametrize(
    ("workspace_id", "deadline_seconds", "error"),
    [
        ("", 1.0, "invalid_workspace_scope"),
        ("workspace/escape", 1.0, "invalid_workspace_scope"),
        ("workspace", True, "invalid_deadline"),
        ("workspace", float("nan"), "invalid_deadline"),
        ("workspace", 5.1, "invalid_deadline"),
    ],
)
def test_default_factory_rejects_invalid_scope_and_deadline_before_sdk_load(
    workspace_id, deadline_seconds, error
):
    with pytest.raises(HonchoReadDenied, match=error):
        build_honcho_ai_220_read_transport(
            **_factory_args(
                workspace_id=workspace_id,
                deadline_seconds=deadline_seconds,
            )
        )


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("HTTPS://Example.COM:443/v3/", "https://example.com"),
        ("http://127.0.0.1:8000/v3", "http://127.0.0.1:8000"),
        ("http://[::1]:8000/v3/", "http://[::1]:8000"),
        ("https://example.com/honcho/v3", "https://example.com/honcho"),
    ],
)
def test_provider_base_url_normalization(raw, normalized):
    assert normalize_provider_base_url(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "api.honcho.dev",
        "file:///tmp/honcho",
        "ftp://api.honcho.dev",
        "https://user:secret@api.honcho.dev",
        "https://api.honcho.dev?",
        "https://api.honcho.dev?workspace=other",
        "https://api.honcho.dev#",
        "https://api.honcho.dev/#other",
        "https://api.honcho.dev/../other",
        "https://api.honcho.dev/%2e%2e/other",
    ],
)
def test_provider_base_url_rejects_ambiguous_or_unsafe_shapes(raw):
    assert normalize_provider_base_url(raw) is None


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"provider_base_url": "https://user@api.honcho.dev"}, "invalid_provider_origin"),
        ({"provider_base_url": "https://api.honcho.dev?other=1"}, "invalid_provider_origin"),
        ({"provider_base_url": "file:///tmp/honcho"}, "invalid_provider_origin"),
        ({"provider_environment": "staging"}, "invalid_provider_environment"),
        ({"provider_host": "hermes/other"}, "invalid_provider_host"),
    ],
)
def test_default_factory_rejects_unbound_provider_coordinates_before_sdk_load(
    overrides, error
):
    with pytest.raises(HonchoReadDenied, match=error):
        build_honcho_ai_220_read_transport(**_factory_args(**overrides))


@pytest.mark.parametrize(
    ("bound_url", "ambient_url", "environment", "error"),
    [
        (
            "https://api.honcho.dev",
            "http://127.0.0.1:8000",
            "production",
            "provider_origin_mismatch",
        ),
        (
            "http://127.0.0.1:8000",
            "https://api.honcho.dev",
            "local",
            "provider_origin_mismatch",
        ),
        (
            "https://api.honcho.dev",
            "https://api.honcho.dev?other=1",
            "production",
            "invalid_ambient_provider_origin",
        ),
    ],
)
def test_default_factory_denies_ambient_origin_substitution_without_sdk_construction(
    monkeypatch, bound_url, ambient_url, environment, error
):
    from plugins.memory.honcho import sdk_transport
    from plugins.memory.honcho.client import HonchoClientConfig

    monkeypatch.setattr(sdk_transport.metadata, "version", _audited_version)
    monkeypatch.setattr(
        HonchoClientConfig,
        "from_global_config",
        classmethod(
            lambda _cls, host=None: SimpleNamespace(
                enabled=True,
                api_key="fixture-key",
                base_url=ambient_url,
                environment=environment,
                raw={},
                host=host,
                workspace_id="existing-workspace",
            )
        ),
    )
    constructors = []

    class MustNotConstruct:
        def __init__(self, **kwargs):
            constructors.append(kwargs)

    honcho_module = ModuleType("honcho")
    honcho_module.Honcho = MustNotConstruct
    monkeypatch.setitem(sys.modules, "honcho", honcho_module)

    with pytest.raises(HonchoReadDenied, match=error):
        build_honcho_ai_220_read_transport(
            **_factory_args(
                provider_base_url=bound_url,
                provider_environment=environment,
            )
        )

    assert constructors == []


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("host", "other-host", "provider_config_binding_mismatch"),
        ("workspace_id", "other-workspace", "provider_config_binding_mismatch"),
        ("environment", "local", "provider_config_binding_mismatch"),
        ("enabled", False, "provider_not_configured"),
    ],
)
def test_default_factory_denies_ambient_config_binding_drift_before_sdk_load(
    monkeypatch, field, value, error
):
    from plugins.memory.honcho import sdk_transport
    from plugins.memory.honcho.client import HonchoClientConfig

    monkeypatch.setattr(sdk_transport.metadata, "version", _audited_version)
    ambient = SimpleNamespace(
        enabled=True,
        api_key="fixture-key",
        base_url="https://fixture.invalid",
        environment="production",
        raw={},
        host="hermes",
        workspace_id="existing-workspace",
    )
    setattr(ambient, field, value)
    monkeypatch.setattr(
        HonchoClientConfig,
        "from_global_config",
        classmethod(lambda _cls, host=None: ambient),
    )

    with pytest.raises(HonchoReadDenied, match=error):
        build_honcho_ai_220_read_transport(**_factory_args())


def test_surface_transport_uses_only_explicit_read_primitives_and_timeouts():
    surface = FakeHonchoAi220Surface()
    transport = HonchoAi220ReadTransport(surface)

    assert transport.resource_exists(
        "workspace",
        workspace_id="existing-workspace",
        resource_id="existing-workspace",
        timeout_seconds=1.0,
    ) is True
    assert transport.resource_exists(
        "peer",
        workspace_id="existing-workspace",
        resource_id="existing-user",
        timeout_seconds=0.5,
    ) is True
    assert transport.resource_exists(
        "session",
        workspace_id="existing-workspace",
        resource_id="existing-session",
        timeout_seconds=0.4,
    ) is True
    assert transport.read_profile(
        workspace_id="existing-workspace",
        peer_id="existing-user",
        timeout_seconds=0.3,
    ) == ["bounded fact"]
    transport.search_messages(
        workspace_id="existing-workspace",
        peer_id="existing-user",
        session_id="existing-session",
        query="fact",
        filters={"AND": [{"session_id": "existing-session"}]},
        limit=8,
        timeout_seconds=0.2,
    )
    transport.read_context(
        workspace_id="existing-workspace",
        peer_id="existing-user",
        session_id="existing-session",
        max_conclusions=8,
        timeout_seconds=0.1,
    )

    assert {name for name, _ in surface.calls} == {
        "workspace_exists",
        "peer_exists",
        "session_exists",
        "read_profile",
        "search_messages",
        "read_context",
    }
    for _name, kwargs in surface.calls:
        assert kwargs["connect_timeout_seconds"] <= 0.25
        assert 0 < kwargs["read_timeout_seconds"] <= 1.0
    search = next(kwargs for name, kwargs in surface.calls if name == "search_messages")
    assert search["session_id"] == "existing-session"
    _assert_no_mutation(surface)


def test_surface_contract_rejects_wrong_version_retry_or_unverified_bridge():
    for field, value in (
        ("contract_id", "unverified"),
        ("sdk_version", "2.1.0"),
        ("max_retries", 1),
        ("read_only", False),
    ):
        surface = FakeHonchoAi220Surface()
        setattr(surface, field, value)
        with pytest.raises(HonchoUnsupportedSdk, match="unsupported_sdk_surface"):
            HonchoAi220ReadTransport(surface)
        _assert_no_mutation(surface)


def test_surface_404_denial_timeout_and_unknown_outcomes_are_typed(monkeypatch):
    class SurfaceError(RuntimeError):
        def __init__(self, status_code):
            super().__init__("private provider detail")
            self.status_code = status_code

    cases = [
        (SurfaceError(404), False),
        (SurfaceError(403), HonchoReadDenied),
        (TimeoutError("late"), HonchoReadAmbiguous),
        (RuntimeError("unknown"), HonchoReadAmbiguous),
    ]
    for error, expected in cases:
        surface = FakeHonchoAi220Surface()

        def fail(**_kwargs):
            raise error

        monkeypatch.setattr(surface, "workspace_exists", fail)
        transport = HonchoAi220ReadTransport(surface)
        if expected is False:
            assert transport.resource_exists(
                "workspace",
                workspace_id="existing-workspace",
                resource_id="existing-workspace",
                timeout_seconds=1.0,
            ) is False
        else:
            with pytest.raises(expected):
                transport.resource_exists(
                    "workspace",
                    workspace_id="existing-workspace",
                    resource_id="existing-workspace",
                    timeout_seconds=1.0,
                )
        _assert_no_mutation(surface)


def test_transport_close_is_local_and_disables_successor_reads():
    surface = FakeHonchoAi220Surface()
    transport = HonchoAi220ReadTransport(surface)

    transport.close()

    assert surface.counters["close"] == 1
    with pytest.raises(HonchoReadDenied, match="transport_closed"):
        transport.read_profile(
            workspace_id="existing-workspace",
            peer_id="existing-user",
            timeout_seconds=1.0,
        )
    _assert_no_mutation(surface)


class _FakeLowLevelHttp:
    max_retries = 0

    def __init__(self, *, base_url="https://fixture.invalid", api_key="fixture-key"):
        self.base_url = base_url
        self.api_key = api_key
        self.calls = []
        self.counters = Counter()
        self._client = _FakeRawClient(self)
        self.next_response = None

    def _response_for(self, path, body):
        if self.next_response is not None:
            response = self.next_response
            self.next_response = None
            return response
        if path.endswith("/search"):
            payload = [{
                "id": "message-1",
                "content": "bounded fact",
                "peer_id": "existing-user",
                "session_id": "existing-session",
                "metadata": {"human_authored": True, "eligible": True},
            }]
        elif path.endswith("/list"):
            resource_id = body["filters"]["id"]
            payload = {"items": [{"id": resource_id}], "page": 1, "pages": 1}
        elif path.endswith("/card"):
            payload = {"peer_card": ["bounded fact"]}
        else:
            payload = {
                "id": "existing-session",
                "peer_representation": "bounded context",
                "peer_card": ["bounded card"],
            }
        return _FakeStreamResponse.from_json(payload)

    def get(self, *_args, **_kwargs):
        self.counters["sdk_get"] += 1
        raise AssertionError("unbounded SDK GET was used")

    def post(self, *_args, **_kwargs):
        self.counters["sdk_post"] += 1
        raise AssertionError("unbounded SDK POST was used")

    def close(self):
        self.counters["close"] += 1

    def put(self, *_args, **_kwargs):
        self.counters["write"] += 1
        raise AssertionError("read surface attempted PUT")

    def delete(self, *_args, **_kwargs):
        self.counters["delete"] += 1
        raise AssertionError("read surface attempted DELETE")


class _FakeStreamResponse:
    def __init__(self, chunks, *, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self._chunks = list(chunks)
        self.iterations = 0
        self.closed = False

    @classmethod
    def from_json(cls, value):
        payload = json_module.dumps(value, separators=(",", ":")).encode("utf-8")
        return cls(
            [payload],
            headers={
                "content-type": "application/json",
                "content-length": str(len(payload)),
            },
        )

    def iter_raw(self, *, chunk_size):
        assert chunk_size == 8192
        self.iterations += 1
        for chunk in self._chunks:
            yield chunk


class _FakeStreamContext:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *_args):
        self.response.closed = True


class _FakeRawClient:
    def __init__(self, owner):
        self.owner = owner

    def stream(self, method, url, *, json, params, headers, timeout):
        assert url.startswith(self.owner.base_url)
        path = url[len(self.owner.base_url):]
        kwargs = {
            "body": json,
            "query": params,
            "headers": headers,
            "timeout": timeout,
        }
        self.owner.calls.append((method, path, kwargs))
        self.owner.counters["stream"] += 1
        return _FakeStreamContext(self.owner._response_for(path, json))


def test_live_audited_low_level_surface_uses_only_v3_list_search_and_get_routes():
    http = _FakeLowLevelHttp()
    client = SimpleNamespace(
        workspace_id="existing-workspace",
        base_url="https://fixture.invalid",
        _http=http,
    )
    surface = HonchoAi220SdkSurface(
        client,
        provider_base_url="https://fixture.invalid",
    )
    transport = HonchoAi220ReadTransport(surface)

    for resource, resource_id in (
        ("workspace", "existing-workspace"),
        ("peer", "existing-user"),
        ("session", "existing-session"),
    ):
        assert transport.resource_exists(
            resource,
            workspace_id="existing-workspace",
            resource_id=resource_id,
            timeout_seconds=1.0,
        ) is True
    assert transport.read_profile(
        workspace_id="existing-workspace",
        peer_id="existing-user",
        timeout_seconds=1.0,
    ) == ["bounded fact"]
    page = transport.search_messages(
        workspace_id="existing-workspace",
        peer_id="existing-user",
        session_id="existing-session",
        query="fact",
        filters={"AND": [{"session_id": "existing-session"}]},
        limit=8,
        timeout_seconds=1.0,
    )
    assert page["items"][0]["session_id"] == "existing-session"
    context = transport.read_context(
        workspace_id="existing-workspace",
        peer_id="existing-user",
        session_id="existing-session",
        max_conclusions=8,
        timeout_seconds=1.0,
    )
    assert context == {
        "representation": "bounded context",
        "peer_card": ["bounded card"],
    }

    assert [method for method, _path, _kwargs in http.calls] == [
        "POST", "POST", "POST", "GET", "POST", "GET"
    ]
    assert all("/v3/" in path for _method, path, _kwargs in http.calls)
    assert all("/workspaces" != path for _method, path, _kwargs in http.calls)
    assert http.counters["write"] == 0
    assert http.counters["delete"] == 0
    assert http.counters["sdk_get"] == 0
    assert http.counters["sdk_post"] == 0
    assert http.counters["stream"] == 6
    assert not hasattr(http, "chat")
    for _method, _path, kwargs in http.calls:
        timeout = kwargs["timeout"]
        assert timeout.connect <= 0.25
        assert timeout.read <= 1.0
        assert kwargs["headers"]["Accept-Encoding"] == "identity"
    context_call = next(call for call in http.calls if call[1].endswith("/context"))
    assert context_call[2]["query"] == {
        "summary": False,
        "peer_target": "existing-user",
        "limit_to_session": True,
        "max_conclusions": 8,
    }
    assert "search_query" not in context_call[2]["query"]


def test_exact_route_builder_rejects_resource_path_injection_before_stream():
    http = _FakeLowLevelHttp()
    transport = _streaming_transport(http)

    with pytest.raises(HonchoReadDenied, match="invalid_resource_scope"):
        transport.read_profile(
            workspace_id="existing-workspace",
            peer_id="../other",
            timeout_seconds=1.0,
        )
    assert http.calls == []


def _streaming_transport(http):
    client = SimpleNamespace(
        workspace_id="existing-workspace",
        base_url=http.base_url,
        _http=http,
    )
    surface = HonchoAi220SdkSurface(
        client,
        provider_base_url=http.base_url,
    )
    return HonchoAi220ReadTransport(surface)


def test_chunked_json_response_is_streamed_and_bounded_before_decode():
    http = _FakeLowLevelHttp()
    payload = b'{"peer_card":["bounded fact"]}'
    response = _FakeStreamResponse(
        [payload[:7], payload[7:19], payload[19:]],
        headers={"content-type": "application/json"},
    )
    http.next_response = response

    assert _streaming_transport(http).read_profile(
        workspace_id="existing-workspace",
        peer_id="existing-user",
        timeout_seconds=1.0,
    ) == ["bounded fact"]
    assert response.iterations == 1
    assert response.closed is True
    assert http.counters["sdk_get"] == 0


def test_oversized_content_length_is_rejected_before_body_iteration():
    http = _FakeLowLevelHttp()
    response = _FakeStreamResponse(
        [b"{}"],
        headers={
            "content-type": "application/json",
            "content-length": str(_MAX_JSON_RESPONSE_BYTES + 1),
        },
    )
    http.next_response = response

    with pytest.raises(HonchoReadAmbiguous, match="response_body_oversized"):
        _streaming_transport(http).read_profile(
            workspace_id="existing-workspace",
            peer_id="existing-user",
            timeout_seconds=1.0,
        )
    assert response.iterations == 0
    assert response.closed is True
    assert http.counters["sdk_get"] == 0


def test_compressed_response_is_rejected_before_body_iteration():
    http = _FakeLowLevelHttp()
    response = _FakeStreamResponse(
        [b"compressed"],
        headers={
            "content-type": "application/json",
            "content-encoding": "gzip",
            "content-length": "10",
        },
    )
    http.next_response = response

    with pytest.raises(HonchoReadAmbiguous, match="response_encoding_denied"):
        _streaming_transport(http).read_profile(
            workspace_id="existing-workspace",
            peer_id="existing-user",
            timeout_seconds=1.0,
        )
    assert response.iterations == 0
    assert response.closed is True


def test_error_response_body_is_never_materialized():
    http = _FakeLowLevelHttp()
    response = _FakeStreamResponse(
        [b"private provider error"],
        status_code=403,
        headers={"content-type": "text/plain"},
    )
    http.next_response = response

    with pytest.raises(HonchoReadDenied, match="provider_denied"):
        _streaming_transport(http).read_profile(
            workspace_id="existing-workspace",
            peer_id="existing-user",
            timeout_seconds=1.0,
        )
    assert response.iterations == 0
    assert response.closed is True


def test_oversized_chunked_body_is_rejected_at_cumulative_byte_cap():
    http = _FakeLowLevelHttp()
    response = _FakeStreamResponse(
        [b'{"peer_card":["', b"x" * _MAX_JSON_RESPONSE_BYTES],
        headers={"content-type": "application/json"},
    )
    http.next_response = response

    with pytest.raises(HonchoReadAmbiguous, match="response_body_oversized"):
        _streaming_transport(http).read_profile(
            workspace_id="existing-workspace",
            peer_id="existing-user",
            timeout_seconds=1.0,
        )
    assert response.iterations == 1
    assert response.closed is True
    assert http.counters["sdk_get"] == 0


def test_duplicate_json_keys_are_rejected_as_ambiguous():
    http = _FakeLowLevelHttp()
    payload = b'{"peer_card":[],"peer_card":[]}'
    response = _FakeStreamResponse(
        [payload],
        headers={
            "content-type": "application/json",
            "content-length": str(len(payload)),
        },
    )
    http.next_response = response

    with pytest.raises(HonchoReadAmbiguous, match="response_json_invalid"):
        _streaming_transport(http).read_profile(
            workspace_id="existing-workspace",
            peer_id="existing-user",
            timeout_seconds=1.0,
        )
    assert response.closed is True


def test_default_factory_builds_zero_retry_sdk_client_without_egress(monkeypatch):
    from plugins.memory.honcho import sdk_transport
    from plugins.memory.honcho.client import HonchoClientConfig

    monkeypatch.setattr(
        sdk_transport.metadata,
        "version",
        _audited_version,
    )
    http = _FakeLowLevelHttp()
    constructor_kwargs = []

    class FakeHoncho:
        def __init__(self, **kwargs):
            constructor_kwargs.append(kwargs)
            self.workspace_id = kwargs["workspace_id"]
            self.base_url = kwargs["base_url"]
            http.base_url = kwargs["base_url"]
            http.api_key = kwargs["api_key"]
            self._http = http

    honcho_module = ModuleType("honcho")
    honcho_module.Honcho = FakeHoncho
    monkeypatch.setitem(sys.modules, "honcho", honcho_module)
    monkeypatch.setattr(
        HonchoClientConfig,
        "from_global_config",
        classmethod(lambda _cls, host=None: SimpleNamespace(
            enabled=True,
            api_key="fixture-key",
            base_url="HTTPS://Fixture.Invalid:443/v3/",
            environment="production",
            raw={},
            host=host,
            workspace_id="existing-workspace",
        )),
    )

    transport = build_honcho_ai_220_read_transport(
        **_factory_args()
    )

    assert isinstance(transport, HonchoAi220ReadTransport)
    assert constructor_kwargs == [{
        "workspace_id": "existing-workspace",
        "api_key": "fixture-key",
        "environment": "production",
        "timeout": 0.5,
        "max_retries": 0,
        "base_url": "https://fixture.invalid",
    }]
    assert http.calls == []


def test_surface_rejects_non_boolean_existence_shape(monkeypatch):
    surface = FakeHonchoAi220Surface()
    monkeypatch.setattr(surface, "workspace_exists", lambda **_kwargs: SimpleNamespace())
    transport = HonchoAi220ReadTransport(surface)

    with pytest.raises(HonchoReadAmbiguous, match="response_shape_drift"):
        transport.resource_exists(
            "workspace",
            workspace_id="existing-workspace",
            resource_id="existing-workspace",
            timeout_seconds=1.0,
        )
    _assert_no_mutation(surface)

"""``fallback=False`` pins an auxiliary call to the provider and model it resolved.

Some callers must never have their prompt re-sent elsewhere: the request may only go to the
route the operator approved for it. By default a capacity failure (timeout, 429, 5xx, 402)
on an explicit route walks the task ``fallback_chain`` and then the main agent's model, which
sends the same messages to another provider. With ``fallback=False`` the call either succeeds
on its own route or raises that route's error, after at most the same-route recovery steps
(parameter repairs, credential refresh, pool rotation) on the same provider and model.

The HTTP tests run the real client construction and transport against two local endpoints, one
for the pinned route and one for the main agent's model, and record every request each receives.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import openai
import pytest
import hermes_yaml as yaml

import agent.auxiliary_client as aux
from agent import auxiliary_error_policy as aux_errors
from agent.plugin_llm import _TrustPolicy, make_plugin_llm_for_test

PINNED_MODEL = "pinned-model"
MAIN_MODEL = "main-model"
MESSAGES = [{"role": "user", "content": "Summarize this."}]


def _completion(model):
    return {
        "id": "chatcmpl-pinned", "created": 1, "model": model, "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": f"from {model}"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def _serve(name, requests, behaviour):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((name, body.get("model")))
            status, payload, delay = behaviour(body)
            if delay:
                time.sleep(delay)
            data = json.dumps(payload).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError:  # the client gave up (the timeout case)
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}/v1"


FAILURES = {
    "rate_limit": (429, {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}, 0),
    "server_error": (500, {"error": {"message": "Internal server error", "type": "server_error"}}, 0),
    "bad_gateway": (502, {"error": {"message": "Bad gateway", "type": "server_error"}}, 0),
    "payment": (402, {"error": {"message": "Insufficient credits", "type": "insufficient_quota"}}, 0),
    "timeout": (200, _completion(PINNED_MODEL), 3.0),
}


@pytest.fixture
def routes(tmp_path, monkeypatch):
    """A failing pinned route and a healthy main-agent model; yields (configure, requests)."""
    aux.shutdown_cached_clients()
    aux._reset_aux_unhealthy_cache()
    requests: list[tuple[str, str]] = []
    failure = {"value": FAILURES["server_error"]}
    pinned = _serve("pinned", requests, lambda body: failure["value"])
    main = _serve("main", requests, lambda body: (200, _completion(body.get("model")), 0))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("PINNED_TEST_KEY", "pinned-key")
    monkeypatch.setenv("MAIN_TEST_KEY", "main-key")

    def configure(kind, *, transient_retries=0, task_config=None):
        failure["value"] = FAILURES[kind]
        config = {
            "model": {"provider": "custom:mainp", "default": MAIN_MODEL},
            "providers": {
                "mainp": {"base_url": main[2], "key_env": "MAIN_TEST_KEY"},
                "pinnedp": {"base_url": pinned[2], "key_env": "PINNED_TEST_KEY"},
            },
            "auxiliary": {"transient_retries": transient_retries, **(task_config or {})},
        }
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        aux.shutdown_cached_clients()
        return pinned[2]

    try:
        yield configure, requests
    finally:
        aux.shutdown_cached_clients()
        aux._reset_aux_unhealthy_cache()
        for server, thread, _url in (pinned, main):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _call(**kw):
    return aux.call_llm(provider="custom:pinnedp", model=PINNED_MODEL, messages=MESSAGES, timeout=1.0, **kw)


def _status(exc):
    return getattr(exc, "status_code", None)


# A 429 gets the ladder's one same-credential retry (after a short wait) on the same client: still
# the pinned route. Everything else is a single request with transient retries configured off.
SAME_ROUTE_ATTEMPTS = {"rate_limit": 2}


@pytest.mark.parametrize("kind", ["rate_limit", "server_error", "bad_gateway", "payment", "timeout"])
def test_pinned_call_raises_its_own_error_without_leaving_the_route(routes, kind):
    configure, requests = routes
    configure(kind)
    with pytest.raises(Exception) as caught:
        _call(fallback=False)
    assert requests == [("pinned", PINNED_MODEL)] * SAME_ROUTE_ATTEMPTS.get(kind, 1), (
        f"{kind}: every request must stay on the pinned route")
    expected_status, _payload, delay = FAILURES[kind]
    if delay:
        assert isinstance(caught.value, openai.APITimeoutError)
    else:
        assert _status(caught.value) == expected_status


@pytest.mark.parametrize("kind", ["server_error", "timeout"])
def test_default_call_still_falls_back_to_the_main_model(routes, kind):
    """Unchanged default: the same failure is rescued by the main agent's model."""
    configure, requests = routes
    configure(kind)
    response = _call()
    assert response.choices[0].message.content == f"from {MAIN_MODEL}"
    assert requests == [("pinned", PINNED_MODEL), ("main", MAIN_MODEL)]


def test_pinned_call_may_retry_the_same_route_but_never_leaves_it(routes):
    configure, requests = routes
    configure("server_error", transient_retries=2)
    with pytest.raises(Exception) as caught:
        _call(fallback=False)
    assert _status(caught.value) == 500
    assert len(requests) == 3, requests  # the first attempt plus two same-route retries
    assert set(requests) == {("pinned", PINNED_MODEL)}


def test_pinned_task_whose_client_cannot_be_built_sends_nothing(routes, monkeypatch):
    """The task's own fallback_chain also stays unused: no client, no request, a clear error."""
    configure, requests = routes
    monkeypatch.delenv("PINNED_TEST_KEY")
    task = {"pinned_task": {"provider": "anthropic", "model": PINNED_MODEL,
                            "fallback_chain": [{"provider": "custom:mainp", "model": MAIN_MODEL}]}}
    configure("server_error", task_config=task)
    with pytest.raises(aux.AuxiliaryClientUnavailable):
        aux.call_llm(task="pinned_task", messages=MESSAGES, timeout=1.0, fallback=False)
    assert requests == []
    # Unchanged default: the configured chain serves the call.
    response = aux.call_llm(task="pinned_task", messages=MESSAGES, timeout=1.0)
    assert response.choices[0].message.content == f"from {MAIN_MODEL}"
    assert requests == [("main", MAIN_MODEL)]


@pytest.mark.parametrize("task_config", [
    None,  # no task config: the main agent's route ("auto")
    {"auto_task": {"provider": "auto", "model": PINNED_MODEL,
                   "fallback_chain": [{"provider": "custom:mainp", "model": MAIN_MODEL}]}},
])
def test_pinned_auto_route_is_refused_before_any_request(routes, task_config):
    """The auto route walks main, fallback chains and discovery, so a pin there fails closed."""
    import asyncio

    configure, requests = routes
    configure("server_error", task_config=task_config)
    task = "auto_task" if task_config else None
    with pytest.raises(aux.AuxiliaryClientUnavailable, match="explicit provider"):
        aux.call_llm(task=task, messages=MESSAGES, timeout=1.0, fallback=False)
    with pytest.raises(aux.AuxiliaryClientUnavailable, match="explicit provider"):
        asyncio.run(aux.async_call_llm(task=task, messages=MESSAGES, timeout=1.0, fallback=False))
    with pytest.raises(aux.AuxiliaryClientUnavailable, match="explicit provider"):
        aux.call_llm(task="vision", messages=MESSAGES, timeout=1.0, fallback=False)
    assert requests == []


@pytest.mark.parametrize("prov", ["custom", "main"])
def test_pinned_bare_custom_or_unset_main_refuses_api_key_discovery(tmp_path, monkeypatch, prov):
    """Bare `custom` (or `main` with unset model.provider) has no endpoint and would walk API-key
    providers via discovery; fallback=False must refuse up front instead of sending a request."""
    import asyncio

    aux.shutdown_cached_clients()
    aux._reset_aux_unhealthy_cache()
    requests: list[tuple[str, str]] = []
    srv = _serve("discovered", requests, lambda body: (200, _completion(body.get("model")), 0))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Seed an API-key provider so _resolve_api_key_provider would otherwise discover it.
    monkeypatch.setenv("GLM_API_KEY", "dummy-zai-key")
    monkeypatch.setenv("GLM_BASE_URL", srv[2])
    cfg = {
        "model": {"default": ""},
        "auxiliary": {
            "t": {"provider": prov, "model": PINNED_MODEL},
            "vision": {"provider": prov, "model": PINNED_MODEL},
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    try:
        with pytest.raises(aux.AuxiliaryClientUnavailable, match="custom endpoint"):
            aux.call_llm(task="t", messages=MESSAGES, timeout=1.0, fallback=False)
        with pytest.raises(aux.AuxiliaryClientUnavailable, match="custom endpoint"):
            asyncio.run(aux.async_call_llm(task="t", messages=MESSAGES, timeout=1.0, fallback=False))
        with pytest.raises(aux.AuxiliaryClientUnavailable, match="custom endpoint"):
            aux.call_llm(provider=prov, messages=MESSAGES, timeout=1.0, fallback=False)
        with pytest.raises(aux.AuxiliaryClientUnavailable, match="custom endpoint"):
            aux.call_llm(task="vision", messages=MESSAGES, timeout=1.0, fallback=False)
        assert requests == []
    finally:
        aux.shutdown_cached_clients()
        srv[0].shutdown()
        srv[0].server_close()


@pytest.mark.parametrize("prov", ["ollama", "custom:ollama"])
def test_pinned_named_local_alias_provider_runs_on_its_own_endpoint(tmp_path, monkeypatch, prov):
    """A configured providers.ollama entry is a concrete named route and must not be refused by the custom guard."""
    import asyncio

    aux.shutdown_cached_clients()
    aux._reset_aux_unhealthy_cache()
    requests: list[tuple[str, str]] = []
    srv = _serve("ollama", requests, lambda body: (200, _completion(body.get("model")), 0))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("OLLAMA_TEST_KEY", "ollama-key")
    cfg = {
        "model": {"default": ""},
        "providers": {"ollama": {"base_url": srv[2], "key_env": "OLLAMA_TEST_KEY"}},
        "auxiliary": {"t": {"provider": prov, "model": PINNED_MODEL}},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    try:
        r1 = aux.call_llm(task="t", messages=MESSAGES, timeout=1.0, fallback=False)
        r2 = asyncio.run(aux.async_call_llm(task="t", messages=MESSAGES, timeout=1.0, fallback=False))
        assert r1.choices[0].message.content == f"from {PINNED_MODEL}"
        assert r2.choices[0].message.content == f"from {PINNED_MODEL}"
        assert requests == [("ollama", PINNED_MODEL), ("ollama", PINNED_MODEL)]
    finally:
        aux.shutdown_cached_clients()
        srv[0].shutdown()
        srv[0].server_close()


@pytest.mark.parametrize("value", [0, None, "false", 1])
def test_fallback_must_be_a_bool(routes, value):
    """A falsy non-bool must not quietly run the fallback ladder (or quietly pin)."""
    configure, requests = routes
    configure("server_error")
    with pytest.raises(TypeError):
        aux.call_llm(provider="custom:pinnedp", model=PINNED_MODEL, messages=MESSAGES, timeout=1.0, fallback=value)
    assert requests == []


# The async path always retries a transient failure once on the same provider.
@pytest.mark.parametrize("kind, status, attempts", [("payment", 402, 1), ("server_error", 500, 2)])
def test_async_pinned_call_raises_its_own_error_without_leaving_the_route(routes, kind, status, attempts):
    import asyncio

    configure, requests = routes
    configure(kind)
    with pytest.raises(Exception) as caught:
        asyncio.run(aux.async_call_llm(provider="custom:pinnedp", model=PINNED_MODEL, messages=MESSAGES,
                                       timeout=1.0, fallback=False))
    assert _status(caught.value) == status
    assert requests == [("pinned", PINNED_MODEL)] * attempts


def test_async_default_call_still_falls_back_to_the_main_model(routes):
    import asyncio

    configure, requests = routes
    configure("payment")
    response = asyncio.run(aux.async_call_llm(provider="custom:pinnedp", model=PINNED_MODEL, messages=MESSAGES,
                                              timeout=1.0))
    assert response.choices[0].message.content == f"from {MAIN_MODEL}"
    assert requests == [("pinned", PINNED_MODEL), ("main", MAIN_MODEL)]


# ── The ladder: same-route rungs are forwarded only when provider and model are unchanged ──


class _Status(Exception):
    def __init__(self, status):
        super().__init__(f"Error code: {status}")
        self.status_code = status


class _Client:
    api_key = "key"
    base_url = "https://pinned.example/v1"


def _request(client):
    return aux._PreparedAuxRequest(
        client=client, final_model=PINNED_MODEL, kwargs={"model": PINNED_MODEL, "messages": MESSAGES},
        resolved_provider="pinnedp", request_provider="pinnedp", resolved_model=PINNED_MODEL,
        resolved_base_url=None, resolved_api_key=None, resolved_api_mode=None, effective_timeout=1.0,
        effective_extra_body={}, base_info="https://pinned.example/v1", fallback=False)


def _drive(ladder, perform):
    """The production driver, with each performed step recorded."""
    seen = []

    def record(step):
        seen.append(step)
        return perform(step)

    try:
        return aux._drive_ladder(ladder, record), seen
    except Exception as exc:
        exc.seen = seen
        raise


def _fake_ladder(steps, log):
    """A ladder that yields ``steps`` in order and records how it ended."""
    try:
        for step in steps:
            try:
                yield step
            except Exception as exc:
                log.append(("failed", step.kind, _status(exc)))
        log.append(("exhausted",))
    except GeneratorExit:
        log.append(("closed",))
        raise
    raise _Status(599)


class _OtherEndpoint:
    api_key = "key"
    base_url = "https://elsewhere.example/v1"


def _pinned(make_steps, first_error, log):
    client = _Client()
    failure = aux._classify_failure(first_error, "pinnedp", client, PINNED_MODEL)
    return aux._pin_ladder_route(_fake_ladder(make_steps(client), log), failure, _request(client))


def _fail(status):
    def perform(step):
        raise _Status(status)
    return perform


def test_pinned_ladder_forwards_same_route_rungs():
    """Rate-limit waits, a same-provider retry and a same-client re-call (a parameter repair) pass."""
    log = []
    ladder = _pinned(lambda c: [aux._LadderStep("wait", (0,)),
                                aux._LadderStep("retry_same_provider", ("pinnedp", PINNED_MODEL, ())),
                                aux._LadderStep("call", (c, {"model": PINNED_MODEL})),
                                aux._LadderStep("call", (_Client(), {"model": PINNED_MODEL}))],
                     _Status(429), log)
    with pytest.raises(_Status) as caught:
        _drive(ladder, _fail(503))
    performed = [s.kind for s in caught.value.seen]
    assert performed == ["retry_same_provider", "call", "call"]
    assert caught.value.seen[0].args[:2] == ("pinnedp", PINNED_MODEL)
    assert log[-1] == ("exhausted",)


@pytest.mark.parametrize("make_step", [
    lambda c: aux._LadderStep("retry_same_provider", ("pinnedp", "another-model", ())),
    lambda c: aux._LadderStep("retry_same_provider", ("another-provider", PINNED_MODEL, ())),
    lambda c: aux._LadderStep("call", (c, {"model": "healed-model"})),
    lambda c: aux._LadderStep("call", (_OtherEndpoint(), {"model": PINNED_MODEL})),
    lambda c: aux._LadderStep("fallback", (object(), MAIN_MODEL, "main-agent(other)")),
    lambda c: aux._LadderStep("attempt", (object(), {"model": MAIN_MODEL}, "other")),
], ids=["retry-model-changed", "retry-provider-changed", "call-model-changed", "call-other-endpoint",
        "fallback", "attempt"])
def test_pinned_ladder_refuses_a_step_that_leaves_the_route(make_step):
    log = []
    ladder = _pinned(lambda c: [make_step(c)], _Status(429), log)
    with pytest.raises(_Status) as caught:
        _drive(ladder, lambda s: pytest.fail(f"dispatched an off-route step: {s.kind}"))
    assert _status(caught.value) == 429, "the route's own error is re-raised"
    assert caught.value.seen == []
    assert log == [("closed",)]


def test_pinned_ladder_reraises_the_latest_same_route_error():
    log = []
    ladder = _pinned(lambda c: [aux._LadderStep("retry_same_provider", ("pinnedp", PINNED_MODEL, ())),
                                aux._LadderStep("fallback", (object(), MAIN_MODEL, "main-agent(other)"))],
                     _Status(429), log)
    with pytest.raises(_Status) as caught:
        _drive(ladder, _fail(503))
    assert _status(caught.value) == 503
    assert log == [("failed", "retry_same_provider", 503), ("closed",)]


def test_real_ladder_pool_rotation_keeps_the_route_and_skips_provider_fallback(monkeypatch):
    """Credential-pool rotation retries on the pinned provider and model; the main-agent model is never tried."""
    client = _Client()
    req = _request(client)
    monkeypatch.setattr(aux, "_failure_pool", lambda route, failure: ("pinnedp", "key"))
    monkeypatch.setattr(aux, "_recover_failure_pool", lambda route, failure: True)
    monkeypatch.setattr(aux_errors, "same_key_retry", lambda verdict: False)
    monkeypatch.setattr(aux, "_try_main_agent_model_fallback",
                        lambda *a, **kw: pytest.fail("main-agent fallback consulted on a pinned call"))
    monkeypatch.setattr(aux, "_try_configured_fallback_chain",
                        lambda *a, **kw: pytest.fail("task chain consulted on a pinned call"))
    failure = aux._classify_failure(_Status(429), "pinnedp", client, PINNED_MODEL)
    assert failure.verdict.should_rotate_credential, "fixture: a 429 must reach the pool rung"
    ladder = aux._start_recovery_ladder(failure, req, {"max_tokens": None, "main_runtime": None},
                                        task="pinned_task", async_mode=False, route_info=None)
    with pytest.raises(_Status) as caught:
        _drive(ladder, _fail(429))
    performed = [(s.kind, s.args[:2]) for s in caught.value.seen]
    assert performed == [("retry_same_provider", ("pinnedp", PINNED_MODEL))]
    assert _status(caught.value) == 429


def test_real_ladder_refuses_an_auth_refresh_retry_under_another_provider(monkeypatch):
    """A same-route rung that would retry under a different provider is stopped before it is sent."""
    client = _Client()
    req = _request(client)
    monkeypatch.setattr(aux, "_auth_refresh_provider_for_route", lambda *a, **kw: "another-provider")
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda *a, **kw: True)
    monkeypatch.setattr(aux, "_evict_cached_clients", lambda *a, **kw: None)
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **kw: None)
    failure = aux._classify_failure(_Status(401), "pinnedp", client, PINNED_MODEL)
    assert aux_errors.auth_refresh(failure.verdict), "fixture: a 401 must reach the auth-refresh rung"
    ladder = aux._start_recovery_ladder(failure, req, {"max_tokens": None, "main_runtime": None},
                                        task="pinned_task", async_mode=False, route_info=None)
    with pytest.raises(_Status) as caught:
        _drive(ladder, lambda s: pytest.fail(f"dispatched {s.kind} {s.args[:2]} off the pinned route"))
    assert _status(caught.value) == 401
    assert caught.value.seen == []


# ── ctx.llm ──


def _plugin_llm(seen):
    def caller(**kw):
        seen.append(kw)
        return ("p", "m", {"choices": [{"message": {"content": "ok"}}]})

    return make_plugin_llm_for_test(plugin_id="pinned-test", policy=_TrustPolicy(plugin_id="pinned-test"), sync_caller=caller)


def test_plugin_llm_passes_fallback_false_through():
    seen = []
    _plugin_llm(seen).complete(MESSAGES, fallback=False)
    _plugin_llm(seen).complete_structured(instructions="x", input=[{"type": "text", "text": "y"}],
                                          json_mode=True, fallback=False)
    assert [kw.get("fallback") for kw in seen] == [False, False]
    call_kw, _route_info = _plugin_llm([])._host_kwargs(seen[0])
    assert call_kw["fallback"] is False


def test_plugin_llm_default_call_kwargs_are_unchanged():
    seen = []
    _plugin_llm(seen).complete(MESSAGES)
    assert "fallback" not in seen[0]
    call_kw, _route_info = _plugin_llm([])._host_kwargs(seen[0])
    assert "fallback" not in call_kw


def test_plugin_llm_rejects_a_non_boolean_fallback():
    with pytest.raises(TypeError):
        _plugin_llm([]).complete(MESSAGES, fallback="false")

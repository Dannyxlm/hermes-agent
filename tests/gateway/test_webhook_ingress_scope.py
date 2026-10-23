"""Profile scope a webhook route's script, prompt render and skill lookup run under.

The runner binds a profile scope only later, around ``handle_message``; the ingress work before
that (``WebhookAdapter._ingress_scope``) must already run as the route's owner:

- a ``/p/<profile>/`` request keeps the routed profile's scope;
- under ``multiplex_profiles`` a bare route belongs to ``default`` (``_route_allows_profile``), and
  the primary adapter map and its config are the DEFAULT ROOT's even when a named profile launched
  the process (``gateway.run.load_gateway_config_for_runner``), so it binds the default root —
  never the named launcher's home;
- a standalone gateway whose fail-closed guard was flipped by a hosted room binds its launch
  profile's own scope (``.env`` over the env frozen at activation), so env-only keys survive (#112878);
- a single-profile gateway enters no scope at all and reads the process environment.

Each test reads a synthetic scoped secret while the script's subprocess env is built, the point
where an unbound read raised ``UnscopedSecretError`` and the event was dropped as "script ignored".
"""

import asyncio
import threading
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from tests.waiting import async_wait_until

_KEY = "WEBHOOK_SCOPE_PROBE_KEY"
_PROBE = (
    "import json, sys\n"
    "payload = json.load(sys.stdin)\n"
    "print(json.dumps({'body': 'ran:' + payload['note']}))\n"
)


def _home(path, secret=None):
    path.mkdir(parents=True, exist_ok=True)
    (path / "scripts").mkdir(exist_ok=True)
    (path / "scripts" / "probe.py").write_text(_PROBE, encoding="utf-8")
    if secret is not None:
        (path / ".env").write_text(f"{_KEY}={secret}\n", encoding="utf-8")
    return path


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Custom root with two named profiles; the process was launched from ``launcher``."""
    root = _home(tmp_path / "root", "default-owner")
    launcher = _home(root / "profiles" / "launcher", "launcher-value")
    worker = _home(root / "profiles" / "worker", "worker-value")
    monkeypatch.setenv("HERMES_HOME", str(launcher))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, **_kw: [("default", root), ("launcher", launcher), ("worker", worker)],
    )
    return {"root": root, "launcher": launcher, "worker": worker}


@pytest.fixture
def seen(monkeypatch):
    """``(home, secret)`` observed each time a route script's subprocess env is built."""
    import tools.environments.local as local_env
    from agent.secret_scope import get_secret
    from hermes_constants import get_hermes_home

    records = []
    real_build = local_env.build_subprocess_env

    def _build_reading_a_secret(*args, **kwargs):
        records.append((get_hermes_home().resolve(), get_secret(_KEY)))
        return real_build(*args, **kwargs)

    monkeypatch.setattr(local_env, "build_subprocess_env", _build_reading_a_secret)
    return records


@pytest.fixture
def multiplex_guard():
    from agent.secret_scope import set_multiplex_active
    set_multiplex_active(True)
    yield
    set_multiplex_active(False)


def _adapter(routes, *, multiplex_profiles):
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0, "routes": routes, "rate_limit": 100}))
    runner = MagicMock()
    runner.config.multiplex_profiles = multiplex_profiles
    adapter.gateway_runner = runner
    adapter.captured = []

    async def _capture(event):
        adapter.captured.append(event)

    adapter.handle_message = _capture
    return adapter


def _app(adapter):
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    app.router.add_post("/p/{profile}/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _route(**extra):
    return {"secret": _INSECURE_NO_AUTH, "script": "probe.py", "prompt": "Script: {body}", **extra}


async def _post(cli, path, delivery, note="hi"):
    return await cli.post(path, json={"note": note}, headers={"X-GitHub-Delivery": delivery})


@pytest.mark.asyncio
async def test_bare_route_binds_default_root_not_named_launcher(homes, seen, multiplex_guard):
    """A named launcher's home is not the bare route's owner: default's script reads default's secret."""
    adapter = _adapter({"probe": _route()}, multiplex_profiles=True)
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await _post(cli, "/webhooks/probe", "bare-1")
        assert resp.status == 202, await resp.text()
    await async_wait_until(lambda: adapter.captured, what="bare route dispatch")
    assert adapter.captured[0].text == "Script: ran:hi"
    assert seen == [(homes["root"].resolve(), "default-owner")]


@pytest.mark.asyncio
async def test_bare_route_and_explicit_default_prefix_bind_the_same_scope(homes, seen, multiplex_guard):
    """``/webhooks/x`` and ``/p/default/webhooks/x`` are one authorization; they must be one scope."""
    adapter = _adapter({"probe": _route()}, multiplex_profiles=True)
    async with TestClient(TestServer(_app(adapter))) as cli:
        assert (await _post(cli, "/webhooks/probe", "same-1")).status == 202
        assert (await _post(cli, "/p/default/webhooks/probe", "same-2")).status == 202
    await async_wait_until(lambda: len(adapter.captured) == 2, what="both dispatches")
    assert seen == [(homes["root"].resolve(), "default-owner")] * 2


@pytest.mark.asyncio
async def test_explicit_profile_route_keeps_routed_scope(homes, seen, multiplex_guard):
    adapter = _adapter({"probe": _route(profile="worker")}, multiplex_profiles=True)
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await _post(cli, "/p/worker/webhooks/probe", "routed-1")
        assert resp.status == 202, await resp.text()
    await async_wait_until(lambda: adapter.captured, what="routed dispatch")
    assert adapter.captured[0].source.profile == "worker"
    assert seen == [(homes["worker"].resolve(), "worker-value")]


@pytest.mark.asyncio
async def test_bare_scope_does_not_widen_route_authorization(homes, seen, multiplex_guard):
    """A route bound to one profile stays 404 bare and under any other prefix; its script never runs."""
    adapter = _adapter({"probe": _route(profile="worker")}, multiplex_profiles=True)
    async with TestClient(TestServer(_app(adapter))) as cli:
        assert (await _post(cli, "/webhooks/probe", "deny-1")).status == 404
        assert (await _post(cli, "/p/launcher/webhooks/probe", "deny-2")).status == 404
        assert (await _post(cli, "/p/default/webhooks/probe", "deny-3")).status == 404
    assert seen == []
    assert adapter.captured == []


@pytest.mark.asyncio
async def test_concurrent_bare_and_routed_scripts_do_not_share_scope(homes, seen, multiplex_guard, monkeypatch):
    """Both script envs are built at the same moment on worker threads; each keeps its own scope."""
    import tools.environments.local as local_env
    observed_build = local_env.build_subprocess_env
    overlap = threading.Barrier(2, timeout=10)

    def _build_together(*args, **kwargs):
        overlap.wait()  # both requests are inside their ingress scope here
        return observed_build(*args, **kwargs)

    monkeypatch.setattr(local_env, "build_subprocess_env", _build_together)
    adapter = _adapter({"probe": _route(), "work": _route(profile="worker")}, multiplex_profiles=True)
    async with TestClient(TestServer(_app(adapter))) as cli:
        bare, routed = await asyncio.gather(
            _post(cli, "/webhooks/probe", "conc-1"), _post(cli, "/p/worker/webhooks/work", "conc-2"))
        assert (bare.status, routed.status) == (202, 202)
    await async_wait_until(lambda: len(adapter.captured) == 2, what="both dispatches")
    assert sorted(seen) == sorted([(homes["root"].resolve(), "default-owner"),
                                   (homes["worker"].resolve(), "worker-value")])


@pytest.mark.asyncio
async def test_script_failure_leaves_no_scope_behind(homes, seen, multiplex_guard, monkeypatch):
    """A failing env build drops that event only; the next one re-enters a fresh, correct scope."""
    from hermes_constants import get_hermes_home_override
    import tools.environments.local as local_env
    observed_build = local_env.build_subprocess_env
    calls = []

    def _fail_first(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            observed_build(*args, **kwargs)  # record the scope it ran in, then fail
            raise RuntimeError("synthetic env failure")
        return observed_build(*args, **kwargs)

    monkeypatch.setattr(local_env, "build_subprocess_env", _fail_first)
    adapter = _adapter({"probe": _route()}, multiplex_profiles=True)
    async with TestClient(TestServer(_app(adapter))) as cli:
        failed = await _post(cli, "/webhooks/probe", "err-1")
        assert failed.status == 200
        assert (await failed.json())["reason"] == "script"
        assert (await _post(cli, "/webhooks/probe", "err-2", note="again")).status == 202
    await async_wait_until(lambda: adapter.captured, what="second dispatch")
    assert get_hermes_home_override() is None
    assert seen == [(homes["root"].resolve(), "default-owner")] * 2
    assert [e.text for e in adapter.captured] == ["Script: ran:again"]


@pytest.mark.asyncio
async def test_standalone_hosted_guard_keeps_launch_env_only_keys(tmp_path, monkeypatch, seen):
    """``multiplex_profiles: false`` + a hosted room flipped the guard: the bare route binds the launch
    profile's own scope, so a key injected only into the launch env (no ``.env`` line) still resolves."""
    from tui_gateway.launch_profile_policy import activate_multi_profile_hosting
    from agent.secret_scope import set_multiplex_active

    launch = _home(tmp_path / "standalone")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv(_KEY, "from-launch-env")
    adapter = _adapter({"probe": _route()}, multiplex_profiles=False)
    activate_multi_profile_hosting()
    try:
        async with TestClient(TestServer(_app(adapter))) as cli:
            resp = await _post(cli, "/webhooks/probe", "standalone-1")
            assert resp.status == 202, await resp.text()
        await async_wait_until(lambda: adapter.captured, what="standalone dispatch")
    finally:
        set_multiplex_active(False)
    assert seen == [(launch.resolve(), "from-launch-env")]


@pytest.mark.asyncio
async def test_single_profile_gateway_enters_no_scope(tmp_path, monkeypatch, seen):
    """No multiplexing anywhere: no home override, and the secret comes from the process env."""
    from hermes_constants import get_hermes_home_override
    import tools.environments.local as local_env
    observed_build = local_env.build_subprocess_env
    overrides = []

    def _note_override(*args, **kwargs):
        overrides.append(get_hermes_home_override())
        return observed_build(*args, **kwargs)

    monkeypatch.setattr(local_env, "build_subprocess_env", _note_override)
    home = _home(tmp_path / "single", "dotenv-value")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(_KEY, "process-env-value")
    adapter = _adapter({"probe": _route()}, multiplex_profiles=False)
    async with TestClient(TestServer(_app(adapter))) as cli:
        resp = await _post(cli, "/webhooks/probe", "single-1")
        assert resp.status == 202, await resp.text()
    await async_wait_until(lambda: adapter.captured, what="single-profile dispatch")
    assert overrides == [None]
    assert seen == [(home.resolve(), "process-env-value")]

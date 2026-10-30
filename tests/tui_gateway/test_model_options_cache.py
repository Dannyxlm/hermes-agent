"""``model.options`` is built once per profile, config and credential state, not on every chat open.

Building the picker is about a second of credential-pool and provider probes; the phone asks for it on
every chat open. Counted by builder invocations behind the real RPC handler.
"""
from types import SimpleNamespace

import pytest

from tui_gateway import server


@pytest.fixture
def options(monkeypatch, tmp_path):
    homes = {name: tmp_path / "profiles" / name for name in ("work", "play")}
    for home in homes.values():
        home.mkdir(parents=True)
    context = SimpleNamespace(current_provider="anthropic", current_model="sonnet")
    builds = []
    monkeypatch.setattr(server, "_profile_home", lambda name: homes.get(name))
    monkeypatch.setattr(server, "_model_picker_context", lambda agent: context)
    monkeypatch.setattr(server, "_MODEL_OPTIONS_CACHE", {}, raising=False)

    def build(ctx, **kwargs):
        builds.append(kwargs)
        return {"providers": [{"slug": f"build-{len(builds)}"}]}

    monkeypatch.setattr("hermes_cli.inventory.build_model_options_payload", build)

    def call(profile="work", **params):
        response = server._methods["model.options"](1, {"profile": profile, **params})
        return response["result"]["providers"][0]["slug"]

    call.builds, call.homes, call.context = builds, homes, context
    return call


def test_repeated_opens_build_once_and_each_gets_its_own_copy(options):
    assert [options() for _ in range(3)] == ["build-1"] * 3
    assert len(options.builds) == 1
    first = server._methods["model.options"](1, {"profile": "work"})["result"]
    first["providers"].clear()
    assert server._methods["model.options"](1, {"profile": "work"})["result"]["providers"] == [{"slug": "build-1"}]


def test_each_profile_and_each_flag_set_has_its_own_picker(options):
    assert (options("work"), options("play"), options("work", include_unconfigured=True)) == (
        "build-1", "build-2", "build-3")
    assert options("work") == "build-1" and options("play") == "build-2"


def test_refresh_rebuilds_and_what_it_built_is_served_next(options):
    assert options() == "build-1"
    assert options(refresh=True) == "build-2"
    assert options() == "build-2"


@pytest.mark.parametrize("name", [".env", "auth.json"])
def test_a_credential_change_rebuilds(options, name):
    assert options() == "build-1"
    (options.homes["work"] / name).write_text("KEY=1\n")
    assert options() == "build-2"
    assert options() == "build-2"


def test_a_session_that_switched_model_gets_its_own_picker(options):
    assert options() == "build-1"
    options.context.current_model = "opus"
    assert options() == "build-2"


def test_an_old_picker_expires(options, monkeypatch):
    assert options() == "build-1"
    monkeypatch.setattr(server, "_MODEL_OPTIONS_TTL_S", 0.0)
    assert options() == "build-2"

"""Regression tests: xAI availability check_fns use read-only has_xai_credentials() and never trigger pool refresh."""

from __future__ import annotations

import json


def test_all_five_xai_availability_checks_are_readonly(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    def _boom(**_kwargs):
        raise AssertionError("availability check must not call resolve_xai_http_credentials()")

    from tools import xai_http
    from tools import x_search_tool
    import plugins.image_gen.xai as xai_image

    monkeypatch.setattr(xai_http, "resolve_xai_http_credentials", _boom)
    monkeypatch.setattr(x_search_tool, "resolve_xai_http_credentials", _boom)
    monkeypatch.setattr(xai_image, "resolve_xai_http_credentials", _boom)

    from tools.transcription_cloud import _has_xai_stt_credentials
    from tools.tts_streaming import XAIStreamer
    from tools.tts_tool import _xai_requirements

    # With no credentials on disk or env -> all 5 return False without calling resolver.
    assert x_search_tool.check_x_search_requirements() is False
    assert _has_xai_stt_credentials() is False
    assert _xai_requirements() is False
    assert XAIStreamer.available() is False
    assert xai_image.XAIImageGenProvider().is_available() is False

    # With an xai-oauth pool row in auth.json -> all 5 return True without calling resolver.
    (home / "auth.json").write_text(
        json.dumps({"auth_version": 2, "credential_pool": {"xai-oauth": [{"id": "g1", "access_token": "tok-1"}]}}),
        encoding="utf-8",
    )
    assert x_search_tool.check_x_search_requirements() is True
    assert _has_xai_stt_credentials() is True
    assert _xai_requirements() is True
    assert XAIStreamer.available() is True
    assert xai_image.XAIImageGenProvider().is_available() is True


def test_root_xai_grant_offers_tools_in_a_named_profile(tmp_path, monkeypatch):
    import plugins.image_gen.xai as xai_image
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions
    from tools.transcription_cloud import _has_xai_stt_credentials
    from tools.tts_streaming import XAIStreamer
    from tools.tts_tool import _xai_requirements
    from tools.x_search_tool import check_x_search_requirements

    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "teammate"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    (root / "auth.json").write_text(
        json.dumps({
            "version": 1,
            "credential_pool": {"xai-oauth": [{
                "id": "root-grant", "access_token": "test-access", "refresh_token": "test-refresh",
            }]},
        }),
        encoding="utf-8",
    )

    toolsets = _get_platform_tools(load_config(), "cli", include_default_mcp_servers=False)
    definitions = get_tool_definitions(enabled_toolsets=sorted(toolsets), quiet_mode=True)
    assert "x_search" in {definition["function"]["name"] for definition in definitions}
    assert check_x_search_requirements() is True
    assert _has_xai_stt_credentials() is True
    assert _xai_requirements() is True
    assert XAIStreamer.available() is True
    assert xai_image.XAIImageGenProvider().is_available() is True


def test_xai_availability_does_not_write_a_corrupt_root_backup(tmp_path, monkeypatch):
    from tools.xai_http import has_xai_credentials

    profile = tmp_path / "profiles" / "teammate"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    (tmp_path / "auth.json").write_text("{invalid JSON", encoding="utf-8")

    assert has_xai_credentials() is False
    assert not (tmp_path / "auth.json.corrupt").exists()

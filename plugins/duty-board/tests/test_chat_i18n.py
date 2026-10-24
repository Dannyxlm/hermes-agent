"""Pinned Intl cardinal outcomes; test execution is Python-only and offline."""
import re
import sys
from types import ModuleType

import pytest

from .test_chat_summary import sample_board

VALUES = [0, 1, 2, 3, 5, 11, 21, 22, 100, 1.5, 1000000]
# Recorded with node v26: new Intl.PluralRules(locale).select(n).
PINNED = {
    "en": "other one other other other other other other other other other",
    "zh": "other other other other other other other other other other other",
    "zh-hant": "other other other other other other other other other other other",
    "ja": "other other other other other other other other other other other",
    "ko": "other other other other other other other other other other other",
    "de": "other one other other other other other other other other other",
    "es": "other one other other other other other other other other many",
    "fr": "one one other other other other other other other one many",
    "it": "other one other other other other other other other other many",
    "pt": "one one other other other other other other other one many",
    "ru": "many one few few many many one few many other many",
    "uk": "many one few few many many one few many other many",
    "tr": "other one other other other other other other other other other",
    "hu": "other one other other other other other other other other other",
    "af": "other one other other other other other other other other other",
    "ga": "other one two few few other other other other other other",
    "ar": "zero one two few few many many many other other other",
}


@pytest.fixture
def text(commands):
    import importlib
    return importlib.import_module(commands.__package__ + ".summary_text")


@pytest.mark.parametrize("locale", PINNED)
def test_plural_selector_pinned(text, locale):
    assert [text.plural_category(locale, n) for n in VALUES] == PINNED[locale].split()


@pytest.mark.parametrize("locale", ["ar", "ga"])
def test_plural_fractional_ranges_are_not_integer_ranges(text, locale):
    assert all(text.plural_category(locale, n) == "other" for n in [3.5, 5.5, 7.5, 11.5, 103.5])


@pytest.mark.parametrize("locale", PINNED)
def test_all_locales_render(text, locale):
    viewer = {"is_owner": True, "platform": "discord", "link_style": "discord"}
    result = text.render_summary(sample_board(), locale=locale, now=10000, viewer=viewer)
    assert not re.search(r"\{\w+\}", result)
    assert "None" not in result
    assert "chat." not in result
    assert len(result) <= 1900
    if locale != "en":
        assert result != text.render_summary(sample_board(), locale="en", now=10000, viewer=viewer)
    for key in ("ownerOnly", "configureOwner", "oldHost", "dmHint", "empty", "error"):
        assert not re.search(r"\{\w+\}", text.message(locale, key, error="RuntimeError"))
    assert text.message(locale, "more", count=52)


def test_language_host_fallback_and_explicit_precedence(text, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)  # no host: read the environment
    monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
    (tmp_path / "config.yaml").write_text("display:\n  skin: x\n  language: ja  # set by setup\n", encoding="utf-8")
    assert text.choose_locale(home=tmp_path) == "ja"
    assert text.choose_locale(settings={"chat_language": "zh"}, home=tmp_path) == "zh"
    assert text.choose_locale("de", {"chat_language": "zh"}, tmp_path) == "de"
    monkeypatch.setenv("HERMES_LANGUAGE", "ko")
    assert text.choose_locale(home=tmp_path) == "ko"
    monkeypatch.delenv("HERMES_LANGUAGE")
    (tmp_path / "config.yaml").write_text("display: {language: ja}\nlanguage: fr\n", encoding="utf-8")
    assert text.choose_locale(home=tmp_path) == "en"  # flow style and other sections are not guessed
    (tmp_path / "config.yaml").write_bytes(b"\xff\xfe")
    assert text.choose_locale(home=tmp_path) == "en"
    assert text.choose_locale(home=tmp_path / "missing") == "en"
    assert text.duration(5 * 60, "zh") == "5分钟"
    assert text.duration(3 * 86400 + 4 * 3600, "en") == "3d 4h"


def test_language_uses_the_hosts_scoped_secret(text, tmp_path, monkeypatch):
    scope = ModuleType("agent.secret_scope")
    scope.get_secret = lambda name, default=None: "uk" if name == "HERMES_LANGUAGE" else default
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    monkeypatch.setenv("HERMES_LANGUAGE", "ja")  # the launch profile's value must not win
    assert text.choose_locale(home=tmp_path) == "uk"

    def unscoped(name, default=None):
        raise RuntimeError("no profile secret scope while multiplexing")
    scope.get_secret = unscoped
    (tmp_path / "config.yaml").write_text("display:\n  language: it\n", encoding="utf-8")
    assert text.choose_locale(home=tmp_path) == "it"


def test_slash_language_arguments(commands, home):
    from .test_chat_summary import settings, waiting
    waiting(home)
    settings(home, chat_language="zh")
    assert "项等你回复" in commands.duty()
    assert "waiting on you" in commands.duty("lang=en")
    assert "件が返信待ち" in commands.duty("ja")
    assert "項等你回覆" in commands.duty("full lang=zh_TW")
    assert commands.duty("full '") == "值班台暂不可用（ValueError）。"

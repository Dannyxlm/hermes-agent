"""Synthetic commands/rendering/access tests; no host data or network."""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import re
import sys
from types import SimpleNamespace

import pytest

from .fixtures.make_home import add_lease, add_message, add_session, db

OWNER = "222222222222222222"


@pytest.fixture
def text(commands):
    return importlib.import_module(commands.__package__ + ".summary_text")


def settings(home, **values):
    (home / "duty-board" / "settings.json").write_text(json.dumps(values), encoding="utf-8")


def caller(commands, monkeypatch, *, user=OWNER, platform="discord", chat_type="dm", bound=True):
    """Simulate a call inside the gateway. ``bound=False`` is a host that binds nothing (before 0.21.7)."""
    values = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_USER_ID": user, "HERMES_SESSION_CHAT_TYPE": chat_type}
    monkeypatch.setattr(commands, "_session", (lambda name: values.get(name, "")) if bound else (lambda name: None))
    monkeypatch.setattr(commands, "_in_gateway", lambda: True)


def waiting(home, sid="mine", title="OWNERTITLE"):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(home, sid, title=title, now=now)
    add_message(home, sid, "assistant", "OWNER_REPLY May I continue?", timestamp=now - 300)


def no_board_read(commands, monkeypatch):
    reads = []
    monkeypatch.setattr(commands.board, "cached_summary", lambda **kwargs: reads.append(True) or {})
    monkeypatch.setattr(commands.board, "build", lambda *args, **kwargs: reads.append(True) or {})
    return reads


def test_owner_and_non_owner(commands, home, monkeypatch):
    settings(home, owner=OWNER)
    waiting(home)
    caller(commands, monkeypatch)
    assert "OWNERTITLE" in commands.duty()
    reads = no_board_read(commands, monkeypatch)
    caller(commands, monkeypatch, user="999")
    assert commands.duty() == "This summary is only available to the board’s owner."
    assert reads == []


def test_owner_gate_ignores_content_setting(commands, home, monkeypatch):
    settings(home, owner=OWNER, hide_other_people_content=False)
    reads = no_board_read(commands, monkeypatch)
    caller(commands, monkeypatch, user="999")
    assert "only available" in commands.duty("full")
    assert reads == []


@pytest.mark.parametrize("platform", ["discord", "telegram", "slack", "feishu"])
def test_no_owner_gateway_refuses_before_collection(commands, home, monkeypatch, platform):
    settings(home)
    reads = no_board_read(commands, monkeypatch)
    caller(commands, monkeypatch, platform=platform)
    assert "Set owner" in commands.duty()
    assert reads == []


@pytest.mark.parametrize("platform", ["", "cli", "tui"])
def test_local_summary_without_owner(commands, home, monkeypatch, platform):
    waiting(home)
    monkeypatch.setattr(commands, "_session", lambda name: platform if name == "HERMES_SESSION_PLATFORM" else "")
    assert "OWNERTITLE" in commands.duty()


def test_old_host_fails_closed(commands, home, monkeypatch):
    settings(home, owner=OWNER)
    reads = no_board_read(commands, monkeypatch)
    caller(commands, monkeypatch, bound=False)
    assert "0.21.7" in commands.duty()
    assert reads == []


@pytest.mark.parametrize("platform", ["", "cli", "tui"])
def test_local_surface_bound_inside_gateway_process_is_local(commands, home, monkeypatch, platform):
    """A bound local surface (the TUI binds source=tui) is the local user even
    if the process also looks like a gateway."""
    settings(home, owner=OWNER)
    waiting(home)
    caller(commands, monkeypatch, platform=platform, user="", chat_type="")
    assert "OWNERTITLE" in commands.duty()


def test_missing_caller_id_refused(commands, home, monkeypatch):
    settings(home, owner=OWNER)
    reads = no_board_read(commands, monkeypatch)
    caller(commands, monkeypatch, user="")
    assert "only available" in commands.duty()
    assert reads == []


@pytest.mark.parametrize("chat_type", ["group", "channel", "thread", ""])
def test_channel_counts_hide_every_field(commands, home, monkeypatch, chat_type):
    settings(home, owner=OWNER)
    waiting(home)
    caller(commands, monkeypatch, chat_type=chat_type)
    result = commands.duty()
    assert "1 waiting on you" in result
    assert "OWNERTITLE" not in result
    assert "OWNER_REPLY" not in result
    assert "Server" not in result
    assert "discord.com" not in result


def test_full_in_channel_requires_explicit_opt_in(commands, home, monkeypatch):
    settings(home, owner=OWNER)
    waiting(home)
    caller(commands, monkeypatch, chat_type="channel")
    result = commands.duty("full")
    assert "OWNERTITLE" not in result
    assert "DM the bot" in result
    settings(home, owner=OWNER, chat_full_in_channels=True)
    assert "OWNERTITLE" in commands.duty("full")
    assert "OWNERTITLE" not in commands.duty()  # opt-in never changes default
    assert "OWNERTITLE" not in commands.duty("counts")


@pytest.mark.parametrize("local", [False, True])
def test_explicit_counts_suppress_dm_and_local_details(commands, home, monkeypatch, local):
    settings(home, owner=OWNER)
    waiting(home)
    if not local:
        caller(commands, monkeypatch)
    assert "OWNERTITLE" in commands.duty()
    assert "OWNERTITLE" not in commands.duty("counts")


@pytest.mark.parametrize("value", ["true", 1, [], {}, None])
def test_channel_opt_in_is_strict_boolean(commands, home, monkeypatch, value):
    settings(home, owner=OWNER, chat_full_in_channels=value)
    waiting(home)
    caller(commands, monkeypatch, chat_type="thread")
    assert "OWNERTITLE" not in commands.duty("full")


def test_settings_change_invalidates_private_cache(commands, home, monkeypatch):
    waiting(home)
    add_session(home, "other", user_id="999", title="OTHERSECRET")
    add_message(home, "other", "assistant", "OTHERSECRET Continue?", timestamp=float((home / "fixture-now").read_text(encoding="utf-8-sig")) - 200)
    settings(home, owner=OWNER, hide_other_people_content=False)
    caller(commands, monkeypatch)
    assert "OTHERSECRET" in commands.duty()
    settings(home, owner=OWNER, hide_other_people_content=True)
    assert "OTHERSECRET" not in commands.duty()
    settings(home, owner="999")
    assert "only available" in commands.duty()
    caller(commands, monkeypatch, user="999")
    assert "OWNERTITLE" not in commands.duty()


def test_collection_uses_authorized_settings_snapshot(commands, home, monkeypatch):
    settings(home, owner=OWNER)
    add_session(home, "other", user_id="999", title="SNAPSHOTSECRET")
    add_message(home, "other", "assistant", "Continue?", timestamp=float((home / "fixture-now").read_text(encoding="utf-8-sig")) - 200)
    original = commands.board._connect

    def change_settings_during_build():
        settings(home, owner=OWNER, hide_other_people_content=False)
        return original()

    monkeypatch.setattr(commands.board, "_connect", change_settings_during_build)
    caller(commands, monkeypatch)
    assert "SNAPSHOTSECRET" not in commands.duty()


def test_other_people_text_never_reaches_chat(commands, home, monkeypatch):
    settings(home, owner=OWNER)
    add_session(home, "other", user_id="999", title="OTHERSECRET")
    add_message(home, "other", "user", "OTHERSECRET ask")
    add_lease(home, "other")
    conn = db(home)
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    conn.execute("INSERT INTO async_delegations (delegation_id, state, delivery_state, dispatched_at, updated_at, task_json, result_json) VALUES ('secret', 'completed', 'dropped', ?, ?, ?, ?)", (now - 300, now - 100, json.dumps({"user_id": "999", "goal": "OTHERSECRET goal"}), json.dumps({"summary": "OTHERSECRET result"})))
    conn.commit()
    conn.close()
    caller(commands, monkeypatch)
    result = commands.duty()
    assert "Content hidden" in result
    assert "OTHERSECRET" not in result
    assert "discord.com" not in result


def sample_board(now=10000):
    link = "https://discord.com/channels/111/222/333"
    item = {"title": "WAIT_TITLE", "where": "PLACE", "source": "discord", "since": now - 300, "link": link, "ask": "NEVER_ASK", "reply_tail": "NEVER_REPLY"}
    return {"conversations": {"waiting": [item], "unanswered": [dict(item, title="UNANSWERED")], "running": [dict(item, title="RUN_TITLE", stalled=True)], "done": []}, "delegations": {"running": [], "dropped": [{"goal": "BACKGROUND_GOAL", "summary": "NEVER_SUMMARY", "session": item, "completed_at": now - 400}]}, "cron": {"problems": [{"name": "CRON_NAME", "last_at": now - 600}]}, "goals": {"items": [{"goal": "STANDING_GOAL", "session": item, "activity_at": now - 500}]}}


def render(text, data, **viewer):
    return text.render_summary(data, locale="en", now=10000, viewer={"is_owner": True, "platform": "discord", "link_style": "discord", **viewer})


def test_all_sections_links_and_empty(text):
    result = render(text, sample_board())
    for token in ("WAIT_TITLE", "UNANSWERED", "RUN_TITLE", "BACKGROUND_GOAL", "CRON_NAME", "STANDING_GOAL", "stalled"):
        # Markdown underscores are escaped.
        assert token.replace("_", "\\_") in result
    assert "<https://discord.com/channels/111/222/333>" in result
    assert "discord://" not in result
    assert "NEVER" not in result
    assert "https://" not in render(text, sample_board(), link_style="plain")
    assert render(text, {}) == "Nothing needs you right now."


def test_private_marked_items_distrust_every_field(text):
    data = sample_board()
    for _, items, _ in text.sections(data):
        for item in items:
            item.update(private=True, title="SECRET", goal="SECRET", name="SECRET", ask="SECRET", reply_tail="SECRET", summary="SECRET", where="SECRET")
    result = render(text, data)
    assert "SECRET" not in result
    assert "discord.com" not in result


def test_renderer_refuses_non_owner(text):
    assert render(text, sample_board(), is_owner=False) == "This summary is only available to the board’s owner."


@pytest.mark.parametrize("field", ["title", "where", "goal", "name"])
def test_mentions_neutralized(text, field):
    data = sample_board()
    data["conversations"]["waiting"][0][field] = "@everyone <@1> <#2> <@&3>"
    result = render(text, data)
    assert "@everyone" not in result
    assert "<@1>" not in result
    assert "<#2>" not in result
    assert "<@&3>" not in result
    assert "@\u200beveryone" in result


def test_angle_mentions_and_roles_escaped(text):
    assert text.safe_field("<@1> <#2> <@&3>") == "\\<@\u200b1\\> \\<\\#2\\> \\<@\u200b&3\\>"


def test_markdown_escaped(text):
    assert text.safe_field("*bold* _italic_ `code` [link](url) #title >quote") == "\\*bold\\* \\_italic\\_ \\`code\\` \\[link\\]\\(url\\) \\#title \\>quote"


def test_newlines_stripped(text):
    assert text.safe_field("one\ntwo\r\nthree\tend\x1b[31m") == "one two three end \\[31m"


def test_each_field_clipped(text):
    assert len(text.safe_field("x" * 10000, 70)) <= 70
    assert len(text.safe_field("*" * 10000, 35)) <= 35
    assert not text.safe_field("*" * 10000, 35).endswith("\\…")


def test_untrusted_links_rejected(text):
    for link in ("discord://-/channels/1/2", "https://evil.example/x", "https://discord.com/channels/1/2> <@everyone>"):
        data = sample_board()
        data["conversations"]["waiting"][0]["link"] = link
        assert link not in render(text, data)


def test_sixty_waiting_bounded_with_more(commands, home, text):
    for n in range(60):
        waiting(home, "wait" + str(n), "Long title " + "x" * 100)
    data = commands.board.build(float((home / "fixture-now").read_text(encoding="utf-8-sig")))
    result = render(text, data)
    assert len(result) <= 1900
    assert "+52 more" in result
    assert sum(line.startswith("- ") for line in result.splitlines()) == 8


def test_whole_message_budget_preserves_sections(text):
    data = sample_board()
    for key, items, _ in text.sections(data):
        item = items[0]
        expanded = [dict(item, title="*" * 80, goal="*" * 80, where="*" * 50) for _ in range(20)]
        if key in {"waiting", "running"}:
            data["conversations"][key] = expanded
        elif key == "dropped":
            data["delegations"]["dropped"] = expanded
        elif key == "cron":
            data["cron"]["problems"] = expanded
        else:
            data["goals"]["items"] = expanded
    result = render(text, data)
    assert len(result) <= 1900
    for heading in ("Waiting on you", "Running", "Finished, but not delivered", "Cron problems", "Standing goals"):
        assert heading in result
    assert "+" in result


def test_cli_parser_text_and_json(commands, home, capsys):
    waiting(home)
    settings(home, owner=OWNER)
    parser = argparse.ArgumentParser()
    commands.setup_cli(parser)
    commands.cli_handler(parser.parse_args([]))
    assert "OWNERTITLE" in capsys.readouterr().out
    commands.cli_handler(parser.parse_args(["--counts"]))
    assert "OWNERTITLE" not in capsys.readouterr().out
    commands.cli_handler(parser.parse_args(["--json"]))
    assert json.loads(capsys.readouterr().out)["plugin_version"] == "0.4.0"
    assert parser.parse_args(["--full", "--lang", "zh"]).lang == "zh"


def test_registration_survives_each_missing_api(commands):
    package = importlib.import_module(commands.__package__.split(".")[0])
    calls = []
    ctx = SimpleNamespace(register_command=lambda *a, **k: calls.append(("slash", a, k)), register_cli_command=lambda *a, **k: calls.append(("cli", a, k)))
    package.register(ctx)
    assert [call[0] for call in calls] == ["slash", "cli"]
    assert calls[0][1][0] == calls[1][1][0] == "duty"
    assert calls[0][2]["args_hint"] == "[full|counts] [lang]"
    assert not inspect.iscoroutinefunction(calls[0][1][1])
    calls.clear()
    del ctx.register_cli_command
    package.register(ctx)
    assert [call[0] for call in calls] == ["slash"]
    calls.clear()
    ctx = SimpleNamespace(register_cli_command=lambda *a, **k: calls.append("cli"))
    package.register(ctx)
    assert calls == ["cli"]


def test_error_has_only_exception_class(commands, home, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("PRIVATE_PATH_AND_CONTENT")
    monkeypatch.setattr(commands.board, "cached_summary", fail)
    result = commands.duty()
    assert result == "Duty Board unavailable (RuntimeError)."
    assert "PRIVATE" not in result
    commands.cli_handler(SimpleNamespace(lang=None, json=False, counts=False))


def test_summary_command_never_writes(commands, home):
    before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    commands.duty()
    after = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert before == after
    assert not (home / "duty-board" / "dismissed.json").exists()


def test_locale_priority(text, monkeypatch):
    monkeypatch.setattr(text, "choose_locale", text.choose_locale)
    assert text.choose_locale("zh_TW", {"chat_language": "ja"}) == "zh-hant"
    assert text.choose_locale(None, {"chat_language": "de-DE"}) == "de"
    assert text.locale_name("zh_Hant_HK") == "zh-hant"
    assert text.locale_name("zz") == "en"
    assert text.duration(7800, "en") == "2h 10m"
    assert not re.search(r"\{\w+\}", render(text, sample_board()))


@pytest.mark.parametrize("platform", ["telegram", "slack", "feishu", "cli"])
def test_other_surfaces_get_no_backslash_escapes(text, platform):
    """Telegram, Slack and Feishu adapters convert Markdown themselves, and a
    terminal shows text as is, so pre-escaped characters arrive as literal
    backslashes there. Only Discord gets backslash escapes; everywhere else
    links and mentions are still broken up."""
    data = sample_board()
    data["conversations"]["waiting"][0]["title"] = "self-improving #ops [x](https://e.vil) @everyone <@U1>"
    result = text.render_summary(data, locale="en", now=10000, viewer={"is_owner": True, "platform": platform, "link_style": "plain"})
    line = next(line for line in result.splitlines() if "improving" in line)
    assert "\\" not in line
    assert "self-improving #ops" in line
    assert "](" not in line
    assert "@everyone" not in line
    assert "<@U1>" not in line


def test_budget_trims_low_priority_sections_first(text):
    """When the message is over budget, what waits on the owner survives
    longest; older background results and goals give way first."""
    now = 100000
    link = "https://discord.com/channels/111/222/333"
    waiting = [{"title": f"WAIT{n}", "where": "place", "since": now - 60 * n, "link": link} for n in range(6)]
    dropped = [{"goal": f"DROPPED{n} " + "x" * 90, "session": {"where": "y" * 40, "link": link}, "completed_at": now - 3600} for n in range(17)]
    running = [{"title": f"RUN{n} " + "z" * 60, "where": "w" * 30, "started_at": now - 900, "link": link} for n in range(5)]
    data = {"conversations": {"waiting": waiting, "unanswered": [], "running": running, "done": []},
            "delegations": {"running": [], "dropped": dropped}, "cron": {"problems": []}, "goals": {"items": []}}
    result = render(text, data)
    assert len(result) <= 1900
    for n in range(6):
        assert f"WAIT{n}" in result
    assert "+" in result  # something lower down was trimmed instead


@pytest.mark.parametrize("payload", ["||SPOILER||", "**BOLD**", "`CODE`", "~~STRIKE~~", "__UNDER__", "*EM*", "_EM_"])
@pytest.mark.parametrize("platform", ["telegram", "slack", "feishu"])
def test_plain_fields_stay_inert_on_converting_surfaces(text, platform, payload):
    """Adapters that convert Markdown would turn delimiters in an untrusted
    title into formatting (a spoiler can hide text). Fields carry no ASCII
    Markdown delimiter there."""
    data = sample_board()
    data["conversations"]["waiting"][0]["title"] = "T " + payload
    result = text.render_summary(data, locale="en", now=10000, viewer={"is_owner": True, "platform": platform, "link_style": "plain"})
    line = next(line for line in result.splitlines() if line.startswith("- T "))
    for delimiter in ("||", "**", "`", "~~", "__", "*", "_"):
        assert delimiter not in line, (delimiter, line)
    assert payload.strip("|*`~_") in line


def test_refusal_never_touches_the_home(commands, home, monkeypatch):
    """Resolving the language must not create files. Hermes's language lookup
    reads config through a loader that creates the home's skeleton (SOUL.md,
    directories); a refused stranger must leave no trace."""
    settings(home, owner=OWNER)
    calls = []
    i18n = type(sys)("agent.i18n")
    i18n.get_language = lambda: calls.append("get_language") or "en"
    agent = type(sys)("agent")
    agent.i18n = i18n
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.i18n", i18n)
    caller(commands, monkeypatch, user="999")
    before = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert "only available" in commands.duty()
    assert sorted(str(p.relative_to(home)) for p in home.rglob("*")) == before
    assert calls == []


def test_language_comes_from_config_file_without_hermes_loader(commands, home, monkeypatch):
    settings(home, owner=OWNER)
    (home / "config.yaml").write_text("model: x\ndisplay:\n  language: ja\n  skin: default\n", encoding="utf-8")
    caller(commands, monkeypatch, user="999")
    assert commands.duty() == commands.message("ja", "ownerOnly")


def test_offline_guard_still_refuses_remote_hosts():
    import socket
    sock = socket.socket()
    try:
        with pytest.raises(AssertionError, match="network"):
            sock.connect(("192.0.2.1", 80))
        with pytest.raises(AssertionError, match="network"):
            socket.create_connection(("example.org", 443))
    finally:
        sock.close()


def test_offline_guard_refuses_loopback_outside_socketpair():
    """A product-code connect to a local service must still fail the suite."""
    import socket
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket()
    try:
        with pytest.raises(AssertionError, match="network"):
            client.connect(server.getsockname())
    finally:
        client.close()
        server.close()


@pytest.mark.skipif(not hasattr(__import__("socket"), "_fallback_socketpair"), reason="no TCP socketpair fallback")
def test_offline_guard_allows_the_windows_socketpair_fallback(monkeypatch):
    """Simulate Windows, where socketpair() is a loopback TCP connect."""
    import socket
    from .fixtures import offline
    offline.install(monkeypatch, socketpair=socket._fallback_socketpair)
    left, right = socket.socketpair()
    try:
        left.sendall(b"x")
        assert right.recv(1) == b"x"
    finally:
        left.close()
        right.close()
    probe = socket.socket()
    try:
        with pytest.raises(AssertionError, match="network"):
            probe.connect(("127.0.0.1", 9))  # the exemption ended with the call
    finally:
        probe.close()

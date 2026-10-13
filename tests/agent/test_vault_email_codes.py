"""Email verification codes for browser_vault_enter_code.

The code is read server-side from an inbox Hermes can already reach, extracted locally and typed
into the page; the model never sees the code or the email. Ambiguity always means "ask the user".
"""

from __future__ import annotations

import pytest

from agent.vault_email_codes import EmailMessage, extract_code, pick_code, sender_matches_site


class TestExtractCode:
    def test_single_numeric_code_in_body(self):
        body = "Hi there,\n\nYour verification code is 482913. It expires in 10 minutes.\n\nThanks, Acme"
        assert extract_code("Verify your sign-in", body) == "482913"

    def test_code_repeated_in_subject_and_body_is_one_candidate(self):
        assert extract_code("482913 is your Acme code", "Enter 482913 to finish signing in. Your code expires soon.") == "482913"

    def test_grouped_digits_are_joined(self):
        assert extract_code("Sign in", "Your one-time code: 482 913") == "482913"
        assert extract_code("Sign in", "Your one-time code: 482-913") == "482913"

    def test_two_different_codes_are_ambiguous(self):
        assert extract_code("Your codes", "Verification code: 482913\nBackup code: 771204") is None

    def test_digits_without_a_code_label_are_ignored(self):
        assert extract_code("Your order shipped", "Order 482913 is on its way. Tracking 99120034.") is None

    def test_alphanumeric_code_only_when_directly_labelled(self):
        assert extract_code("Sign in", "Your one-time code is K7P2QX. Do not share it.") == "K7P2QX"
        assert extract_code("Verify your account", "Please verify your account. Reference AB12CD for support.") is None

    def test_html_body_ignores_urls_styles_and_markup(self):
        html = (
            '<html><head><style>.c{width:600px;font-size:2400px}</style></head><body>'
            '<table width="600"><tr><td>Your verification code</td></tr>'
            '<tr><td style="font-size:24px;letter-spacing:4px">739104</td></tr>'
            '<tr><td><a href="https://acme.test/verify?token=123456&code=654321">Or click here</a></td></tr>'
            '</table></body></html>'
        )
        assert extract_code("Acme sign-in", html) == "739104"

    def test_dates_times_and_short_numbers_are_not_codes(self):
        body = "Requested on 10/11/2026 at 13:46 from Toronto. Your code is 5521. It expires in 10 minutes."
        assert extract_code("Sign-in code", body) == "5521"

    def test_a_footer_year_does_not_make_a_real_code_ambiguous(self):
        body = "Your code is 482913.\n\n© 2026 Acme Inc. Didn't request a code? Ignore this email."
        assert extract_code("Acme", body) == "482913"
        assert extract_code("Acme", "Your code is 2026. Didn't request a code? Ignore this email.") == "2026"

    def test_nothing_codelike_returns_none(self):
        assert extract_code("Welcome", "Thanks for joining Acme!") is None


NOW = 1_800_000_000.0


def _msg(sender, body, *, age=30.0, subject="Your sign-in code"):
    return EmailMessage(sender=sender, subject=subject, received_at=NOW - age, body=body)


class TestSiteMatch:
    def test_sender_on_the_site_domain_or_a_subdomain_matches(self):
        assert sender_matches_site("Acme <no-reply@acme.test>", "", "accounts.acme.test",
                                   sender_aliases={"acme.test": ["acme.test"]})
        assert sender_matches_site("no-reply@mail.acme.test", "", "acme.test")
        assert sender_matches_site("Bank <alerts@mybank.co.uk>", "", "login.mybank.co.uk",
                                   sender_aliases={"login.mybank.co.uk": ["mybank.co.uk"]})

    def test_cross_tld_sender_requires_an_explicit_alias(self):
        aliases = {"stubhub.ca": ["stubhub.com"], "bestbuy.ca": ["bestbuy.com"]}
        assert not sender_matches_site("noreply@stubhub.com", "", "www.stubhub.ca")
        assert sender_matches_site("noreply@stubhub.com", "", "www.stubhub.ca", sender_aliases=aliases)
        assert sender_matches_site("noreply@mail.bestbuy.com", "", "www.bestbuy.ca", sender_aliases=aliases)

    def test_unconfigured_cross_tld_senders_do_not_match(self):
        # Only the site's own name, not a squashed or prefixed variant, and not the free-text login label.
        assert not sender_matches_site("x <noreply@stub-hub.com>", "Your code", "www.stubhub.ca")
        assert not sender_matches_site("x <noreply@stubhubs.com>", "Your code", "www.stubhub.ca")
        assert not sender_matches_site("x <noreply@stubhub-login.com>", "Your code", "www.stubhub.ca")
        assert not sender_matches_site("x <noreply@stubhub.ca.evil.com>", "Your code", "www.stubhub.ca")
        assert not sender_matches_site("GoDaddy <noreply@godaddy.com>", "Your code", "sso.example.com", site_label="GoDaddy")
        # No TLD establishes ownership without explicit configuration.
        for tld in ("xyz", "top", "co", "cm", "tk", "test2"):
            assert not sender_matches_site(f"x <noreply@acme.{tld}>", "Your code", "www.acme.com"), tld
            assert not sender_matches_site("x <noreply@acme.com>", "Your code", f"www.acme.{tld}"), tld
        # Short names do not establish trust either.
        assert not sender_matches_site("x <noreply@abc.com>", "Your code", "www.abc.ca")

    def test_lookalike_domains_do_not_match(self):
        assert not sender_matches_site("x <no-reply@notacme.test>", "Hello", "acme.test")
        assert not sender_matches_site("x <no-reply@acme.test.evil.com>", "Hello", "acme.test")
        assert not sender_matches_site("x <alerts@other.co.uk>", "Hello", "login.mybank.co.uk")

    def test_dedicated_sender_domains_need_aliases_not_display_names_or_subjects(self):
        """Display names and subjects are free to spoof; a code from an impostor typed into the real page
        could authorise the impostor's device. Only the sender's domain counts."""
        assert sender_matches_site("Shopify <mailer@shopifyemail.com>", "", "accounts.shopify.com",
                                   sender_aliases={"shopify.com": ["shopifyemail.com"]})
        assert sender_matches_site("Facebook <security@facebookmail.com>", "", "www.facebook.com",
                                   sender_aliases={"facebook.com": ["facebookmail.com"]})
        assert not sender_matches_site("mailer@sendgrid.net", "Your Acme verification code", "acme.test")
        assert not sender_matches_site("Acme Security <alerts@evil.test>", "Your code", "acme.test")
        assert not sender_matches_site("GoDaddy <noreply@secureserver.net>", "Your code", "sso.example.com", site_label="GoDaddy Login")

    def test_wrong_sender_does_not_match(self):
        assert not sender_matches_site("Other Co <no-reply@other.test>", "Your verification code", "acme.test")

    def test_brand_prefixed_domains_anyone_can_register_do_not_match(self):
        """Unconfigured domains merely starting with the brand are attacker-registrable."""
        assert not sender_matches_site("x <no-reply@acme-login.test>", "Your code", "acme.test")
        assert not sender_matches_site("x <no-reply@acmesecure.test>", "Your code", "acme.test")
        assert not sender_matches_site("x <no-reply@acmemailer-verify.test>", "Your code", "acme.test")
        assert not sender_matches_site("noreply@mail.godaddy-sso.com", "Your code", "sso.example.com", site_label="GoDaddy")


class TestPickCode:
    def test_single_code_from_the_site(self):
        msgs = [_msg("Acme <no-reply@acme.test>", "Your verification code is 482913.")]
        assert pick_code(msgs, "accounts.acme.test", since=NOW - 600,
                         sender_aliases={"accounts.acme.test": ["acme.test"]}) == "482913"

    def test_newest_matching_message_wins(self):
        msgs = [_msg("no-reply@acme.test", "Your code is 111111.", age=200),
                _msg("no-reply@acme.test", "Your code is 222222.", age=20),
                _msg("no-reply@other.test", "Your code is 333333.", age=5)]
        assert pick_code(msgs, "acme.test", since=NOW - 600) == "222222"

    def test_wrong_sender_only_gives_nothing(self):
        msgs = [_msg("Other <no-reply@other.test>", "Your verification code is 482913.")]
        assert pick_code(msgs, "acme.test", since=NOW - 600) is None

    def test_stale_message_is_ignored(self):
        msgs = [_msg("no-reply@acme.test", "Your verification code is 482913.", age=900)]
        assert pick_code(msgs, "acme.test", since=NOW - 600) is None

    def test_ambiguous_newest_message_does_not_fall_back_to_an_older_one(self):
        msgs = [_msg("no-reply@acme.test", "Your code is 111111.", age=200),
                _msg("no-reply@acme.test", "Code: 222222. Backup code: 333333.", age=20)]
        assert pick_code(msgs, "acme.test", since=NOW - 600) is None


# ---------------------------------------------------------------------------
# Inbox lanes: read-only, bodies stay in process
# ---------------------------------------------------------------------------

import base64
import json


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class FakeGws:
    """A gws-compatible CLI: ``<command> gmail users messages list|get --params JSON --format json``."""

    def __init__(self, messages):
        self.messages = messages  # id -> dict(sender, subject, received_at, html|text)
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        params = json.loads(argv[argv.index("--params") + 1])
        method = argv[argv.index("messages") + 1]
        if method == "list":
            after = int(params["q"].split("after:")[1].split()[0])
            ids = [i for i, m in sorted(self.messages.items(), key=lambda kv: -kv[1]["received_at"]) if m["received_at"] >= after]
            return json.dumps({"messages": [{"id": i, "threadId": i} for i in ids]} if ids else {"resultSizeEstimate": 0})
        m = self.messages[params["id"]]
        headers = [{"name": "From", "value": m["sender"]}, {"name": "Subject", "value": m["subject"]}]
        out = {"id": params["id"], "internalDate": str(int(m["received_at"] * 1000)), "payload": {"mimeType": "multipart/alternative", "headers": headers}}
        if params["format"] == "full":
            out["payload"]["parts"] = [
                {"mimeType": "text/plain", "body": {"data": _b64(m.get("text", ""))}},
                {"mimeType": "text/html", "body": {"data": _b64(m.get("html", ""))}},
            ]
        return json.dumps(out)


class TestGmailLane:
    def test_reads_only_matching_bodies_with_list_and_get(self):
        from agent.vault_email_codes import GmailLane

        gws = FakeGws({
            "a": {"sender": "Acme <no-reply@acme.test>", "subject": "Sign in", "received_at": NOW - 20, "text": "Your code is 482913."},
            "b": {"sender": "news@other.test", "subject": "Newsletter", "received_at": NOW - 10, "text": "Promo code 777777"},
        })
        lane = GmailLane(["gws-cli", "workspace", "--account", "work"], run=gws)
        msgs = lane.recent(NOW - 600, lambda sender, subject: "acme.test" in sender)
        assert [(m.sender, m.received_at) for m in msgs] == [("Acme <no-reply@acme.test>", NOW - 20)]
        assert "482913" in msgs[0].body
        verbs = {(c[c.index("messages") + 1], json.loads(c[c.index("--params") + 1]).get("format")) for c in gws.calls}
        assert verbs <= {("list", None), ("get", "metadata"), ("get", "full")}
        full_ids = [json.loads(c[c.index("--params") + 1])["id"] for c in gws.calls if '"full"' in c[c.index("--params") + 1]]
        assert full_ids == ["a"]  # the newsletter body was never fetched
        assert all(c[:4] == ["gws-cli", "workspace", "--account", "work"] for c in gws.calls)


class FakeImap:
    """imaplib.IMAP4_SSL stand-in that refuses anything but a read-only session."""

    def __init__(self, messages):
        self.messages = messages  # uid(bytes) -> (internaldate epoch, rfc822 bytes)
        self.calls: list[tuple] = []
        self.logged_in = None

    def login(self, user, password):
        self.logged_in = (user, bool(password))
        return "OK", [b"ok"]

    def select(self, mailbox="INBOX", readonly=False):
        self.calls.append(("select", mailbox, readonly))
        assert readonly is True
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        command = command.upper()
        assert command in {"SEARCH", "FETCH"}, command
        if command == "SEARCH":
            return "OK", [b" ".join(sorted(self.messages))]
        uid, spec = args
        assert "PEEK" in spec or spec == "(INTERNALDATE)" or "BODY[" not in spec
        when, raw = self.messages[uid.encode() if isinstance(uid, str) else uid]
        import imaplib
        import time as _t
        date = imaplib.Time2Internaldate(_t.gmtime(when)).encode()
        if "HEADER.FIELDS" in spec:
            head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            return "OK", [(b"1 (UID " + uid.encode() + b" INTERNALDATE " + date + b" BODY[HEADER.FIELDS (FROM SUBJECT)] {%d}" % len(head), head), b")"]
        return "OK", [(b"1 (UID " + uid.encode() + b" BODY[] {%d}" % len(raw), raw), b")"]

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]


def _rfc822(sender, subject, body):
    return (f"From: {sender}\r\nSubject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n").encode()


class TestImapLane:
    def test_examines_read_only_and_peeks_only_matching_bodies(self):
        from agent.vault_email_codes import ImapLane

        fake = FakeImap({
            b"7": (NOW - 900, _rfc822("no-reply@acme.test", "Old", "Your code is 111111.")),
            b"8": (NOW - 30, _rfc822("Acme <no-reply@acme.test>", "Sign in", "Your code is 482913.")),
            b"9": (NOW - 10, _rfc822("news@other.test", "Promo", "Promo code 777777")),
        })
        resolved = []
        lane = ImapLane("imap.example.test", 993, "me@example.test", lambda: resolved.append(1) or "app-pw",
                        connect=lambda host, port: fake)
        msgs = lane.recent(NOW - 600, lambda sender, subject: "acme.test" in sender)
        lane.close()
        assert [m.subject for m in msgs] == ["Sign in"] and "482913" in msgs[0].body
        assert fake.logged_in == ("me@example.test", True) and resolved == [1]
        full = [c for c in fake.calls if c[0] == "uid" and c[1] == "FETCH" and "HEADER" not in c[3]]
        assert [c[2] for c in full] == ["8"] and all("BODY.PEEK[]" in c[3] for c in full)
        assert ("logout",) in fake.calls


# ---------------------------------------------------------------------------
# Config → inbox, and the bounded wait
# ---------------------------------------------------------------------------

CFG = {
    "enabled": True,
    "sender_aliases": {"accounts.acme.test": ["acme.test"]},
    "wait_seconds": 60,
    "poll_interval_seconds": 5,
    "lookback_seconds": 600,
    "inboxes": [
        {"addresses": ["me@work.example", "me@alias.example"], "gmail_command": ["/usr/local/bin/gws", "--account", "work"]},
        {"addresses": ["me@icloud.example", "other@icloud.example"],
         "imap": {"host": "imap.mail.me.com", "port": 993, "username": "me@icloud.example", "password_ref": "op://v/i/credential"}},
    ],
}


class TestLaneForIdentifier:
    def test_identifier_picks_its_inbox_case_and_plus_tag_insensitive(self):
        from agent.vault_email_codes import GmailLane, ImapLane, lane_for_identifier

        lane = lane_for_identifier("Me+shop@Alias.Example", CFG)
        assert isinstance(lane, GmailLane) and lane.command[-1] == "work"
        refs = []
        imap = lane_for_identifier("other@icloud.example", CFG, resolve_ref=lambda ref: refs.append(ref) or "pw")
        assert isinstance(imap, ImapLane) and imap.username == "me@icloud.example" and refs == []  # resolved lazily
        assert imap._password() == "pw" and refs == ["op://v/i/credential"]

    def test_unknown_identifier_username_or_disabled_has_no_lane(self):
        from agent.vault_email_codes import lane_for_identifier

        assert lane_for_identifier("someone@else.test", CFG) is None
        assert lane_for_identifier("meuser", CFG) is None
        assert lane_for_identifier("", CFG) is None
        assert lane_for_identifier("me@work.example", {**CFG, "enabled": False}) is None
        assert lane_for_identifier("me@work.example", {}) is None


class ScriptedLane:
    def __init__(self, polls):
        self.polls = list(polls)
        self.seen_since = []
        self.closed = False

    def recent(self, since, matches):
        self.seen_since.append(since)
        batch = self.polls.pop(0) if self.polls else []
        if isinstance(batch, Exception):
            raise batch
        return [m for m in batch if matches(m.sender, m.subject)]

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = NOW
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _wait(lane, clock, site="accounts.acme.test", **kw):
    from agent.vault_email_codes import wait_for_email_code

    return wait_for_email_code(site, "me@work.example", cfg=CFG, clock=clock, sleep=clock.sleep,
                               lane_factory=lambda identifier, cfg: lane, **kw)


class TestWaitForEmailCode:
    def test_polls_until_the_code_arrives(self):
        clock = Clock()
        arrived = EmailMessage("Acme <no-reply@acme.test>", "Sign in", NOW + 9, "Your verification code is 482913.", key="gmail:x1")
        lane = ScriptedLane([[], [], [arrived]])
        assert _wait(lane, clock) == "482913"
        assert clock.sleeps == [5, 5] and lane.closed
        assert lane.seen_since[0] == NOW - 600

    def test_gives_up_at_the_deadline(self):
        clock = Clock()
        lane = ScriptedLane([])
        assert _wait(lane, clock) is None
        assert sum(clock.sleeps) <= 60 and lane.closed

    def test_ambiguous_newest_message_stops_waiting_and_returns_nothing(self):
        clock = Clock()
        lane = ScriptedLane([[EmailMessage("no-reply@acme.test", "Sign in", NOW - 5, "Code 111111 or code 222222", key="gmail:x2")]])
        assert _wait(lane, clock) is None and clock.sleeps == []

    def test_a_code_already_entered_is_not_entered_again(self):
        msg = EmailMessage("no-reply@acme.test", "Sign in", NOW - 5, "Your code is 482913.", key="gmail:used-once")
        assert _wait(ScriptedLane([[msg]]), Clock()) == "482913"
        assert _wait(ScriptedLane([[msg]] * 20), Clock()) is None

    def test_lane_failure_falls_back_quietly(self, caplog):
        import logging

        caplog.set_level(logging.DEBUG)
        lane = ScriptedLane([RuntimeError("Your code is 482913 (leaked body)")])
        assert _wait(lane, Clock()) is None and lane.closed
        assert "482913" not in caplog.text

    def test_no_lane_for_the_identifier_means_no_wait(self):
        from agent.vault_email_codes import wait_for_email_code

        clock = Clock()
        assert wait_for_email_code("acme.test", "someone@else.test", cfg=CFG, clock=clock, sleep=clock.sleep) is None
        assert clock.sleeps == []

    def test_two_concurrent_waits_never_both_enter_the_same_code(self):
        """The at-most-once guard reserves a message under the same lock that checks it."""
        import threading

        msg = EmailMessage("no-reply@acme.test", "Sign in", NOW - 5, "Your code is 593172.", key="gmail:race-once")
        barrier = threading.Barrier(2)

        class RacingLane(ScriptedLane):
            def recent(self, since, matches):
                barrier.wait(timeout=5)  # both sessions have read the inbox before either reserves
                return [msg]

        results = []
        threads = [threading.Thread(target=lambda: results.append(_wait(RacingLane([]), Clock()))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert sorted(results, key=str) == ["593172", None]


class TestMailCliChildEnv:
    """The mail CLI acts for the served profile: its env comes from served_profile_child_env, never the
    launch profile's os.environ (one process serves many profiles)."""

    def test_child_env_is_the_served_profile_env(self, monkeypatch):
        import subprocess

        from agent import vault_email_codes

        served = {"HERMES_HOME": "/srv/profiles/b", "PATH": "/usr/bin"}
        calls = []
        monkeypatch.setattr("tools.environments.local.served_profile_child_env",
                            lambda **kw: (calls.append(kw), dict(served))[1])

        def fake_run(argv, **kw):
            calls.append(kw.get("env"))
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

        monkeypatch.setattr(vault_email_codes.subprocess, "run", fake_run)
        assert vault_email_codes._run_cli(["mailcli", "gmail"]) == "{}"
        assert calls == [{"inherit_credentials": True}, served]

    def test_output_is_decoded_as_utf8_not_the_locale_codepage(self, monkeypatch):
        """A real child writing UTF-8 plus one invalid byte: decoded as UTF-8 with replacement, never via
        locale.getpreferredencoding() (cp1252/cp936 on Windows) and never a UnicodeDecodeError."""
        import os
        import sys

        from agent import vault_email_codes

        monkeypatch.setattr("tools.environments.local.served_profile_child_env", lambda **kw: dict(os.environ))
        script = 'import sys; sys.stdout.buffer.write(\'{"s": "caf\\u00e9 \\u2713"}\'.encode("utf-8") + b"\\xff")'
        out = vault_email_codes._run_cli([sys.executable, "-c", script])
        assert out == '{"s": "caf\u00e9 \u2713"}\ufffd'

    def test_unscoped_child_env_fails_closed_before_spawning(self, monkeypatch):
        from agent import vault_email_codes

        def unscoped(**kw):
            raise RuntimeError("no profile scope bound")

        spawned = []
        monkeypatch.setattr("tools.environments.local.served_profile_child_env", unscoped)
        monkeypatch.setattr(vault_email_codes.subprocess, "run", lambda *a, **k: spawned.append(a))
        with pytest.raises(RuntimeError):
            vault_email_codes._run_cli(["mailcli", "gmail"])
        assert spawned == []

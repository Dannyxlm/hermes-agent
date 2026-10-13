"""Explicit sender trust: no brand, public-suffix or cross-TLD ownership guesses."""

import json

import pytest

from agent.vault_email_codes import EmailMessage, sender_matches_site, wait_for_email_code


@pytest.mark.parametrize("site,sender", [
    *[("acme.com", f"acme.{tld}") for tld in ("io", "ai", "me", "cc", "ws")],
    ("stubhub.ca", "stubhub.io"), ("bestbuy.ca", "bestbuy.ly"), ("paypal.com", "paypal.gg"),
    ("stubhub.ca", "stubhub.com"), ("facebook.com", "facebookmail.com"),
    ("shopify.com", "shopifyemail.com"), ("example.com", "godaddymail.com"),
    ("alice.github.io", "bob.github.io"), ("alice.co.za", "bob.co.za"),
])
def test_no_inferred_ownership(site, sender):
    assert not sender_matches_site(f"Site <code@{sender}>", "Your code", site, "GoDaddy")


def test_alias_is_directional_and_scoped_to_dns_boundaries():
    aliases = {"stubhub.ca": ["stubhub.com"], "login.example.test": ["codes.mail.test"]}
    def matches(sender, site):
        return sender_matches_site(f"code@{sender}", "", site, sender_aliases=aliases)

    assert matches("stubhub.com", "www.stubhub.ca")
    assert matches("alerts.stubhub.com", "accounts.stubhub.ca")
    assert matches("codes.mail.test", "login.example.test")
    assert matches("a.codes.mail.test", "login.example.test")
    for sender in ("stubhub.io", "notstubhub.com", "stubhub.com.evil.test", "stub-hub.com"):
        assert not matches(sender, "www.stubhub.ca")
    for site in ("notstubhub.ca", "stubhub.ca.evil.test", "stubhub.io"):
        assert not matches("stubhub.com", site)
    assert not matches("stubhub.ca", "stubhub.com")  # no reverse trust
    assert not matches("codes.mail.test", "example.test")  # no parent or sibling widening
    assert not matches("codes.mail.test", "other.example.test")
    assert not matches("mail.test", "login.example.test")
    assert not sender_matches_site("code@c.test", "", "a.test", sender_aliases={
        "a.test": ["b.test"], "b.test": ["c.test"]})


@pytest.mark.parametrize("sender", ["example.test", "@example.test", "x@exampKe.test",
                                    "x@example.test.evil.test", "x@notexample.test"])
def test_sender_requires_a_valid_ascii_mailbox_domain(sender):
    assert not sender_matches_site(sender, "", "example.test")
    assert not sender_matches_site(sender, "", "exampke.test")


def test_case_normalization_and_empty_rules():
    assert sender_matches_site("code@MAIL.SENDER.TEST", "", "LOGIN.EXAMPLE.TEST",
                               sender_aliases={"EXAMPLE.TEST": ["SENDER.TEST"]})
    assert not sender_matches_site("code@sender.test", "", "example.test",
                                   sender_aliases={"example.test": []})
    assert not sender_matches_site("code@sender.test", "", "example.test",
                                   sender_aliases={"EXAMPLE.TEST": ["sender.test"], "example.test": []})


def test_default_trust_is_exact_host_and_descendants_not_guessed_parents():
    assert sender_matches_site("code@EXAMPLE.TEST", "", "example.test")
    assert sender_matches_site("code@mail.example.test", "", "EXAMPLE.TEST")
    assert not sender_matches_site("code@example.test", "", "login.example.test")
    assert sender_matches_site("code@example.test", "", "login.example.test",
                               sender_aliases={"login.example.test": ["example.test"]})
    assert not sender_matches_site("code@evil.test", "example.test code", "example.test", "Evil")


INVALID_DOMAINS = [
    "", "*", "*.example.test", ".example.test", "example.test.", " example.test", "example.test ",
    "example..test", "https://example.test", "example.test/path", "example.test:443", "x@example.test",
    "Example Login", "localhost", "com", "127.0.0.1", "[::1]", "-bad.test", "bad-.test",
    "bad_name.test", "exämple.test", "a" * 64 + ".test", ".".join(["a" * 63] * 4),
    None, 42, True,
]


@pytest.mark.parametrize("value", INVALID_DOMAINS)
def test_invalid_alias_domains_fail_closed(value):
    # One bad rule invalidates the entire map, including otherwise valid rules.
    for aliases in ({"example.test": [value, "sender.test"]},
                    {"example.test": ["sender.test"], value: ["sender.test"]}):
        assert not sender_matches_site("code@sender.test", "", "example.test", sender_aliases=aliases)


@pytest.mark.parametrize("aliases", [None, [], "sender.test", True, 1,
                                      {"example.test": "sender.test"},
                                      {"example.test": None}, {"example.test": {}},
                                      {"example.test": ["sender.test", 7]}])
def test_malformed_alias_config_stops_before_inbox_access(aliases, caplog):
    opened = []
    cfg = {"enabled": True, "sender_aliases": aliases}
    assert wait_for_email_code("example.test", "person@example.test", cfg=cfg,
                               lane_factory=lambda *_: opened.append(True)) is None
    assert opened == []
    assert "sender.test" not in caplog.text


@pytest.mark.parametrize("host", INVALID_DOMAINS)
def test_invalid_site_host_fails_closed(host):
    assert not sender_matches_site("code@example.test", "", host)


class Inbox:
    def __init__(self, messages):
        self.messages = messages
        self.body_reads = []
        self.closed = False

    def recent(self, since, matches):
        accepted = [m for m in self.messages if m.received_at >= since and matches(m.sender, m.subject)]
        self.body_reads.extend(m.sender for m in accepted)
        return accepted

    def close(self):
        self.closed = True


def test_config_set_runtime_loader_and_legacy_migration(tmp_path, monkeypatch, capsys):
    from agent.vault_email_codes import email_codes_config
    from hermes_cli.config import _validate_config_key, set_config_value
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert DEFAULT_CONFIG["vault"]["email_codes"]["sender_aliases"] == {}
    assert _validate_config_key("vault.email_codes.sender_aliases") == (True, None)
    legacy = {"enabled": True, "wait_seconds": 0, "inboxes": [
        {"addresses": ["person@example.test"], "gmail_command": ["synthetic-mail"]}]}
    set_config_value("vault.email_codes", json.dumps(legacy))
    original = (tmp_path / "config.yaml").read_bytes()
    cfg = email_codes_config()
    assert cfg["enabled"] is True and cfg["inboxes"] == legacy["inboxes"]
    assert cfg.get("sender_aliases", {}) == {}
    assert (tmp_path / "config.yaml").read_bytes() == original  # loading never edits a legacy file

    own = EmailMessage("code@stubhub.ca", "Code", 99, "Your code is 111111.")
    alias = EmailMessage("code@stubhub.com", "Code", 100, "Your code is 222222.")
    impostor = EmailMessage("code@stubhub.io", "Code", 101, "Your code is 333333.")
    def run(site, messages):
        lane = Inbox(messages)
        code = wait_for_email_code(site, "person@example.test", clock=lambda: 102,
                                   lane_factory=lambda *_: lane)
        assert lane.closed
        return code, lane.body_reads

    assert run("stubhub.ca", [own, alias, impostor]) == ("111111", [own.sender])
    set_config_value("vault.email_codes.sender_aliases", json.dumps({"stubhub.ca": ["stubhub.com"]}))
    assert email_codes_config()["sender_aliases"] == {"stubhub.ca": ["stubhub.com"]}
    assert run("www.stubhub.ca", [alias, impostor]) == ("222222", [alias.sender])
    assert run("unrelated.test", [alias]) == (None, [])
    ambiguous = EmailMessage(alias.sender, "Code", 102, "Code: 444444. Backup code: 555555.")
    assert run("www.stubhub.ca", [alias, ambiguous])[0] is None
    out = capsys.readouterr()
    assert "not a recognized config key" not in out.out + out.err

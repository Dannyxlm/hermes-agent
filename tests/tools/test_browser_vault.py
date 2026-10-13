"""Tests for the vault-backed password-blind browser autofill feature.

Covers:
- VaultStore: encrypt/decrypt round-trip, file perms, identifier-as-metadata
  (login secret payload is password-only)
- login-control classifier: scoring + new-password/one-time-code exclusion,
  password-only fill selection
- origin-binding refusal (pre-check + in-script TOCTOU assert)
- fail-closed secret eval (no argv fallback)
- vault-value redaction registry (browser_cdp read-back regression)
- tool gating: check_fn False when the vault is empty
"""

from __future__ import annotations

import json
import re
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.vault_login_classifier import (
    ClassifiedLoginControl,
    LoginControl,
    build_fill_js,
    classify_login_control,
    select_password_fill,
)
from agent.vault_store import (
    VaultError,
    VaultStore,
    normalize_origin,
    scrub_secret_from_text,
)


@pytest.fixture()
def store(tmp_path):
    return VaultStore(base_dir=tmp_path / "vault")


def _add_login(store, origin="https://example.com", password="s3cret-pw"):
    return store.add_item(
        kind="login",
        label="Example login",
        origin=origin,
        secret={
            "identifier_type": "email",
            "identifier": "user@example.com",
            "password": password,
            "origin": origin,
        },
    )


# ---------------------------------------------------------------------------
# VaultStore
# ---------------------------------------------------------------------------

class TestVaultStore:
    def test_roundtrip_encrypt_decrypt(self, store):
        meta = _add_login(store)
        secret = store.resolve_secret(meta.id)
        # Design: login secret payload is password-only; identifier is metadata.
        assert secret == {"password": "s3cret-pw"}
        assert meta.identifier == "user@example.com"
        assert meta.identifier_type == "email"

    def test_vault_file_never_contains_password(self, store, tmp_path):
        _add_login(store)
        blob = (tmp_path / "vault" / "vault.json.enc").read_bytes()
        assert b"s3cret-pw" not in blob

    def test_file_permissions_0600(self, store, tmp_path):
        _add_login(store)
        for name in ("vault.json.enc", "vault.key"):
            mode = stat.S_IMODE(os.stat(tmp_path / "vault" / name).st_mode)
            assert mode == 0o600, f"{name} has mode {oct(mode)}"

    def test_listing_is_password_free(self, store):
        meta = _add_login(store)
        items = store.list_items()
        assert len(items) == 1
        dumped = json.dumps(items[0].to_dict())
        assert "s3cret-pw" not in dumped
        assert "password" not in dumped
        # Identifier IS visible metadata now.
        assert items[0].identifier == "user@example.com"
        assert items[0].identifier_type == "email"
        assert items[0].id == meta.id
        assert items[0].origin == "https://example.com"

    def test_remove_item(self, store):
        meta = _add_login(store)
        assert store.remove_item(meta.id) is True
        assert store.remove_item(meta.id) is False
        assert store.list_items() == []

    def test_login_requires_origin(self, store):
        with pytest.raises(VaultError):
            store.add_item(
                kind="login",
                label="x",
                secret={
                    "identifier_type": "email",
                    "identifier": "a@b.c",
                    "password": "p",
                },
            )

    def test_all_kinds_supported(self, store):
        store.add_item(kind="payment", label="Card", origin="https://shop.test", secret=_CARD)
        store.add_item(kind="address", label="Home", secret=_ADDRESS)
        kinds = {m.kind for m in store.list_items()}
        assert kinds == {"payment", "address"}

    def test_checkout_kinds_keep_only_canonical_fields(self, store):
        """The fill maps canonical names → autocomplete tokens; a stray ad-hoc key would be stored (secret!)
        yet unfillable, and a missing required field would make the item dead on every checkout."""
        meta = store.add_item(kind="payment", label="Card", secret={**_CARD, "note": "personal"})
        assert "note" not in store.resolve_secret(meta.id)
        with pytest.raises(VaultError, match="cvc"):
            store.add_item(kind="payment", label="Card", secret={k: v for k, v in _CARD.items() if k != "cvc"})

    def test_unknown_kind_rejected(self, store):
        with pytest.raises(VaultError):
            store.add_item(kind="totp", label="x", secret={})

    def test_has_items(self, store):
        assert store.has_items() is False
        _add_login(store)
        assert store.has_items() is True

    def test_normalize_origin(self):
        assert normalize_origin("https://Example.com:443/login?x=1") == "https://example.com"
        assert normalize_origin("http://localhost:8931/") == "http://localhost:8931"
        assert normalize_origin("http://site.test:80") == "http://site.test"
        with pytest.raises(VaultError):
            normalize_origin("example.com")

    def test_scrub_secret_from_text(self):
        secret = {"password": "hunter22x", "identifier": "me@x.io"}
        out = scrub_secret_from_text("boom hunter22x at me@x.io", secret)
        assert "hunter22x" not in out
        assert "me@x.io" not in out


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

_CARD = {"card_number": "4111111111111111", "cardholder_name": "A User", "exp_month": "7", "exp_year": "2029",
         "cvc": "123", "billing_postal_code": "94110"}
_ADDRESS = {"address_line1": "1 Main St", "city": "Springfield", "postal_code": "12345", "country": "US"}


def _ctrl(**kw):
    base = dict(autocomplete="", form_index=0, index=0, label="", name="", type="text")
    base.update(kw)
    return LoginControl(**base)


class TestClassifier:
    def test_autocomplete_exact_match_maps_token(self):
        for token in ("username", "email", "tel", "current-password"):
            res = classify_login_control(_ctrl(autocomplete=token))
            assert res is not None and res.token == token

    def test_new_password_autocomplete_excluded(self):
        assert classify_login_control(
            _ctrl(autocomplete="new-password", type="password")
        ) is None

    def test_one_time_code_excluded(self):
        assert classify_login_control(_ctrl(autocomplete="one-time-code")) is None

    def test_label_new_password_excluded(self):
        for label in ("New password", "Confirm Password", "create-password", "Repeat  password"):
            assert classify_login_control(_ctrl(type="password", label=label)) is None, label

    def test_password_type_maps_current_password(self):
        res = classify_login_control(_ctrl(type="password"))
        assert res.token == "current-password"

    def test_email_tel_types_map_tokens(self):
        assert classify_login_control(_ctrl(type="email")).token == "email"
        res = classify_login_control(_ctrl(type="tel"))
        assert res.token == "tel"

    def test_label_heuristics(self):
        assert classify_login_control(_ctrl(label="E-mail address")).token == "email"
        assert classify_login_control(_ctrl(name="mobile_number")).token == "tel"
        res = classify_login_control(_ctrl(label="Username or account"))
        assert res.token == "username"

    def test_unmatched_returns_none(self):
        assert classify_login_control(_ctrl(label="Search the docs")) is None

    def test_select_password_fill_picks_best_password(self):
        user = ClassifiedLoginControl(_ctrl(index=0, form_index=0, autocomplete="username"), 100, "username")
        pw_heur = ClassifiedLoginControl(_ctrl(index=1, form_index=0, type="password"), 90, "current-password")
        pw_exact = ClassifiedLoginControl(_ctrl(index=3, form_index=0, autocomplete="current-password"), 100, "current-password")
        fills = select_password_fill([user, pw_heur, pw_exact], "p")
        # Password only — the identifier field is never filled by the vault.
        assert [(f["index"], f["token"]) for f in fills] == [(3, "current-password")]

    def test_select_password_fill_requires_password_field(self):
        user = ClassifiedLoginControl(_ctrl(index=0, autocomplete="username"), 100, "username")
        assert select_password_fill([user], "p") == []

    def test_select_password_fill_single_field_only(self):
        pw1 = ClassifiedLoginControl(_ctrl(index=1, type="password"), 90, "current-password")
        pw2 = ClassifiedLoginControl(_ctrl(index=2, type="password"), 90, "current-password")
        fills = select_password_fill([pw1, pw2], "p")
        assert len(fills) == 1 and fills[0]["index"] == 1


    def test_build_fill_js_leaves_no_dom_marker_and_binds_target_to_inspection(self):
        # P1-1: no persistent selector for filled controls. The fill targets the input by the
        # <nonce>:<index> stamp ITS OWN inspection wrote (a bare index is re-resolved by position,
        # and a second inspection in between would re-stamp — either way the password could land in
        # another field); stamps carry no secret and the fill script strips every one before returning.
        js = build_fill_js(
            [{"index": 0, "token": "current-password", "value": "x"}],
            expected_origin="https://example.com",
        )
        assert "vaultSecret" not in js
        assert "data-vault-secret" not in js
        assert "elements[f.index]" not in js
        assert "[data-hermes-vault-slot=" in js and "nonce + ':' + f.index" in js
        assert 'f.token === "current-password" && el.type !== "password"' in js  # a password fill never lands in a text box
        assert js.index('removeAttribute("data-hermes-vault-slot")') > js.index("setter.set.call")

    def test_build_fill_js_asserts_origin_before_any_write(self):
        # P1-2: the origin assert must run inside the SAME script, before
        # any element write.
        js = build_fill_js(
            [{"index": 0, "token": "current-password", "value": "x"}],
            expected_origin="https://example.com",
        )
        assert '"https://example.com"' in js
        assert "window.location.origin" in js
        assert "origin_changed" in js
        assert js.index("origin_changed") < js.index("querySelectorAll")


# ---------------------------------------------------------------------------
# Browser tool: origin binding + gating
# ---------------------------------------------------------------------------

class TestBrowserVaultTools:
    def test_check_fn_follows_the_browser_not_the_item_count(self, tmp_path):
        """The vault tools ride with the browser toolset: an empty vault must still expose
        browser_vault_save_login (that is how the first login gets saved), and no browser means no tools."""
        from tools import browser_vault_tool

        empty = VaultStore(base_dir=tmp_path / "empty-vault")
        with patch("agent.vault_store.get_vault_store", return_value=empty), \
             patch("tools.browser_use_cli.is_browser_use_cli_mode", return_value=False):
            with patch("tools.browser_tool_install.check_browser_requirements", return_value=True):
                assert browser_vault_tool._check_vault_available() is True
            with patch("tools.browser_tool_install.check_browser_requirements", return_value=False):
                assert browser_vault_tool._check_vault_available() is False
        # Browser Use mode: check_browser_requirements() is False by design, the vault must still ride along
        with patch("tools.browser_use_cli.is_browser_use_cli_mode", return_value=True), \
             patch("tools.browser_tool_install.check_browser_requirements", return_value=False):
            assert browser_vault_tool._check_vault_available() is True

    def test_list_returns_identifier_never_password(self, store):
        from tools import browser_vault_tool

        _add_login(store)
        with patch("agent.vault_store.get_vault_store", return_value=store):
            out = json.loads(browser_vault_tool.browser_vault_list())
        assert out["success"] is True
        assert out["items"][0]["handle"].startswith("vault_")
        # Design change: identifier is agent-visible metadata.
        assert out["items"][0]["identifier"] == "user@example.com"
        assert out["items"][0]["identifier_type"] == "email"
        assert "s3cret-pw" not in json.dumps(out)

    def test_fill_refused_on_origin_mismatch(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store, origin="https://example.com")
        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch.object(browser_vault_tool, "_current_page_origin", return_value="https://evil.com"):
            out = json.loads(browser_vault_tool.browser_vault_fill(meta.id))
        assert out["success"] is False
        assert out["error_type"] == "origin_mismatch"
        assert "s3cret-pw" not in json.dumps(out)

    @staticmethod
    def _manager_meta():
        from agent.vault_store import VaultItemMeta

        return VaultItemMeta(
            id="op:multi", kind="login", label="Amazon", origin="https://amazon.co.uk",
            created_at="2026-01-01T00:00:00Z", identifier_type="username", identifier="jane@example.com",
            allowed_origins=("https://amazon.co.uk", "https://www.amazon.co.uk",
                             "https://eu.account.amazon.com"))

    def test_fill_allowed_on_every_saved_origin_of_a_multi_website_item(self):
        """A manager item with several saved websites fills on each of them
        (exact match only), and the in-page origin assert pins the origin
        actually being filled — not just the first saved one."""
        from tools import browser_vault_tool

        meta = self._manager_meta()

        class _ManagerBackend:
            name, display_name, needs_unlock = "onepassword", "1Password", False

            def is_unlocked(self):
                return True

            def get_meta(self, handle):
                return meta if handle == meta.id else None

            def resolve_password(self, handle):
                return "s3cret-pw"

        controls = [
            {"autocomplete": "email", "formIndex": 0, "index": 0, "label": "", "name": "email", "type": "email"},
            {"autocomplete": "current-password", "formIndex": 0, "index": 1, "label": "", "name": "pw", "type": "password"},
        ]
        secret_exprs = []

        def fake_eval(task_id, expression):
            return {"success": True, "result": json.dumps(controls)}

        def fake_eval_secret(task_id, expression):
            secret_exprs.append(expression)
            return {"success": True, "result": json.dumps({"filled": 1})}

        with patch("agent.vault_backends.backend_for_handle", return_value=_ManagerBackend()), \
             patch.object(browser_vault_tool, "_current_page_origin", return_value="https://www.amazon.co.uk"), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
             patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret):
            raw = browser_vault_tool.browser_vault_fill("op:multi")
        out = json.loads(raw)
        assert out["success"] is True
        assert out["origin"] == "https://www.amazon.co.uk"
        # the synchronous in-page check is bound to the origin actually matched
        assert "https://www.amazon.co.uk" in secret_exprs[0]
        assert "s3cret-pw" not in raw
        assert out["filled_fields"] == 1

    def test_fill_still_refused_on_origin_not_saved_on_the_item(self):
        """Multi-website items widen nothing: an unsaved origin — even a sibling
        subdomain — is still refused (fail-closed regression)."""
        from tools import browser_vault_tool

        meta = self._manager_meta()

        class _ManagerBackend:
            name, display_name, needs_unlock = "onepassword", "1Password", False

            def is_unlocked(self):
                return True

            def get_meta(self, handle):
                return meta if handle == meta.id else None

            def resolve_password(self, handle):
                return "s3cret-pw"

        with patch("agent.vault_backends.backend_for_handle", return_value=_ManagerBackend()), \
             patch.object(browser_vault_tool, "_current_page_origin", return_value="https://payments.amazon.co.uk"):
            out = json.loads(browser_vault_tool.browser_vault_fill("op:multi"))
        assert out["success"] is False
        assert out["error_type"] == "origin_mismatch"
        assert "s3cret-pw" not in json.dumps(out)

    def test_fill_unknown_handle(self, store):
        from tools import browser_vault_tool

        with patch("agent.vault_store.get_vault_store", return_value=store):
            out = json.loads(browser_vault_tool.browser_vault_fill("vault_nope"))
        assert out["success"] is False

    def test_fill_success_returns_counts_only(self, store):
        from tools import browser_vault_tool

        meta = _add_login(store, origin="https://example.com")
        controls = [
            {"autocomplete": "email", "formIndex": 0, "index": 0, "label": "", "name": "email", "type": "email"},
            {"autocomplete": "current-password", "formIndex": 0, "index": 1, "label": "", "name": "pw", "type": "password"},
        ]

        def fake_eval(task_id, expression):
            if "location.href" in expression:
                return {"success": True, "result": "https://example.com/login"}
            return {"success": True, "result": json.dumps(controls)}

        secret_exprs = []

        def fake_eval_secret(task_id, expression):
            secret_exprs.append(expression)
            return {"success": True, "result": json.dumps({"filled": 1})}

        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
             patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret):
            raw = browser_vault_tool.browser_vault_fill(meta.id)
        out = json.loads(raw)
        # Password-only fill: exactly one field.
        out.pop("next", None)  # workflow hint, not data
        assert out == {
            "success": True,
            "filled_fields": 1,
            "backend": "local",
            "kind": "login",
            "origin": "https://example.com",
        }
        assert "s3cret-pw" not in raw
        # The secret expression only ever goes through the secret eval path,
        # and it targets only the password field (index 1).
        assert len(secret_exprs) == 1
        assert "s3cret-pw" in secret_exprs[0]
        assert '"index": 0' not in secret_exprs[0]
        assert "user@example.com" not in secret_exprs[0]

    def test_fill_toctou_navigation_writes_nothing(self, store):
        """P1-2 schedule regression: inspection passes on the allowed origin,
        the page navigates before the fill script runs, the in-script origin
        assert refuses, and zero credential bytes are written."""
        from tools import browser_vault_tool

        meta = _add_login(store, origin="https://example.com")
        controls = [
            {"autocomplete": "current-password", "formIndex": 0, "index": 0, "label": "", "name": "pw", "type": "password"},
        ]

        def fake_eval(task_id, expression):
            if "location.href" in expression:
                # Pre-check sees the allowed origin.
                return {"success": True, "result": "https://example.com/login"}
            return {"success": True, "result": json.dumps(controls)}

        def fake_eval_secret(task_id, expression):
            # The evaluated script itself must carry the origin assert.
            assert "window.location.origin" in expression
            assert '"https://example.com"' in expression
            # Simulate the page having navigated cross-origin by the time
            # the fill script executes: the script's own assert fires.
            return {
                "success": True,
                "result": json.dumps(
                    {"refused": "origin_changed", "found": "https://evil.com"}
                ),
            }

        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
             patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret):
            raw = browser_vault_tool.browser_vault_fill(meta.id)
        out = json.loads(raw)
        assert out["success"] is False
        assert out["error_type"] == "origin_changed"
        assert out.get("filled_fields", 0) == 0
        assert "s3cret-pw" not in raw

    def test_secret_eval_fails_closed_without_supervisor(self, store):
        """P1-1: the secret-bearing eval NEVER falls back to the argv path."""
        from tools import browser_vault_tool

        meta = _add_login(store, origin="https://example.com")
        controls = [
            {"autocomplete": "current-password", "formIndex": 0, "index": 0, "label": "", "name": "pw", "type": "password"},
        ]

        def fake_eval(task_id, expression):
            if "location.href" in expression:
                return {"success": True, "result": "https://example.com/login"}
            return {"success": True, "result": json.dumps(controls)}

        # No supervisor registered and the local daemon exposes no CDP endpoint → the fill refuses.
        # The daemon may be asked for its endpoint (`get cdp-url`, no secret) but NEVER handed an
        # `eval` carrying the password: that is the argv exposure this test pins.
        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
             patch("tools.browser_supervisor.SUPERVISOR_REGISTRY") as reg, \
             patch("tools.browser_tool_session._run_browser_command", return_value={"success": False}) as run_cmd:
            reg.get.return_value = None
            raw = browser_vault_tool.browser_vault_fill(meta.id)
        out = json.loads(raw)
        assert out["success"] is False
        assert out["error_type"] == "supervisor_required"
        assert all(call.args[1] == "get" for call in run_cmd.call_args_list), run_cmd.call_args_list
        assert "s3cret-pw" not in json.dumps([str(c) for c in run_cmd.call_args_list]) and "s3cret-pw" not in raw


    def test_vault_canary_redacted_from_browser_cdp_results(self, store):
        """P1-1 regression: a filled, non-token-shaped canary password must be
        unrecoverable through a model-facing browser_cdp-style result."""
        from agent import redact
        from agent.redact import redact_sensitive_text
        from tools import browser_vault_tool
        from tools.browser_cdp_tool import _redact_cdp_output

        canary = "plain sentence nobody would flag 7"
        meta = _add_login(store, origin="https://example.com", password=canary)
        controls = [
            {"autocomplete": "current-password", "formIndex": 0, "index": 0, "label": "", "name": "pw", "type": "password"},
        ]

        def fake_eval(task_id, expression):
            if "location.href" in expression:
                return {"success": True, "result": "https://example.com/login"}
            return {"success": True, "result": json.dumps(controls)}

        def fake_eval_secret(task_id, expression):
            return {"success": True, "result": json.dumps({"filled": 1})}

        try:
            with patch("agent.vault_store.get_vault_store", return_value=store), \
                 patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
                 patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret):
                out = json.loads(browser_vault_tool.browser_vault_fill(meta.id))
            assert out["success"] is True

            # Simulate a browser_cdp Runtime.evaluate sibling read echoing
            # the canary back (e.g. reading the input's value from the DOM).
            cdp_result = {
                "result": {"type": "string", "value": canary},
                "description": f"input value is {canary}",
            }
            scrubbed = _redact_cdp_output(cdp_result)
            assert canary not in json.dumps(scrubbed)
            assert "«redacted-vault-secret»" in json.dumps(scrubbed, ensure_ascii=False)

            # And the generic browser-result scrub catches it too, even with
            # user-level redaction preferences irrelevant (unconditional).
            assert canary not in redact_sensitive_text(f"page text: {canary}")
        finally:
            redact.clear_vault_redaction_values()

    def test_payment_fill_requires_confirmation_then_fills_card_fields(self, store):
        """A card is written only after the user confirms (a prompt injection reaching a checkout must not be
        able to spend); the secret eval then targets the classified card controls and the result carries
        the field tokens but never a value."""
        from tools import browser_vault_tool
        from agent import redact

        meta = store.add_item(kind="payment", label="Visa", origin="https://shop.test", secret=_CARD)
        controls = [
            {"autocomplete": "cc-number", "index": 0, "type": "text"},
            {"label": "Expiry (MM/YY)", "index": 1, "type": "text"},
            {"label": "CVC", "index": 2, "type": "text"},
            {"autocomplete": "email", "index": 3, "type": "email"},
        ]

        def fake_eval(task_id, expression):
            if "location.href" in expression:
                return {"success": True, "result": "https://shop.test/checkout"}
            return {"success": True, "result": json.dumps(controls)}

        secret_exprs = []

        def fake_eval_secret(task_id, expression):
            secret_exprs.append(expression)
            return {"success": True, "result": json.dumps({"filled": 3})}

        try:
            with patch("agent.vault_store.get_vault_store", return_value=store), \
                 patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
                 patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret), \
                 patch("tools.approval_prompt.request_elicitation_consent", return_value="decline"):
                declined = json.loads(browser_vault_tool.browser_vault_fill(meta.id))
            assert declined["success"] is False and declined["error_type"] == "payment_declined"
            assert secret_exprs == []

            with patch("agent.vault_store.get_vault_store", return_value=store), \
                 patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
                 patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret), \
                 patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
                raw = browser_vault_tool.browser_vault_fill(meta.id)
            out = json.loads(raw)
            assert out["success"] is True and out["fields"] == ["cc-csc", "cc-exp", "cc-number"]
            assert _CARD["card_number"] not in raw and _CARD["cvc"] not in raw
            assert len(secret_exprs) == 1 and _CARD["card_number"] in secret_exprs[0] and "07/29" in secret_exprs[0]
            assert '"index": 3' not in secret_exprs[0]  # the email box is never a card target
            assert _CARD["card_number"] not in redact.redact_sensitive_text(f"dom says {_CARD['card_number']}")
        finally:
            redact.clear_vault_redaction_values()


    def test_unbound_manager_card_binds_to_the_approved_https_page(self):
        """A password-manager card has no website: the approval prompt names the CURRENT page, the fill is
        bound to exactly that origin, a decline writes nothing, and plain-http pages are refused unasked."""
        from tools import browser_vault_tool
        from agent import redact
        from agent.vault_store import VaultItemMeta

        meta = VaultItemMeta(id="op:crd", kind="payment", label="Expense Card", origin=None, created_at="",
                             any_origin=True)

        class FakeBackend:
            name, display_name, needs_unlock = "onepassword", "1Password", True

            def is_unlocked(self):
                return True

            def get_meta(self, handle):
                return meta

            def resolve_secret(self, handle):
                return dict(_CARD)

        controls = [{"autocomplete": "cc-number", "index": 0, "type": "text"},
                    {"autocomplete": "cc-csc", "index": 1, "type": "text"}]
        page = {"href": "https://store.test/checkout"}

        def fake_eval(task_id, expression):
            if "location.href" in expression:
                return {"success": True, "result": page["href"]}
            return {"success": True, "result": json.dumps(controls)}

        secret_exprs, prompts = [], []

        def fake_eval_secret(task_id, expression):
            secret_exprs.append(expression)
            return {"success": True, "result": json.dumps({"filled": 2})}

        def run(decision):
            def consent(message, description, **kw):
                prompts.append((message, description))
                return decision
            with patch("agent.vault_backends.backend_for_handle", return_value=FakeBackend()), \
                 patch.object(browser_vault_tool, "_focus_bound_origin", return_value=None), \
                 patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
                 patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_eval_secret), \
                 patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent):
                return browser_vault_tool.browser_vault_fill("op:crd")

        try:
            declined = json.loads(run("decline"))
            assert declined["error_type"] == "payment_declined" and secret_exprs == []
            assert "https://store.test" in prompts[-1][0] and "not tied to one website" in prompts[-1][1]

            raw = run("accept")
            out = json.loads(raw)
            assert out["success"] is True and out["origin"] == "https://store.test"
            assert _CARD["card_number"] not in raw
            assert len(secret_exprs) == 1 and "https://store.test" in secret_exprs[0]  # in-script origin assert

            page["href"] = "http://store.test/checkout"
            n_prompts = len(prompts)
            insecure = json.loads(run("accept"))
            assert insecure["error_type"] == "insecure_origin" and len(prompts) == n_prompts
            assert len(secret_exprs) == 1
        finally:
            redact.clear_vault_redaction_values()


class TestVaultHardening:
    """Read-deny, backup perms-tightening, canonical dir securing.

    Mirrors the browser-profile snapshot hardening (f1d05c): the vault dir
    holds key + ciphertext side by side, so it gets the same treatment.
    """

    def test_read_block_vault_dir_and_contents(self, tmp_path, monkeypatch):
        import agent.file_safety as fs

        home = tmp_path / "hermes_home"
        vault = home / "vault"
        vault.mkdir(parents=True)
        (vault / "vault.key").write_text("k", encoding="utf-8")
        (vault / "vault.json.enc").write_text("blob", encoding="utf-8")
        monkeypatch.setattr(fs, "_hermes_home_path", lambda: home)

        for target in (vault, vault / "vault.key", vault / "vault.json.enc"):
            err = fs.get_read_block_error(str(target))
            assert err is not None, f"expected read deny for {target}"

    def test_read_block_leaves_sibling_dirs_alone(self, tmp_path, monkeypatch):
        import agent.file_safety as fs

        home = tmp_path / "hermes_home"
        other = home / "vaults-notes"
        other.mkdir(parents=True)
        f = other / "notes.txt"
        f.write_text("hi", encoding="utf-8")
        monkeypatch.setattr(fs, "_hermes_home_path", lambda: home)
        assert fs.get_read_block_error(str(f)) is None

    def test_backup_secret_names_include_vault_files(self):
        from hermes_cli.backup import _SECRET_FILE_NAMES

        assert "vault.key" in _SECRET_FILE_NAMES
        assert "vault.json.enc" in _SECRET_FILE_NAMES

    def test_vault_dir_is_owner_only(self, store, tmp_path):
        old_umask = os.umask(0o022)
        try:
            _add_login(store)
        finally:
            os.umask(old_umask)
        mode = stat.S_IMODE(os.stat(tmp_path / "vault").st_mode)
        assert not mode & 0o077, oct(mode)




def test_every_registered_tool_schema_declares_openai_style_parameters():
    """The registry emits ``parameters`` (OpenAI function shape) and every provider adapter converts from
    it; a schema that spells it ``input_schema`` (Anthropic shape) ships with NO parameters, so the model is
    told the tool takes nothing and calls browser_vault_fill without a handle."""
    import model_tools
    from tools.registry import registry

    missing = [entry.name for entry in registry.get_all_entries()
               if "parameters" not in entry.schema or "input_schema" in entry.schema]
    assert not missing, missing


class TestSaveLoginPrompt:
    """browser_vault_save_login: the surface prompt supplies the login, the tool stores it bound to the page
    origin and fills. The password must never come back in the tool result."""

    def test_saves_to_page_origin_and_never_echoes_the_password(self, store, monkeypatch):
        from agent.vault_backends import unlock as unlock_mod
        from tools import browser_vault_tool

        seen = {}

        def prompt(origin, site):
            seen["origin"], seen["site"] = origin, site
            return {"identifier": "tek@acme.test", "password": "hunter2-very-secret"}

        unlock_mod.set_save_login_prompt_callback(prompt)
        monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task_id: "https://acme.test")
        monkeypatch.setattr(browser_vault_tool, "browser_vault_fill",
                            lambda handle, task_id=None: json.dumps({"success": True, "filled_fields": 1}))
        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch("agent.vault_backends.unlock.can_prompt_here", return_value=True):
            out = json.loads(browser_vault_tool.browser_vault_save_login(task_id="t1"))
        unlock_mod.set_save_login_prompt_callback(None)

        assert out["success"] is True and out["identifier"] == "tek@acme.test"
        assert "hunter2" not in json.dumps(out)
        assert seen == {"origin": "https://acme.test", "site": "acme.test"}
        [meta] = store.list_items()
        assert meta.origin == "https://acme.test" and meta.identifier == "tek@acme.test"

    def test_declined_or_headless_stores_nothing(self, store, monkeypatch):
        from agent.vault_backends import unlock as unlock_mod
        from tools import browser_vault_tool

        monkeypatch.setattr(browser_vault_tool, "_current_page_origin", lambda task_id: "https://acme.test")
        with patch("agent.vault_store.get_vault_store", return_value=store):
            unlock_mod.set_save_login_prompt_callback(lambda origin, site: None)
            with patch("agent.vault_backends.unlock.can_prompt_here", return_value=True):
                declined = json.loads(browser_vault_tool.browser_vault_save_login())
            with patch("agent.vault_backends.unlock.can_prompt_here", return_value=False):
                headless = json.loads(browser_vault_tool.browser_vault_save_login())
            unlock_mod.set_save_login_prompt_callback(None)
        assert declined["error_type"] == "save_declined"
        assert headless["error_type"] == "prompt_unavailable"
        assert store.list_items() == []


class TestManagerAutoDetection:
    def test_installed_manager_is_a_source_without_config_and_config_can_opt_out(self):
        from agent.vault_backends import base

        with patch.object(base, "is_installed", return_value=True):
            with patch.object(base, "_cfg", return_value={}):
                assert {b.name for b in base.enabled_backends()} == {"local", "onepassword", "bitwarden"}
            with patch.object(base, "_cfg", return_value={"bitwarden": {"enabled": False}}):
                assert {b.name for b in base.enabled_backends()} == {"local", "onepassword"}
        with patch.object(base, "is_installed", return_value=False), patch.object(base, "_cfg", return_value={}):
            assert [b.name for b in base.enabled_backends()] == ["local"]


def test_every_vault_tool_is_in_the_browser_toolset():
    """toolsets.py is a hand-maintained list; a tool registered here but missing there is invisible to the model
    (live: browser_vault_save_login was registered, tested, and never offered)."""
    import toolsets
    from tools import browser_vault_tool
    from tools.registry import registry

    registered = {e.name for e in registry.get_all_entries() if e.name.startswith("browser_vault_")}
    assert registered <= set(toolsets.TOOLSETS["browser"]["tools"]), registered - set(toolsets.TOOLSETS["browser"]["tools"])


class TestTwoFactor:
    def test_totp_matches_rfc6238_vector_and_seed_normalisation(self):
        from agent.vault_store import VaultError, normalize_otp_secret, totp_now

        seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
        assert totp_now(seed, digits=8, at=59) == "94287082"
        assert totp_now(seed, at=1111111109) == "081804"
        assert normalize_otp_secret("otpauth://totp/GitHub:tek?secret=jbsw y3dp ehpk3pxp&issuer=GitHub") == "JBSWY3DPEHPK3PXP"
        with pytest.raises(VaultError):
            normalize_otp_secret("not base32!")
        # Non-default otpauth parameters are kept and honoured (RFC 6238 SHA-256 / 8-digit vector at T=59).
        stored = normalize_otp_secret(f"otpauth://totp/x?secret={'GEZDGNBVGY3TQOJQ' * 2}&digits=8&period=30&algorithm=SHA256")
        assert stored.endswith("|8|30|SHA256")
        sha256_seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZA"  # "1234567890" * 3.2 -> RFC 32-byte seed
        assert totp_now(sha256_seed + "|8|30|SHA256", at=59) == "46119246"
        assert totp_now("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ|6|60|SHA1", at=119) == totp_now("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", period=60, at=119)
        with pytest.raises(VaultError):
            normalize_otp_secret("otpauth://hotp/x?secret=JBSWY3DPEHPK3PXP&counter=1")

    def test_saved_authenticator_key_mints_codes_without_asking(self, store, monkeypatch):
        """The whole point: with a seed on the login, enter_code never prompts and the code never comes back."""
        from agent.vault_backends import unlock as unlock_mod
        from tools import browser_vault_tool

        meta = store.add_item("login", "gh", {"identifier_type": "username", "identifier": "tek", "password": "pw",
                                              "otp_secret": "JBSWY3DPEHPK3PXP"}, origin="https://github.com")
        assert store.get_meta(meta.id).has_otp is True
        asked = []
        unlock_mod.set_code_prompt_callback(lambda site, hint: asked.append(site) or "000000")
        controls = [{"index": 0, "type": "text", "name": "otp", "label": "Authentication code", "autocomplete": "one-time-code"}]
        seen = {}

        def fake_eval(task_id, expr):
            return {"success": True, "result": json.dumps(controls) if "querySelectorAll" in expr else "https://github.com/sessions/two-factor"}

        def fake_secret(task_id, expr):
            seen["expr"] = expr
            return {"success": True, "result": json.dumps({"filled": 1})}

        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch.object(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
             patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_secret):
            raw = browser_vault_tool.browser_vault_enter_code(meta.id, task_id="t")
        unlock_mod.set_code_prompt_callback(None)
        out = json.loads(raw)
        assert out["success"] and out["source"] == "local" and asked == []
        code = re.search(r'"value": "(\d{6})"', seen["expr"]).group(1)
        assert code not in raw  # the code went to the page, not to the model

    def test_without_a_key_the_user_is_asked_and_split_boxes_get_one_digit_each(self, store):
        from agent.vault_backends import unlock as unlock_mod
        from tools import browser_vault_tool

        unlock_mod.set_code_prompt_callback(lambda site, hint: "246 810")
        boxes = [{"index": i, "type": "tel", "name": f"digit{i}", "label": "", "autocomplete": "one-time-code",
                  "formIndex": 0, "maxLength": 1} for i in range(6)]
        seen = {}
        fake_eval = lambda t, e: {"success": True, "result": json.dumps(boxes) if "querySelectorAll" in e else "https://acme.test/2fa"}

        def fake_secret(t, e):
            seen["expr"] = e
            return {"success": True, "result": json.dumps({"filled": 6})}

        with patch("agent.vault_backends.unlock.can_prompt_here", return_value=True), \
             patch.object(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
             patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_secret):
            out = json.loads(browser_vault_tool.browser_vault_enter_code(task_id="t"))
            unlock_mod.set_code_prompt_callback(lambda site, hint: "")
            declined = json.loads(browser_vault_tool.browser_vault_enter_code(task_id="t"))
        unlock_mod.set_code_prompt_callback(None)
        assert out["success"] and out["source"] == "user" and out["filled_fields"] == 6
        assert re.findall(r'"value": "(\d)"', seen["expr"]) == list("246810")
        assert declined["error_type"] == "code_declined"

    def test_several_code_like_inputs_that_are_not_a_digit_widget_get_one_field(self):
        """Reviewer case: a page with 4+ code-ish inputs (promo code, zip code, a real OTP box...) must never
        get a digit sprayed across them. Only an unmistakable maxlength=1 same-form adjacent group splits."""
        from agent.vault_login_classifier import ClassifiedLoginControl, LoginControl, build_otp_fills

        def ctl(i, form=0, maxlen=None, score=70):
            return ClassifiedLoginControl(LoginControl("", form, i, "", f"code{i}", "text", maxlen), score, "one-time-code")

        scattered = [ctl(0), ctl(3), ctl(7), ctl(9, form=1), ctl(12, score=100)]
        assert build_otp_fills(scattered, "246810") == [{"index": 12, "token": "one-time-code", "value": "246810"}]
        # maxlength=1 but different forms / non-adjacent: still one field
        assert len(build_otp_fills([ctl(i, form=i % 2, maxlen=1) for i in range(6)], "246810")) == 1
        assert len(build_otp_fills([ctl(i * 2, maxlen=1) for i in range(6)], "246810")) == 1
        # five boxes for a six-digit code: one field
        assert len(build_otp_fills([ctl(i, maxlen=1) for i in range(5)], "246810")) == 1
        # the real widget
        assert [f["value"] for f in build_otp_fills([ctl(i + 4, maxlen=1) for i in range(6)], "246810")] == list("246810")

    def test_no_code_field_points_at_passkey_or_device_approval(self):
        from tools import browser_vault_tool

        fake_eval = lambda t, e: {"success": True, "result": json.dumps([{"index": 0, "type": "text", "name": "q", "label": "Search", "autocomplete": ""}]) if "querySelectorAll" in e else "https://acme.test/approve"}
        with patch.object(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval):
            out = json.loads(browser_vault_tool.browser_vault_enter_code(task_id="t"))
        assert out["error_type"] == "no_code_field"


class TestEmailCodes:
    """enter_code's email source: TOTP first, then the inbox that receives the login's identifier, then the user."""

    CODE = "739104"
    CONTROLS = [{"index": 0, "type": "text", "name": "otp", "label": "Verification code", "autocomplete": "one-time-code"}]

    def _run(self, store, *, lane_polls, prompt=None, can_prompt=True, identifier="me@work.example", otp_secret=None):
        from agent import vault_email_codes
        from agent.vault_backends import unlock as unlock_mod
        from tools import browser_vault_tool

        secret = {"identifier_type": "email", "identifier": identifier, "password": "pw"}
        if otp_secret:
            secret["otp_secret"] = otp_secret
        meta = store.add_item("login", "Acme", secret, origin="https://accounts.acme.test")
        seen, lanes = {}, []

        class Lane:
            def __init__(self):
                self.polls = list(lane_polls)

            def recent(self, since, matches):
                batch = self.polls.pop(0) if self.polls else []
                return [m for m in batch if matches(m.sender, m.subject)]

            def close(self):
                pass

        def factory(ident, cfg):
            lanes.append(ident)
            return Lane()

        def fake_eval(task_id, expr):
            return {"success": True, "result": json.dumps(self.CONTROLS) if "querySelectorAll" in expr else "https://accounts.acme.test/2fa"}

        def fake_secret(task_id, expr):
            seen["expr"] = expr
            return {"success": True, "result": json.dumps({"filled": 1})}

        asked = []
        unlock_mod.set_code_prompt_callback((lambda site, hint: asked.append(site) or prompt) if prompt is not None else None)
        cfg = {"enabled": True, "wait_seconds": 0, "poll_interval_seconds": 1, "lookback_seconds": 600}
        real_wait = vault_email_codes.wait_for_email_code
        try:
            with patch("agent.vault_store.get_vault_store", return_value=store), \
                 patch("agent.vault_email_codes.email_codes_config", return_value=cfg), \
                 patch("agent.vault_email_codes.wait_for_email_code",
                       side_effect=lambda *a, **k: real_wait(*a, **{**k, "lane_factory": factory, "sleep": lambda s: None})), \
                 patch("agent.vault_backends.unlock.can_prompt_here", return_value=can_prompt), \
                 patch.object(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None), \
                 patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
                 patch.object(browser_vault_tool, "_eval_js_secret", side_effect=fake_secret):
                raw = browser_vault_tool.browser_vault_enter_code(meta.id, task_id="t")
        finally:
            unlock_mod.set_code_prompt_callback(None)
        return raw, seen, asked, lanes

    def _mail(self, body=None, sender="Acme <no-reply@acme.test>", key="gmail:m1"):
        import time as _t
        from agent.vault_email_codes import EmailMessage
        return EmailMessage(sender, "Your Acme sign-in code", _t.time() - 20, body or f"Your verification code is {self.CODE}.", key=key)

    def test_code_from_email_is_filled_headless_without_asking_and_never_returned(self, store):
        from agent import redact
        from agent.redact import redact_sensitive_text

        try:
            raw, seen, asked, lanes = self._run(store, lane_polls=[[self._mail(key="gmail:headless")]], can_prompt=False)
            out = json.loads(raw)
            assert out["success"] and out["source"] == "email" and asked == []
            assert lanes == ["me@work.example"]  # the inbox is chosen by the login's identifier
            assert f'"value": "{self.CODE}"' in seen["expr"]
            assert self.CODE not in raw
            assert self.CODE not in redact_sensitive_text(f"the page now shows {self.CODE}")
        finally:
            redact.clear_vault_redaction_values()

    def test_no_email_falls_back_to_the_user_prompt(self, store):
        raw, seen, asked, _ = self._run(store, lane_polls=[], prompt="135792")
        out = json.loads(raw)
        assert out["success"] and out["source"] == "user" and asked == ["accounts.acme.test"]
        assert '"value": "135792"' in seen["expr"]

    def test_ambiguous_or_wrong_sender_email_falls_back_to_the_user(self, store):
        raw, _, asked, _ = self._run(store, lane_polls=[[self._mail(body="Code 111111. Backup code 222222.", key="gmail:amb")]], prompt="135792")
        assert json.loads(raw)["source"] == "user" and asked
        raw, _, asked, _ = self._run(store, lane_polls=[[self._mail(sender="x <no-reply@evil.test>", key="gmail:evil")]], prompt="135792")
        assert json.loads(raw)["source"] == "user" and asked

    def test_headless_without_an_email_code_says_so(self, store):
        raw, _, _, _ = self._run(store, lane_polls=[], can_prompt=False)
        out = json.loads(raw)
        assert out["error_type"] == "prompt_unavailable" and "email" in out["error"]

    def test_authenticator_key_still_wins_over_email(self, store):
        raw, _, _, lanes = self._run(store, lane_polls=[[self._mail(key="gmail:totp")]], otp_secret="JBSWY3DPEHPK3PXP")
        assert json.loads(raw)["source"] == "local" and lanes == []

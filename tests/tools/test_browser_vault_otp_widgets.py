"""enter_code on split one-time-code widgets, and on the tab the agent is actually driving.

Two failures from one white-labeled GoHighLevel email-verification page:

1. The code was six ``<input class="ui-otp-field-input" type="number" maxlength="1" pattern="[0-9]"
   placeholder="0">`` boxes with no name, id, label or autocomplete. The classifier saw no code field
   (``no_code_field``).
2. The page was open in the tab browser_exec drove, but the vault acted on another tab of the same shared
   Chrome (an unrelated pinned tab the supervisor attached to first) and refused with ``origin_mismatch``:
   nothing told the vault which tab the agent was driving.

Codes and secrets here are fixtures. Nothing reads or types a real code.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.vault_login_classifier import LoginControl, build_otp_fills, classify_otp_controls

_FIXTURE_CODE = "246810"
_CRM = "https://crm.example.test"
_PINNED = "https://finance.example.test"


def _box(index: int, **kw) -> dict:
    """One box of the GoHighLevel widget as the inspection script reports it."""
    raw = {"index": index, "type": "number", "name": " ", "label": "    0 ", "autocomplete": "", "formIndex": None,
           "maxLength": 1, "pattern": "[0-9]", "inputMode": "", "hint": "ui-otp-field-input ui-otp-field"}
    raw.update(kw)
    return raw


def _classify(raw: list[dict]):
    return classify_otp_controls([LoginControl.from_dict(r) for r in raw])


def _fills(raw: list[dict], code: str = _FIXTURE_CODE) -> list[dict]:
    found = _classify(raw)
    return build_otp_fills(found, code) if found else []


# ── split-widget detection ───────────────────────────────────────────────────


class TestSplitCodeWidgets:
    def test_six_unnamed_digit_boxes_get_one_digit_each(self):
        # an unrelated search box before the widget must not join it
        raw = [{"index": 0, "type": "text", "name": "q", "label": "Search", "autocomplete": ""}]
        raw += [_box(i) for i in range(1, 7)]
        assert [(f["index"], f["value"]) for f in _fills(raw)] == list(zip(range(1, 7), _FIXTURE_CODE))

    def test_boxes_named_only_by_a_wrapper_class_are_found(self):
        raw = [_box(i, type="text", pattern="", hint="form-control pin-entry") for i in range(6)]
        assert [f["value"] for f in _fills(raw)] == list(_FIXTURE_CODE)

    def test_unhinted_digit_only_boxes_are_found(self):
        raw = [_box(i, type="text", pattern="", inputMode="numeric", hint="") for i in range(6)]
        assert [f["value"] for f in _fills(raw)] == list(_FIXTURE_CODE)

    def test_one_box_marked_one_time_code_brings_its_whole_group(self):
        """iOS/Chrome guidance puts autocomplete=one-time-code on the first box only."""
        raw = [_box(0, autocomplete="one-time-code")] + [_box(i, hint="") for i in range(1, 6)]
        assert [f["value"] for f in _fills(raw)] == list(_FIXTURE_CODE)

    def test_a_generic_card_wrapper_does_not_veto_a_code_widget(self):
        raw = [_box(i, hint="ui-otp-field-input card card-body") for i in range(6)]
        assert len(_fills(raw)) == 6

    @pytest.mark.parametrize("raw", [
        # a date split into parts: MM / DD / YYYY
        [{"index": 0, "type": "text", "name": "month", "label": "MM", "maxLength": 2, "inputMode": "numeric"},
         {"index": 1, "type": "text", "name": "day", "label": "DD", "maxLength": 2, "inputMode": "numeric"},
         {"index": 2, "type": "text", "name": "year", "label": "YYYY", "maxLength": 4, "inputMode": "numeric"}],
        # a phone number split into parts
        [{"index": i, "type": "tel", "name": f"phone{i}", "label": "Phone", "maxLength": n} for i, n in enumerate((3, 3, 4))],
        # a date of birth as single-digit boxes, labelled as such
        [_box(i, label=lbl, hint="") for i, lbl in enumerate(("Day", "Day", "Month", "Month", "Year", "Year"))],
        # single-character letter boxes that say nothing about a code (a word game, initials)
        [_box(i, type="text", pattern="", hint="letter-tile") for i in range(6)],
        # three digit boxes: too few for a code widget
        [_box(i) for i in range(3)],
    ], ids=["date-parts", "phone-parts", "dob-digit-boxes", "letter-tiles", "three-boxes"])
    def test_numeric_groups_that_are_not_codes_are_left_alone(self, raw):
        assert _fills(raw) == []

    def test_single_code_field_still_takes_the_whole_code(self):
        for raw in ([{"index": 0, "type": "text", "name": "otp", "label": "", "autocomplete": "one-time-code"}],
                    [{"index": 0, "type": "text", "name": "verification_code", "label": "Verification code",
                      "autocomplete": "", "maxLength": 6}]):
            assert _fills(raw) == [{"index": 0, "token": "one-time-code", "value": _FIXTURE_CODE}]


class TestEnterCodeOnTheSplitWidget:
    def test_user_code_lands_one_digit_per_box_and_never_in_the_result(self):
        from agent.vault_backends import unlock as unlock_mod
        from tools import browser_vault_tool as vt

        raw = [_box(i) for i in range(6)]
        seen = {}

        def fake_eval(task_id, expr):
            return {"success": True, "result": json.dumps(raw) if "querySelectorAll" in expr else _CRM + "/verify"}

        def fake_secret(task_id, expr):
            seen["expr"] = expr
            return {"success": True, "result": json.dumps({"filled": 6})}

        unlock_mod.set_code_prompt_callback(lambda site, hint: _FIXTURE_CODE)
        try:
            with patch("agent.vault_backends.unlock.can_prompt_here", return_value=True), \
                 patch.object(vt, "_focus_bound_origin", lambda *a, **k: None), \
                 patch.object(vt, "_eval_js", side_effect=fake_eval), \
                 patch.object(vt, "_eval_js_secret", side_effect=fake_secret):
                out_raw = vt.browser_vault_enter_code(task_id="t-crm")
        finally:
            unlock_mod.set_code_prompt_callback(None)
        out = json.loads(out_raw)
        assert out["success"] and out["filled_fields"] == 6
        assert re.findall(r'"value": "(\d)"', seen["expr"]) == list(_FIXTURE_CODE)
        assert _FIXTURE_CODE not in out_raw


# ── driven-tab resolution ────────────────────────────────────────────────────


class _SharedChrome:
    """The supervisor's view of a shared Chrome: several tabs, one attached page session. ``focus_page`` follows
    the real contract: candidates on ``origin`` (any http(s) tab for ""), filtered by the probe, the
    ``prefer_target`` tab first, else the first candidate."""

    def __init__(self, tabs: dict[str, tuple[str, bool]], attached: str):
        self.tabs = tabs  # target id -> (href, passes the kind's probe)
        self.attached = attached
        self.focus_calls: list[dict] = []

    def focus_page(self, origin, accept=None, *, prefer_url=None, prefer_target=None, **_kw):
        from agent.vault_store import normalize_origin
        self.focus_calls.append({"origin": origin, "accept": accept, "prefer_target": prefer_target})
        ids = [t for t, (href, probe_ok) in self.tabs.items()
               if (not origin or normalize_origin(href) == origin) and (accept is None or probe_ok)]
        if not ids:
            return {"ok": False, "error": "no open page"}
        self.attached = prefer_target if prefer_target in ids else ids[0]
        return {"ok": True, "url": self.tabs[self.attached][0]}

    def evaluate_runtime(self, expression):
        return {"ok": True, "result": self.tabs[self.attached][0]}


@pytest.fixture
def chrome(monkeypatch):
    from tools import browser_vault_tool as vt
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    holder = {}
    monkeypatch.setattr(vt, "_page_backend", lambda task_id: None)
    monkeypatch.setattr(vt, "_ensure_supervisor", lambda task_id: holder["chrome"])
    monkeypatch.setattr("tools.browser_vault_signin.bound_supervisor", lambda task_id: None)

    def make(tabs, attached, driven=None):
        holder["chrome"] = _SharedChrome(tabs, attached)
        if driven:
            SUPERVISOR_REGISTRY.note_page("t-tab", tabs[driven][0], driven)
        return holder["chrome"]

    yield make
    SUPERVISOR_REGISTRY.stop("t-tab")


def _login(origin: str = _CRM):
    return SimpleNamespace(kind="login", any_origin=False, allowed_origins=(origin,), origin=origin, id="crm-login")


class TestDrivenTab:
    def test_the_driven_tab_wins_over_the_tab_the_supervisor_attached_first(self, chrome):
        """The incident: an unrelated tab is attached; the agent drives a CRM tab, and a second CRM tab is
        open too. The vault must act on the driven one."""
        from tools import browser_vault_tool as vt

        page = chrome({"pinned": (_PINNED + "/dashboard", False), "crm-other": (_CRM + "/", True),
                       "crm-driven": (_CRM + "/v2/location/x/verify", True)}, attached="pinned", driven="crm-driven")
        assert vt._bound_page_origin("t-tab", _login(), "otp") == _CRM
        assert page.attached == "crm-driven"

    def test_a_driven_tab_the_probe_does_not_recognise_is_still_used(self, chrome):
        from tools import browser_vault_tool as vt

        page = chrome({"pinned": (_PINNED + "/", False), "crm": (_CRM + "/verify", False)},
                      attached="pinned", driven="crm")
        assert vt._bound_page_origin("t-tab", _login(), "otp") == _CRM
        assert page.attached == "crm"

    def test_a_refusal_names_the_driven_tab_not_a_stale_attached_one(self, chrome):
        from tools import browser_vault_tool as vt

        chrome({"gmail": ("https://mail.google.com/", False), "pinned": (_PINNED + "/", False)},
               attached="gmail", driven="pinned")
        out = vt._bound_page_origin("t-tab", _login(), "otp")
        assert isinstance(out, dict)
        assert out["error_type"] == "origin_mismatch" and f"({_PINNED})" in out["error"]

    def test_an_unbound_code_stays_on_the_driven_site(self, chrome):
        """enter_code without a handle searches any tab; with a driven tab known it must not wander to
        another site's code field."""
        from tools import browser_vault_tool as vt

        page = chrome({"other": ("https://bank.example/2fa", True), "crm": (_CRM + "/verify", False)},
                      attached="other", driven="crm")
        vt._focus_bound_origin("t-tab", "", "otp")
        assert page.attached == "crm"

    def test_without_a_driven_tab_the_probe_search_is_unchanged(self, chrome):
        from tools import browser_vault_tool as vt

        page = chrome({"pinned": (_PINNED + "/", False), "crm": (_CRM + "/verify", True)}, attached="pinned")
        assert vt._bound_page_origin("t-tab", _login(), "otp") == _CRM
        assert page.attached == "crm" and all(c["prefer_target"] is None for c in page.focus_calls)


# ── the producer: browser_exec records its harness's current tab ─────────────


_SHOT = Path(__file__).resolve().parents[2] / "tools" / "browser_use_shot.py"


def _run_harness_exit(tmp_path: Path, current_tab: str, exit_code: int) -> tuple[int, str]:
    """Load browser_use_shot the way the browser_exec launcher does (by path, no Hermes imports), arm the
    recorder with a stand-in helpers module, and end the interpreter the way the agent's code might."""
    out = tmp_path / "driven.json"
    out.write_text("")
    script = (
        "import importlib.util, sys, types\n"
        f"spec = importlib.util.spec_from_file_location('hermes_browser_use_shot', {str(_SHOT)!r})\n"
        "shot = importlib.util.module_from_spec(spec); spec.loader.exec_module(shot)\n"
        f"def current_tab():\n    {current_tab}\n"
        "shot.record_driven_tab_at_exit(types.SimpleNamespace(current_tab=current_tab))\n"
        f"raise SystemExit({exit_code})\n"
    )
    env = {**os.environ, "HERMES_BH_DRIVEN_TAB_FILE": str(out)}
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60,
                          check=False)
    return proc.returncode, out.read_text()


def test_the_harness_records_its_current_tab_when_the_call_ends(tmp_path):
    rc, written = _run_harness_exit(
        tmp_path, "return {'targetId': 'T-CRM', 'url': 'https://crm.example.test/verify', 'title': 'x'}", 1)
    assert rc == 1  # the agent's exit status is untouched
    assert json.loads(written) == {"targetId": "T-CRM", "url": "https://crm.example.test/verify"}


def test_a_daemon_that_cannot_say_records_nothing(tmp_path):
    rc, written = _run_harness_exit(tmp_path, "raise RuntimeError('not_attached')", 0)
    assert rc == 0 and written == ""


def test_browser_exec_records_the_driven_tab_for_its_task(tmp_path, monkeypatch):
    """End to end through browser_exec with a stand-in CLI that does what the launcher's recorder does."""
    from tools import browser_session as bs
    from tools import browser_supervisor
    from tools import browser_use_cli as bu_cli
    from tools.browser_supervisor import _SupervisorRegistry

    registry = _SupervisorRegistry()
    monkeypatch.setattr(registry, "get_or_start", lambda task_id, cdp_url, **kw: None)  # no real browser behind it
    monkeypatch.setattr(browser_supervisor, "SUPERVISOR_REGISTRY", registry)
    monkeypatch.setattr(bs, "get_session_info", lambda key: {"cdp_url": f"ws://127.0.0.1:47000/devtools/browser/{key}",
                                                             "features": {"local": True}})
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda: {"browser": {}})
    monkeypatch.setattr("tools.browser_session_cdp.resolve_cdp_override", lambda url: url)
    monkeypatch.delenv("BU_NAME", raising=False)
    cli = tmp_path / "browser-use"
    cli.write_text("#!/bin/sh\ncat > /dev/null\n"
                   "printf '%s' '{\"targetId\": \"T-CRM\", \"url\": \"https://crm.example.test/verify\"}' "
                   "> \"$HERMES_BH_DRIVEN_TAB_FILE\"\necho ok\n", encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(bu_cli, "_find_cli", lambda: [str(cli)])

    result = json.loads(bu_cli.browser_exec("print(1)", task_id="t-driven"))

    assert result["success"] is True
    assert registry.driven_tab("t-driven") == ("https://crm.example.test/verify", "T-CRM")

#!/usr/bin/env python3
"""Vault-backed model-blind browser autofill tools.

One model-facing tool, ``browser_vault``, deferred behind tool_search (only its catalog line is
paid per request) and available wherever a browser is. Its actions:

- ``list``  → handles + metadata (for logins this includes the
  identifier — it is NOT a secret; the agent types it itself). Passwords are
  never returned.
- ``fill``  → server-side fill of the CURRENT page from a vault
  handle: the password field for logins, card fields for payment items (after
  the user confirms), address fields for address items. The secret is
  resolved locally, the page origin must EXACTLY match the item's bound
  origin (pre-checked AND re-asserted synchronously inside the fill script),
  the field is chosen by the ported login-control classifier, injection runs
  exclusively over the supervisor CDP WebSocket (never argv), and the tool
  result reports only ``{filled_fields, kind, origin, success}`` — the
  password never appears in tool results, logs, or the session DB, and its
  exact bytes are registered with the browser-result redaction boundary so
  no later browser tool call can echo them back to the model.

Ported design from Merit-Systems/OpenInstinct (MIT): opaque-handle vault
autofill (kernel-login-autofill.ts / fill_from_vault.ts).
"""

from __future__ import annotations

import json
import secrets
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_vault_available() -> bool:
    """Schema-gate: the vault rides with the browser. An empty vault still needs save_login so the
    agent can offer to remember a login the first time it meets a form; hiding the tool until an item
    exists meant nobody ever discovered the feature. Deferral keeps that presence nearly free."""
    from tools.browser_tool_install import check_browser_requirements
    from tools.browser_use_cli import is_browser_use_cli_mode
    # check_browser_requirements() is False by design in Browser Use mode (browser_exec replaces the
    # built-in surface); the vault serves both stacks.
    return bool(is_browser_use_cli_mode() or check_browser_requirements())


# ---------------------------------------------------------------------------
# JS evaluation plumbing (server-side; results never carry secret values)
# ---------------------------------------------------------------------------

def _eval_js(task_id: str, expression: str) -> dict[str, Any]:
    """Evaluate NON-SECRET JS on the current page (inspection, origin reads).

    Prefers the supervisor's persistent CDP WebSocket, falls back to the
    agent-browser CLI ``eval`` command. Never use this for expressions that
    embed secret values — the fallback places the expression in subprocess
    argv. Use :func:`_eval_js_secret` for secret-bearing expressions.
    """
    from tools.browser_vault_signin import bound_supervisor
    bound = bound_supervisor(task_id)
    if bound is not None:
        try:
            result = bound.evaluate_runtime(expression)
            return {"success": result.get("ok") is True, "result": result.get("result")}
        except (OSError, RuntimeError, ValueError, TimeoutError):
            return {"success": False, "error": "Sign-in page unavailable"}
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY

        supervisor = SUPERVISOR_REGISTRY.get(task_id)
        if supervisor is not None:
            sup = supervisor.evaluate_runtime(expression)
            if sup.get("ok"):
                return {"success": True, "result": sup.get("result")}
            err = str(sup.get("error") or "")
            if "supervisor" not in err.lower():
                return {"success": False, "error": err}
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("vault fill: supervisor eval unavailable (%s)", exc)

    from tools.browser_tool import _last_session_key
    from tools.browser_tool_session import _run_browser_command

    effective = _last_session_key(task_id)
    result = _run_browser_command(effective, "eval", [expression])
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "eval failed")}
    return {"success": True, "result": result.get("data", {}).get("result")}


def _ensure_supervisor(task_id: str):
    """The supervisor for ``task_id``, attaching one on demand for a LOCAL built-in browser session.

    Cloud/CDP-override sessions and browser_exec attach their supervisor when the session is created;
    a local agent-browser ``--session`` has no ``cdp_url`` of its own, so nothing did. Ask the daemon
    for the packaged Chromium's endpoint (``get cdp-url``: same daemon, same reaper) and attach.
    Returns None when no endpoint is reachable; the fill then refuses rather than touching argv."""
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    from tools.browser_vault_signin import bound_supervisor

    supervisor = bound_supervisor(task_id) or SUPERVISOR_REGISTRY.get(task_id)
    if supervisor is not None:
        return supervisor
    from tools.browser_tool import _active_sessions, _last_session_key
    from tools.browser_tool_cdp import _get_dialog_policy_config, _resolve_cdp_override
    from tools.browser_tool_session import _run_browser_command

    # A session that already knows its endpoint (real-profile / CDP-override / cloud) needs no daemon
    # round-trip; in a busy shared browser ``get cdp-url`` can sit out its full 30 s command timeout.
    session_key = _last_session_key(task_id)
    cdp_url = str((_active_sessions.get(session_key) or {}).get("cdp_url") or "")
    if not cdp_url:
        res = _run_browser_command(session_key, "get", ["cdp-url"])
        cdp_url = str(((res or {}).get("data") or {}).get("cdpUrl") or "") if (res or {}).get("success") else ""
    if not cdp_url:
        return None
    policy, timeout_s = _get_dialog_policy_config()
    try:
        return SUPERVISOR_REGISTRY.get_or_start(task_id=task_id, cdp_url=_resolve_cdp_override(cdp_url),
                                                dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:
        logger.debug("vault fill: supervisor attach to local session failed (%s)", exc)
        return None


def _eval_js_secret(task_id: str, expression: str) -> dict[str, Any]:
    """Evaluate a SECRET-BEARING JS expression. Supervisor CDP-WS only.

    Fails closed: there is deliberately NO fallback to the agent-browser CLI
    ``eval`` path, because that places the expression — and therefore the
    credential bytes — in subprocess argv, visible to any process listing.
    When no supervisor session is available the caller gets a typed refusal
    (``error_type='supervisor_required'``) and nothing is written.
    """
    from tools.desktop_admission import DesktopInputDenied, assert_agent_admitted, task_endpoint
    try:
        assert_agent_admitted(task_endpoint(task_id))
    except DesktopInputDenied:
        return {"success": False, "error_type": "human_has_control", "error": "Desktop agent input is unavailable"}

    try:
        supervisor = _ensure_supervisor(task_id)
    except Exception as exc:
        logger.debug("vault fill: supervisor unavailable (%s)", exc)
        supervisor = None

    if supervisor is None:
        return {
            "success": False,
            "error_type": "supervisor_required",
            "error": (
                "Vault fill requires the supervised browser session (direct "
                "CDP WebSocket). The fallback eval path would place the "
                "credential in subprocess argv, so it is never used for "
                "secrets. Start the browser through the Hermes-managed "
                "session and retry."
            ),
        }

    # Re-admit at the WRITE. The handler-level fence (_fenced_page_op) admitted before a possibly
    # human-length prompt (enter_code waits for the user's code); a takeover during that wait must
    # refuse here, before the credential lands in a page the human is now typing into. The outer
    # epoch check only discards the result, and a fill is a side effect, not a result.
    if _bot_desktop_browser_session(task_id):
        from tools.bot_desktop import lease as _bd_lease
        try:
            _bd_lease.assert_agent_may_act()
        except _bd_lease.HumanHasControl as exc:
            return {"success": False, "error_type": "human_has_control", "error": str(exc)}

    from tools.desktop_admission import DesktopInputDenied, assert_agent_admitted
    try:
        assert_agent_admitted(str(getattr(supervisor, "cdp_url", "")))
    except DesktopInputDenied:
        return {"success": False, "error_type": "human_has_control", "error": "Desktop agent input is unavailable"}

    from tools.browser_vault_signin import bound_target_matches
    if not bound_target_matches(supervisor, task_id):
        return {"success": False, "error_type": "target_changed", "error": "Sign-in page changed"}
    sup = supervisor.evaluate_runtime(expression)
    if sup.get("ok"):
        return {"success": True, "result": sup.get("result")}
    return {
        "success": False,
        "error_type": "supervisor_required"
        if "supervisor" in str(sup.get("error") or "").lower()
        else "eval_failed",
        "error": str(sup.get("error") or "eval failed"),
    }


def _parse_json_result(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def _current_page_origin(task_id: str) -> Optional[str]:
    res = _eval_js(task_id, "window.location.href")
    if not res.get("success"):
        return None
    href = str(res.get("result") or "").strip().strip('"').strip("'")
    if not href or href == "about:blank":
        return None
    try:
        from agent.vault_store import normalize_origin

        return normalize_origin(href)
    except Exception:
        return None


# Per kind: a JS probe that is truthy on a tab holding the form this kind fills.
_TAB_PROBES = {
    "login": "!!document.querySelector('input[type=password]')",
    "payment": "!!document.querySelector('input[autocomplete^=cc-], [name*=card i], [placeholder*=card i], [name*=cvc i], [name*=cvv i]')",
    "address": "!!document.querySelector('input[autocomplete^=address-], [autocomplete=postal-code], [name*=address i], [name*=zip i], [name*=postal i]')",
}


def _focus_bound_origin(task_id: str, origin: str, kind: str) -> Optional[str]:
    """Point the supervisor's page session at the open tab on ``origin`` that holds a ``kind`` form
    (browser_exec sessions open their own tabs, so the tab the supervisor attached to first is rarely the
    login page). Returns the origin when a tab was focused, else None (caller falls back to the current page)."""
    try:
        supervisor = _ensure_supervisor(task_id)
    except Exception:
        supervisor = None
    if supervisor is None:
        return None
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    from tools.browser_vault_signin import fill_target, bound_target_matches
    binding = fill_target.get()
    if binding is not None:
        return origin if bound_target_matches(supervisor, task_id) and origin == binding.origin else None
    focused = supervisor.focus_page(origin, accept=_TAB_PROBES.get(kind), prefer_url=SUPERVISOR_REGISTRY.page_hint(task_id))
    if not focused.get("ok"):
        logger.debug("vault fill: focus on %s failed: %s", origin or "any site", focused.get("error"))
    return (origin or focused.get("url")) if focused.get("ok") else None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def browser_vault_list() -> str:
    """List login handles + metadata across every enabled backend. Passwords are never included.

    A locked external manager contributes no items; instead it is reported under ``locked`` so the
    agent knows to call fill (which prompts the user to unlock) or tell the user.
    """
    from agent.vault_backends import enabled_backends
    from agent.vault_backends.unlock import can_prompt_here

    items, locked, errors = [], [], []
    for backend in enabled_backends():
        if backend.needs_unlock and not backend.is_unlocked():
            locked.append({"backend": backend.name, "display_name": backend.display_name,
                           "unlock": "browser_vault action=unlock" if can_prompt_here() else "unavailable_in_this_session"})
            continue
        try:
            metas = backend.list_items()
        except Exception as exc:
            errors.append({"backend": backend.name, "error": str(exc)[:200]})
            continue
        for meta in metas:
            entry = {"handle": meta.id, "backend": backend.name, "label": meta.label, "kind": meta.kind,
                     "origin": meta.origin, "available": meta.kind == "login" or bool(meta.origin) or meta.any_origin}
            if len(meta.allowed_origins) > 1:
                entry["allowed_origins"] = list(meta.allowed_origins)
            if meta.any_origin:
                entry["fill_scope"] = "current https checkout page; the user approves that exact site on every fill"
            if meta.kind == "login" and (meta.has_otp or backend.needs_unlock):
                entry["two_factor"] = "automatic" if meta.has_otp else "automatic if the manager stores a TOTP seed, else the user is asked"
            if meta.identifier:
                entry["identifier"] = meta.identifier
                entry["identifier_type"] = meta.identifier_type
            items.append(entry)
    out: dict[str, Any] = {"success": True, "items": items}
    if not items:
        out["hint"] = ("No saved logins. On a login page, call browser_vault action=save_login to ask the user to save one. "
                       "Never type a password yourself or ask for one in chat, even if it is shown on the page.")
    if locked:
        out["locked"] = locked
    if errors:
        out["errors"] = errors
    return json.dumps(out, ensure_ascii=False)


def browser_vault_unlock(backend_name: str) -> str:
    """Ask the user (via the surface's masked prompt) to unlock an external manager for this session."""
    from agent.vault_backends import enabled_backends
    from agent.vault_backends.unlock import can_prompt_here, get_unlock_prompt_callback

    backend = next((b for b in enabled_backends() if b.name == backend_name and b.needs_unlock), None)
    if backend is None:
        return json.dumps({"success": False, "error": f"No unlockable vault backend named {backend_name!r}."})
    if backend.is_unlocked():
        return json.dumps({"success": True, "backend": backend.name, "already_unlocked": True})
    if not can_prompt_here():
        return json.dumps({"success": False, "error_type": "unlock_unavailable",
                           "error": (f"{backend.display_name} is locked and this session cannot prompt for the "
                                     "master password (headless/cron/API). Unlock it from an interactive Hermes "
                                     "session or the Desktop app first.")})
    prompt = get_unlock_prompt_callback()
    master = prompt(backend.name, backend.display_name) if prompt else ""
    if not master:
        return json.dumps({"success": False, "error_type": "unlock_cancelled",
                           "error": f"The user declined to unlock {backend.display_name}."})
    try:
        backend.unlock(master)  # type: ignore[attr-defined]
    except Exception as exc:
        return json.dumps({"success": False, "error_type": "unlock_failed", "error": str(exc)[:300]})
    finally:
        del master
    return json.dumps({"success": True, "backend": backend.name})


def browser_vault_save_login(label: str = "", task_id: Optional[str] = None) -> str:
    """Ask the user (masked prompt on their surface) for the login of the CURRENT page, store it in the local
    vault bound to that origin, and fill the password at once. The values never enter the conversation."""
    from agent.vault_backends.unlock import can_prompt_here, get_save_login_prompt_callback
    from agent.vault_store import get_vault_store

    effective_task_id = task_id or "default"
    # The supervisor's default page session is whatever tab it attached to first (on Browser Use that is
    # the daemon's blank tab); the login form lives in the tab with a password field, so focus that one.
    _focus_bound_origin(effective_task_id, "", "login")
    origin = _current_page_origin(effective_task_id)
    if not origin:
        return json.dumps({"success": False, "error": "Open the site's login page first; the login is saved for that page's origin."})
    prompt = get_save_login_prompt_callback()
    if prompt is None or not can_prompt_here():
        return json.dumps({"success": False, "error_type": "prompt_unavailable",
                           "error": (f"This session cannot ask the user for a login (headless/cron/API). Tell them to run "
                                     f"`hermes vault add` or use Desktop → Settings → Passwords & Logins for {origin}.")})
    host = origin.split("://", 1)[-1]
    site = label.strip() or host
    answer = prompt(origin, host)  # the prompt names the site by host: the user recognises URLs, not agent labels
    if not answer or not answer.get("password") or not answer.get("identifier"):
        return json.dumps({"success": False, "error_type": "save_declined",
                           "error": "The user chose not to save a login for this site. Do not ask again this turn."})
    identifier = str(answer["identifier"]).strip()
    id_type = "email" if "@" in identifier else ("phone" if identifier.lstrip("+").isdigit() else "username")
    try:
        meta = get_vault_store().add_item("login", site, {"identifier_type": id_type, "identifier": identifier,
                                                        "password": str(answer["password"])}, origin=origin)
    except Exception as exc:
        return json.dumps({"success": False, "error_type": "save_failed", "error": str(exc)[:200]})
    finally:
        answer.clear()
    filled = json.loads(browser_vault_fill(meta.id, task_id=effective_task_id))
    return json.dumps({"success": True, "handle": meta.id, "origin": origin, "identifier": identifier,
                       "identifier_type": id_type, "fill": filled,
                       "next": "Type the identifier into the username field if the form has one, then submit."},
                      ensure_ascii=False)


_TAB_PROBES["otp"] = ("!!document.querySelector('input[autocomplete=one-time-code], input[name*=otp i], input[name*=code i], "
                      "input[id*=otp i], input[id*=code i], input[name*=totp i], input[aria-label*=code i]')")


def _email_code(backend, handle: str, site: str) -> Optional[str]:
    """The code from the site's email in the inbox that receives this login's identifier
    (``vault.email_codes``), or None. Read server-side; the email never reaches the model."""
    from agent import vault_email_codes

    try:
        meta = backend.get_meta(handle)
    except Exception:
        meta = None
    if meta is None or not meta.identifier:
        return None
    try:
        return vault_email_codes.wait_for_email_code(site, meta.identifier, site_label=meta.label or "")
    except Exception as exc:
        logger.info("vault enter_code: email source failed (%s)", type(exc).__name__)
        return None


def browser_vault_enter_code(handle: str = "", task_id: Optional[str] = None) -> str:
    """Second factor: fill the one-time code the CURRENT page asks for. If the saved login (``handle``) has an
    authenticator seed, the code is minted server-side and nobody is asked; else, when ``vault.email_codes``
    maps the login's identifier to a readable inbox, the code is read server-side from the site's email; otherwise
    the user is prompted on their surface for the code their phone/email/app shows. The code goes into the page
    over the supervisor socket and never enters the conversation."""
    from agent.redact import register_vault_redaction_value
    from agent.vault_backends import backend_for_handle
    from agent.vault_backends.unlock import can_prompt_here, get_code_prompt_callback
    from agent.vault_login_classifier import LoginControl, build_fill_js, build_inspection_js, build_otp_fills, classify_otp_controls

    effective_task_id = task_id or "default"
    _focus_bound_origin(effective_task_id, "", "otp")
    origin = _current_page_origin(effective_task_id)
    if not origin:
        return json.dumps({"success": False, "error": "No page with a code field is open."})
    site = origin.split("://", 1)[-1]

    nonce = secrets.token_hex(8)
    inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
    raw_controls = _parse_json_result(inspect.get("result")) if inspect.get("success") else None
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    otp_controls = classify_otp_controls([LoginControl.from_dict(r) for r in (raw_controls or []) if isinstance(r, dict)])
    if not otp_controls:
        return json.dumps({"success": False, "error_type": "no_code_field",
                           "error": ("No one-time-code field on the current page. If the site wants a passkey, hardware key or "
                                     "an approval tap in an app, tell the user to complete it on their device and wait for the page to move on.")})

    code: Optional[str] = None
    source = "user"
    backend = backend_for_handle(handle) if handle else None
    if backend is not None:
        try:
            code = backend.resolve_otp(handle)
        except Exception:
            code = None
        if code:
            source = backend.name
    if not code and backend is not None:
        code = _email_code(backend, handle, site)
        if code:
            source = "email"
    if not code:
        prompt = get_code_prompt_callback()
        if prompt is None or not can_prompt_here():
            return json.dumps({"success": False, "error_type": "prompt_unavailable",
                               "error": (f"{site} asks for a one-time code, no code arrived by email in an inbox Hermes can read, "
                                         "and this session cannot ask the user (headless/cron/API). Save an authenticator key "
                                         "for this login so codes can be generated automatically.")})
        code = (prompt(site, "") or "").strip().replace(" ", "").replace("-", "")
        if not code:
            return json.dumps({"success": False, "error_type": "code_declined",
                               "error": "The user did not enter a code. Do not ask again this turn."})

    register_vault_redaction_value(code)
    fills = build_otp_fills(otp_controls, code)
    result = _eval_js_secret(effective_task_id, build_fill_js(fills, expected_origin=origin, nonce=nonce))
    del code
    if not result.get("success"):
        return json.dumps({"success": False, "error": str(result.get("error") or "fill failed")[:200]})
    parsed = _parse_json_result(result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return json.dumps({"success": False, "error_type": "origin_changed", "error": "The page navigated before the code could be entered. Nothing was written."})
    filled = int(parsed.get("filled", 0)) if isinstance(parsed, dict) else 0
    return json.dumps({"success": bool(filled), "filled_fields": filled, "origin": origin, "source": source,
                       "next": "Submit the form (many sites auto-submit when the last digit lands)."})


def browser_vault_fill(handle: str, task_id: Optional[str] = None) -> str:
    """Fill the current page's password field from a vault handle.

    Password-only: the identifier is agent-visible metadata (see
    browser_vault_list) and is typed by the agent via normal input tools.
    The password is resolved server-side and injected via in-page JS over
    the supervisor CDP WebSocket; the result reports only counts/metadata.
    """
    from agent.redact import register_vault_redaction_value
    from agent.vault_login_classifier import (
        ClassifiedLoginControl,
        LoginControl,
        build_fill_js,
        build_inspection_js,
        classify_checkout_control,
        classify_login_control,
        select_checkout_fills,
        select_password_fill,
    )
    from agent.vault_backends import UnlockRequired, backend_for_handle
    from agent.vault_store import ADDRESS_FIELDS, PAYMENT_FIELDS, VaultError, scrub_secret_from_text

    effective_task_id = task_id or "default"
    backend = backend_for_handle(handle)
    if backend is not None and backend.needs_unlock and not backend.is_unlocked():
        unlocked = json.loads(browser_vault_unlock(backend.name))
        if not unlocked.get("success"):
            return json.dumps(unlocked)

    try:
        meta = backend.get_meta(handle) if backend is not None else None
    except UnlockRequired:
        return json.dumps({"success": False, "error_type": "unlock_required",
                           "error": f"{backend.display_name} locked again; call browser_vault action=unlock."})
    if meta is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"No vault item with handle {handle!r}. Use browser_vault action=list. "
                    "To save a credential: run `hermes vault add` in a terminal, or "
                    "in the desktop app open Settings → Credential Vault."
                ),
            }
        )
    if meta.kind != "login" and not meta.origin and not meta.any_origin:
        return json.dumps({"success": False, "error_type": "no_origin",
                           "error": f"Vault item {handle!r} has no bound origin; {meta.kind} items are filled only on the site they were saved for."})

    # ── Origin binding pre-check (cheap early exit; the authoritative check
    # runs synchronously inside the fill script itself) ──────────────────────
    # Manager items can bind several websites (e.g. amazon.co.uk + www.amazon.co.uk);
    # every saved origin is a valid fill target. Matching stays exact-origin —
    # nothing wildcard/parent-domain is ever inferred.
    if meta.any_origin:
        # Unbound manager card: the user's approval of THIS page's exact origin is the binding. The fill
        # script re-asserts that origin atomically, so a navigation after approval writes nothing.
        _focus_bound_origin(effective_task_id, "", meta.kind)
        page_origin = _current_page_origin(effective_task_id)
        if not page_origin:
            return json.dumps({"success": False, "error": "Could not determine the current page origin. Open the checkout page first."})
        if not page_origin.startswith("https://"):
            return json.dumps({"success": False, "error_type": "insecure_origin",
                               "error": f"Refused: cards from a password manager are filled only on https pages, not {page_origin}."})
        allowed = [page_origin]
    else:
        allowed = list(meta.allowed_origins) or ([str(meta.origin)] if meta.origin else [])
        page_origin = None
        for candidate in allowed:
            page_origin = _focus_bound_origin(effective_task_id, candidate, meta.kind)
            if page_origin:
                break
        page_origin = page_origin or _current_page_origin(effective_task_id)
    if not page_origin:
        return json.dumps(
            {"success": False, "error": "Could not determine the current page origin. Navigate to the login page first."}
        )
    if page_origin not in allowed:
        return json.dumps(
            {
                "success": False,
                "error_type": "origin_mismatch",
                "error": (
                    f"Refused: current page origin ({page_origin}) does not match "
                    f"the vault item's bound origin(s) ({', '.join(allowed)}). Vault fills "
                    "only run on the exact origin(s) the credential was saved for."
                ),
            }
        )

    # ── Inspect + classify page controls ────────────────────────────────────
    classify = classify_login_control if meta.kind == "login" else classify_checkout_control

    def inspect_controls():
        """(nonce, classified controls) or (None, error-json). The nonce binds a fill to THIS inspection."""
        nonce = secrets.token_hex(8)
        inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
        if not inspect.get("success"):
            return None, json.dumps(
                {"success": False, "error": f"Could not inspect page inputs: {inspect.get('error', 'eval failed')}"})
        raw_controls = _parse_json_result(inspect.get("result"))
        if isinstance(raw_controls, str):
            raw_controls = _parse_json_result(raw_controls)
        if not isinstance(raw_controls, list):
            return None, json.dumps({"success": False, "error": "Page input inspection returned no usable controls."})
        found: list[ClassifiedLoginControl] = []
        for raw in raw_controls:
            if not isinstance(raw, dict):
                continue
            result = classify(LoginControl.from_dict(raw))
            if result is not None:
                found.append(result)
        if not found:
            return None, json.dumps({"success": False, "error": f"No {meta.kind} form fields were found on the current page."})
        return nonce, found

    nonce, classified = inspect_controls()
    if nonce is None:
        return classified

    receipt = None

    def with_receipt(obj: dict, filled: bool = False) -> str:
        if receipt is not None:
            obj["payment"] = {**receipt, "filled": filled}
        return json.dumps(obj)

    if meta.kind == "payment":
        # The card goes in only after the human approves THIS total at THIS exact origin with THIS card.
        payment = _payment_approval_block(effective_task_id, backend, handle, meta, page_origin)
        verdict, details = _confirm_payment_fill(meta.label, page_origin, payment, unbound=meta.any_origin)
        if verdict != "accept":
            expired = verdict == "cancel"
            return json.dumps({
                "success": False, "error_type": "payment_expired" if expired else "payment_declined",
                "error": ("Nobody answered the payment approval in time. Nothing was filled. Do not retry on your own; "
                          "ask the user." if expired else
                          "The user did not confirm filling this payment card. Do not retry; ask them instead."),
                "payment": _payment_receipt(payment, "expired" if expired else "declined", details, filled=False)})
        receipt = _payment_receipt(payment, "approved", details, filled=False)
        # The human wait can be minutes: the page may have navigated or re-rendered since the first look.
        if _current_page_origin(effective_task_id) != page_origin:
            return json.dumps({"success": False, "error_type": "origin_changed", "payment": receipt,
                               "error": f"Refused: the page left {page_origin} while the user was approving. Nothing was written."})
        nonce, classified = inspect_controls()
        if nonce is None:
            out = json.loads(classified)
            out["payment"] = receipt
            return json.dumps(out)
        # Same-origin cart edits invalidate the approved snapshot just as navigation does.
        # Re-read only non-secret page facts; never re-use the human's approval after a mismatch.
        current_summary = _checkout_summary(effective_task_id)
        if current_summary is None or any(current_summary[k] != payment[k] for k in current_summary):
            return with_receipt({
                "success": False, "error_type": "payment_changed", "fresh_request_needed": True,
                "error": "The checkout changed or could not be re-read after approval. Nothing was filled. "
                         "Ask the user for a fresh payment request; never replay this approval."})

    # ── Resolve secret and fill (secret never enters any logged string) ─────
    try:
        if meta.kind == "login":
            secret = {"password": backend.resolve_password(handle)}
            fills = select_password_fill(classified, secret["password"])
        else:
            secret = backend.resolve_secret(handle)
            fills = select_checkout_fills(classified, secret, PAYMENT_FIELDS if meta.kind == "payment" else ADDRESS_FIELDS)
    except UnlockRequired:
        out = {"success": False, "error_type": "unlock_required",
               "error": f"{backend.display_name} locked again; call browser_vault action=unlock."}
        if receipt is not None:
            out["fresh_request_needed"] = True
        return with_receipt(out)
    except (VaultError, OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        # Resolution may fail before a secret dictionary exists to scrub against. Never echo
        # provider exception text: it may include a card, token or other credential.
        out = {"success": False, "error_type": "secret_resolution_failed",
               "error": "Could not resolve the saved credential. Nothing was filled."}
        if receipt is not None:
            out["fresh_request_needed"] = True
        return with_receipt(out)
    if not fills:
        out = {"success": False, "error": f"No fillable {meta.kind} field matched the saved item on this page."}
        return with_receipt(out)

    # Register the secret bytes with the model-egress redaction boundary
    # BEFORE they touch the page: any later browser_* result (including
    # browser_cdp Runtime.evaluate reads) that echoes them is scrubbed.
    # Address values are not secrets but the card fields are: register every payment value.
    for value in (secret.values() if meta.kind == "payment" else [secret.get("password", "")]):
        register_vault_redaction_value(value)

    try:
        fill_result = _eval_js_secret(
            effective_task_id, build_fill_js(fills, expected_origin=page_origin, nonce=nonce)
        )
    except Exception as exc:
        # Strip any secret material from exception text before surfacing.
        return with_receipt({"success": False, "error": scrub_secret_from_text(str(exc), secret)})
    if not fill_result.get("success"):
        err = scrub_secret_from_text(str(fill_result.get("error") or "fill failed"), secret)
        out = {"success": False, "error": err}
        if fill_result.get("error_type"):
            out["error_type"] = fill_result["error_type"]
        return with_receipt(out)

    parsed = _parse_json_result(fill_result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return with_receipt(
            {
                "success": False,
                "error_type": "origin_changed",
                "error": (
                    "Refused: the page navigated away from the bound origin "
                    f"({page_origin}) before the fill could run "
                    f"(now on {parsed.get('found') or 'unknown'}). "
                    "Nothing was written."
                ),
            }
        )
    filled = parsed.get("filled", 0) if isinstance(parsed, dict) else 0

    out = {"success": bool(filled), "filled_fields": int(filled), "backend": backend.name,
           "kind": meta.kind, "origin": page_origin}
    if meta.kind == "login":
        out["next"] = ("Submit. If the site then asks for a verification code, call browser_vault action=enter_code with this handle"
                       + (" (a code will be generated automatically)." if meta.has_otp else "."))
    if meta.kind != "login":
        out["fields"] = sorted(f["token"] for f in fills)  # which controls were targeted, never the values
    return with_receipt(out, filled=bool(filled))


def _payment_limits_config() -> dict:
    """``vault.payment_limits``: card label → ``{amount, currency, period}``. Missing → {}."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_home import HomeInitializationError

    try:
        vault = load_config_readonly().get("vault")
    except (OSError, HomeInitializationError):
        return {}
    limits = vault.get("payment_limits") if isinstance(vault, dict) else None
    return limits if isinstance(limits, dict) else {}


def _limit_period(text: str) -> str:
    low = (text or "").lower()
    if "month" in low:
        return "monthly"
    if "purchase" in low or "transaction" in low or "per_purchase" in low:
        return "per_purchase"
    return "unknown"


def _card_limit(backend, handle: str, label: str) -> Optional[dict]:
    """The card's limit from config (wins) or the vault item's own limit field. Never from the page."""
    import re
    from agent.vault_checkout_summary import parse_money

    entry = _payment_limits_config().get(label)
    if isinstance(entry, dict):
        try:
            amount = float(entry.get("amount"))
        except (TypeError, ValueError):
            amount = None
        if amount is not None and amount > 0:
            currency = str(entry.get("currency") or "").upper() or None
            period = str(entry.get("period") or "")
            return {"amount_minor": round(amount * 100), "currency": currency,
                    "period": period if period in ("per_purchase", "monthly") else "unknown", "source": "config"}
    from agent.vault_backends import UnlockRequired

    getter = getattr(backend, "card_limit_text", None)
    try:
        text = getter(handle) if callable(getter) else None
    except (UnlockRequired, RuntimeError, json.JSONDecodeError):
        text = None
    if not text:
        return None
    money = parse_money(text)
    if money is None and (m := re.search(r"\d+(?:\.\d{1,2})?", text.replace(",", ""))):
        money = {"amount_minor": round(float(m.group(0)) * 100), "currency": None}
    if not money or money["amount_minor"] <= 0:
        return None
    return {"amount_minor": money["amount_minor"], "currency": money.get("currency"),
            "period": _limit_period(text), "source": "vault"}


def _checkout_summary(task_id: str) -> Optional[dict]:
    """Read non-secret page facts; distinguish an unreadable page from a readable missing total."""
    from agent.vault_checkout_summary import ORDER_SUMMARY_JS, summarize_checkout

    try:
        res = _eval_js(task_id, ORDER_SUMMARY_JS)
        raw = _parse_json_result(res.get("result")) if isinstance(res, dict) and res.get("success") else None
        if isinstance(raw, str):
            raw = _parse_json_result(raw)
        return summarize_checkout(raw) if isinstance(raw, dict) else None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _payment_approval_block(task_id: str, backend, handle: str, meta, origin: str) -> dict:
    """The ``payment`` block of a vault-payment approval (contract v1). ``origin`` is the browser's exact page
    origin and ``card`` comes from the vault; merchant/total/items are read deterministically from the page
    and labelled ``source: page``. The full card number, expiry and CVC never appear here."""
    from agent.vault_backends import UnlockRequired
    from agent.vault_store import VaultError
    from agent.vault_checkout_summary import summarize_checkout

    summary = _checkout_summary(task_id) or summarize_checkout(None)
    getter = getattr(backend, "card_last4", None)
    try:
        last4 = getter(handle) if callable(getter) else getattr(meta, "last4", None)
    except (UnlockRequired, VaultError, OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        last4 = None
    last4 = last4 if isinstance(last4, str) and len(last4) == 4 and last4.isdigit() else None
    limit = _card_limit(backend, handle, meta.label)
    total = summary["total"]
    over_limit = None
    if total is not None and limit is not None and (
            not total.get("currency") or not limit.get("currency") or total["currency"] == limit["currency"]):
        over_limit = total["amount_minor"] > limit["amount_minor"]
    return {
        "v": 1, "origin": origin, "origin_bound": not getattr(meta, "any_origin", False),
        "merchant": summary["merchant"], "total": total, "lines": summary["lines"],
        "lines_truncated": summary["lines_truncated"], "adjustments": summary["adjustments"],
        "card": {"label": meta.label, "last4": last4, "backend": getattr(backend, "name", None), "limit": limit},
        "checks": {"total_found": total is not None, "over_limit": over_limit},
    }


def _money_display(amount_minor: int, currency: Optional[str]) -> str:
    symbol = {"USD": "$", "CAD": "$", "AUD": "$", "EUR": "€", "GBP": "£"}.get(currency or "", "$")
    return f"{symbol}{amount_minor // 100:,}.{amount_minor % 100:02d}"


def _payment_copy(payment: dict, *, unbound: bool) -> tuple[str, str, str]:
    """(command, description, title) for legacy clients, the CLI panel and push. Amount + origin in the
    command keep two different payments from coalescing into one approval."""
    from urllib.parse import urlsplit

    origin = payment["origin"]
    host = urlsplit(origin).hostname or origin
    card = payment["card"]
    card_text = card["label"] + (f" ••{card['last4']}" if card.get("last4") else "")
    total = payment.get("total")
    if total:
        command = f"Pay {total['display']} at {origin} with {card_text}"
        title = f"Approve {total['display']} at {host}?"
        description = (f"{title} The agent fills {card_text} and places the order. "
                       "It never sees the card number.")
    else:
        command = f"Fill {card_text} at {origin} (total not found)"
        title = f"Approve card fill at {host}?"
        description = (f"Total not found on the checkout page. Approve filling {card_text} on {host} anyway? "
                       "The agent never sees the card number.")
    limit = card.get("limit")
    if payment["checks"].get("over_limit") and limit:
        description += (f" This is over the {_money_display(limit['amount_minor'], limit.get('currency'))} "
                        "card limit, so the card issuer may decline it.")
    if unbound:
        description += (f" This card is not tied to one website: check that {host} is the store you mean "
                        "before approving.")
    return command, description, title


def _confirm_payment_fill(label: str, origin: str, payment: dict, *, unbound: bool = False) -> tuple[str, dict]:
    """Human confirmation before a card is written into a page: a prompt injection that reaches a checkout
    must not be able to spend. Routes through the approval surface of the active session (gateway decision
    panel or CLI panel) with the structured ``payment`` block; headless sessions cannot confirm and the fill
    is refused. Once only: no session/always scope is offered, and the server refuses one if sent. For an
    unbound password-manager card this approval is the only site binding, so the prompt says so.
    Returns ``(verdict, details)`` where verdict is accept/decline/cancel (cancel = nobody answered)."""
    import time
    from tools import approval_context as _ctx
    from tools.approval_prompt import request_elicitation_consent

    command, description, title = _payment_copy(payment, unbound=unbound)
    try:
        timeout = max(float(_ctx._get_approval_timeout()), 0.0)
    except (TypeError, ValueError, OverflowError, AttributeError):
        timeout = 300.0
    created = time.time()
    extra = {"surface": "vault-payment", "allow_session": False, "allow_permanent": False, "payment": payment,
             "created_at": created, "timeout_seconds": timeout, "expires_at": created + timeout}
    details: dict = {}
    verdict = request_elicitation_consent(command, description, surface="vault-payment", title=title,
                                          extra=extra, details=details)
    return verdict, details


def _payment_receipt(payment: dict, decision: str, details: dict, *, filled: bool) -> dict:
    """Non-secret echo kept in the saved tool result so the chat can redraw the order card after a reload:
    merchant, exact origin, items, total, card label + last four, and how it was decided."""
    import time
    keep = ("v", "origin", "origin_bound", "merchant", "total", "lines", "lines_truncated", "adjustments",
            "card", "checks")
    return {**{k: payment.get(k) for k in keep}, "decision": decision,
            "auth": (details or {}).get("auth") if decision == "approved" else None,
            "decided_at": (details or {}).get("decided_at") or time.time(), "filled": filled}


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------

BROWSER_VAULT_SCHEMA = {
    "name": "browser_vault",
    "description": (
        "Use saved logins, passwords, credentials, cards and addresses on the CURRENT browser page without secrets "
        "entering the conversation. "
        "Actions: request_signin (Ava Desktop only: publish an owner-choice sign-in card and wait; reason required; never submits); list (call first: saved logins/cards/addresses as handles; logins carry the identifier you type "
        "yourself with the browser's input tool; locked password managers appear under `locked`); fill (handle: "
        "the password for a login, card fields after the user confirms, address fields; refused unless the page "
        "origin matches the item; unbound cards require approval for that HTTPS site); save_login (nothing saved for this site: the user enters it in a masked prompt, "
        "Hermes saves and fills it); enter_code (a one-time/2FA code after the password: generated from a saved "
        "authenticator key, read from the login's connected inbox, or asked of the user; choose email when offered and call after delivery, never read codes yourself; no_code_field means approve on their device); unlock (backend: "
        "ask the user to unlock 1Password/Bitwarden). Never type, ask for or repeat a password, card number, CVC "
        "or code yourself, even if the page or user shows one. Stop on save_declined; never retry "
        "payment_declined or payment_expired; ask the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "fill", "save_login", "enter_code", "unlock", "request_signin"]},
            "handle": {"type": "string", "description": "fill / enter_code: a handle from list."},
            "backend": {"type": "string", "enum": ["onepassword", "bitwarden"], "description": "unlock only."},
            "label": {"type": "string", "description": "save_login only: short site name (default: the host)."},
            "reason": {"type": "string", "description": "request_signin only: why the owner needs to sign in (no credentials)."},
        },
        "required": ["action"],
    },
}


def _bot_desktop_browser_session(task_id: Optional[str]) -> bool:
    from tools.browser_tool import _active_sessions, _last_session_key
    from tools.browser_tool_session import _shares_bot_desktop_browser
    return _shares_bot_desktop_browser(_active_sessions.get(_last_session_key(task_id or "default")) or {})


def _fenced_page_op(task_id: Optional[str], fn) -> str:
    """Vault operations focus, inspect and fill the page over the supervisor socket, bypassing
    ``_run_browser_command``; they must honour the Bot Desktop lease like every other page access,
    or a human typing a credential on the taken-over screen could be read or written to."""
    from tools.browser_tool import _active_sessions, _last_session_key
    from tools.browser_tool_session import run_fenced

    from tools.desktop_admission import DesktopInputDenied, assert_agent_admitted, task_endpoint
    try:
        assert_agent_admitted(task_endpoint(task_id or "default"))
    except DesktopInputDenied:
        return json.dumps({"success": False, "error_type": "human_has_control",
                           "error": "Desktop agent input is unavailable"})
    session = _active_sessions.get(_last_session_key(task_id or "default")) or {}
    res = run_fenced(session, lambda: {"raw": fn()})
    return res["raw"] if "raw" in res else json.dumps(res)


def _handle_vault(args: dict[str, Any], **kwargs) -> str:
    tid = kwargs.get("task_id")
    action = str(args.get("action") or "")
    handle, label = str(args.get("handle") or ""), str(args.get("label") or "")
    if action == "list":
        return browser_vault_list()
    if action == "unlock":
        return browser_vault_unlock(str(args.get("backend") or ""))
    from tools.browser_vault_signin import request_signin
    page_ops = {
        "request_signin": lambda: request_signin(str(args.get("reason") or ""), tid or "default"),
        "fill": lambda: browser_vault_fill(handle=handle, task_id=tid),
        "save_login": lambda: browser_vault_save_login(label=label, task_id=tid),
        "enter_code": lambda: browser_vault_enter_code(handle=handle, task_id=tid),
    }
    if action not in page_ops:
        return json.dumps({"success": False, "error": f"Unknown action {action!r}; use list, fill, save_login, "
                                                       "enter_code or unlock."})
    return _fenced_page_op(tid, page_ops[action])


from tools.registry import no_cache_check_fn, registry

_check_vault_available = no_cache_check_fn(_check_vault_available)

registry.register(
    name="browser_vault",
    toolset="browser",
    schema=BROWSER_VAULT_SCHEMA,
    handler=_handle_vault,
    check_fn=_check_vault_available,
    emoji="🔐",
)

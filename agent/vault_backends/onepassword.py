"""1Password Login (and, opt-in, Credit Card) items as a vault backend (``op`` CLI).

Unlock: ``op signin --raw`` with the master password on stdin (desktop-app
integration or account-level auth) mints an ``OP_SESSION_<account>`` token.
A configured service-account token skips the prompt entirely (headless).
List: ``op item list --categories Login --format json`` → title, urls,
username. Resolve: ``op item get <id> --vault <vault> --fields label=password --reveal``.

Service accounts refuse ``op item get`` without a vault query, so the vault id
seen at list time is remembered per item and passed on every read.

With ``vault.onepassword.payment_cards: true`` Credit Card items are listed as
``payment`` handles. 1Password cards carry no website, so they are unbound
(``any_origin``): the fill tool binds each fill to the current https page only
after the user approves that exact site.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from agent.secret_sources.base import run_cli
from agent.secret_sources.onepassword import _OP_ENV_ALLOWLIST, _scrub, find_op
from agent.vault_backends.base import LoginBackend, UnlockRequired, run_with_stdin_secret
from agent.vault_backends import unlock as _unlock
from agent.vault_store import VaultItemMeta, normalize_origin

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0

# 1Password built-in Credit Card field ids → canonical PAYMENT_FIELDS names.
_CARD_FIELD_IDS = {"ccnum": "card_number", "cvv": "cvc", "cardholder": "cardholder_name"}


class OnePasswordLoginBackend(LoginBackend):
    name = "onepassword"
    display_name = "1Password"
    prefix = "op:"
    needs_unlock = True

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        from agent.secret_scope import get_secret
        env_name = str(self.cfg.get("service_account_token_env") or "OP_SERVICE_ACCOUNT_TOKEN")
        self._service_token = get_secret(env_name, "") or ""
        self._vaults: dict[str, str] = {}  # item id → vault id, filled by list_items

    @property
    def payment_cards(self) -> bool:
        """Opt-in: list Credit Card items as unbound payment handles (vault.onepassword.payment_cards)."""
        return self.cfg.get("payment_cards") is True

    # ── auth ────────────────────────────────────────────────────────────────

    def _op(self) -> Path:
        op = find_op(str(self.cfg.get("binary_path") or ""))
        if op is None:
            raise RuntimeError("1Password CLI (op) not found — install it or set vault.onepassword.binary_path")
        return op

    def _env(self, session_token: Optional[str]) -> dict[str, str]:
        from agent.secret_scope import get_secret
        env = {k: os.environ[k] for k in _OP_ENV_ALLOWLIST if k in os.environ and not k.startswith("OP_CONNECT_")}
        # Connect credentials outrank OP_SERVICE_ACCOUNT_TOKEN inside op, so they must come from the
        # profile's own secret scope like the service token does — never from the launch environment.
        for k in ("OP_CONNECT_HOST", "OP_CONNECT_TOKEN"):
            if v := get_secret(k, ""):
                env[k] = v
        env["NO_COLOR"] = "1"
        account = str(self.cfg.get("account") or "")
        if account:
            env["OP_ACCOUNT"] = account
        if self._service_token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = self._service_token
        elif session_token:
            # op signin --raw prints the bare token; the env var name carries the account shorthand,
            # which op also accepts as plain OP_SESSION for the default account.
            env[f"OP_SESSION_{account}" if account else "OP_SESSION"] = session_token
        return env

    def is_unlocked(self) -> bool:
        return bool(self._service_token) or _unlock.is_unlocked(self.name)

    def unlock(self, master_password: str) -> None:
        """Mint a session token from the master password (consumed on stdin, never argv)."""
        generation = _unlock.begin_unlock(self.name)
        cmd = [str(self._op()), "signin", "--raw"]
        if account := str(self.cfg.get("account") or ""):
            cmd += ["--account", account]
        proc = run_with_stdin_secret(cmd, env=self._env(None), secret=master_password, timeout=_TIMEOUT, label="op")
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            raise RuntimeError(f"1Password unlock failed: {_scrub(proc.stderr or '')[:200] or 'no session token'}")
        if not _unlock.store_session_token(self.name, token, generation):
            raise RuntimeError("1Password was locked while unlocking; try again")

    def _run(self, *args: str) -> str:
        token = None if self._service_token else _unlock.get_session_token(self.name)
        if not self._service_token and not token:
            raise UnlockRequired(self)
        proc = run_cli([str(self._op()), *args], env=self._env(token), timeout=_TIMEOUT, label="op",
                       timeout_message="op timed out", stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            err = _scrub(proc.stderr or "")
            if "session" in err.lower() or "sign in" in err.lower() or "not signed in" in err.lower():
                _unlock.lock(self.name)
                raise UnlockRequired(self)
            raise RuntimeError(f"op failed: {err[:200]}")
        return proc.stdout or ""

    def read_reference(self, reference: str) -> str:
        """One ``op://vault/item/field`` value (an email-code inbox's app password), resolved in process."""
        if not reference.startswith("op://"):
            raise ValueError("only op:// references can be read")
        return self._run("read", "--", reference).rstrip("\r\n")

    def _item_get(self, handle: str, *args: str) -> str:
        """``op item get`` scoped to the item's vault: service accounts refuse it without a vault query."""
        item_id = handle[len(self.prefix):]
        if item_id not in self._vaults:
            self.list_items()
        vault = self._vaults.get(item_id)
        return self._run("item", "get", item_id, *(("--vault", vault) if vault else ()), *args)

    # ── backend contract ───────────────────────────────────────────────────
    def list_items(self) -> list[VaultItemMeta]:
        if not self.is_unlocked():
            return []
        categories = "Login,Credit Card" if self.payment_cards else "Login"
        raw = json.loads(self._run("item", "list", "--categories", categories, "--format", "json") or "[]")
        out: list[VaultItemMeta] = []
        for item in raw if isinstance(raw, list) else []:
            item_id = str(item.get("id") or "")
            vault = item.get("vault")
            if isinstance(vault, dict) and vault.get("id"):
                self._vaults[item_id] = str(vault["id"])
            if str(item.get("category") or "").upper() == "CREDIT_CARD":
                if self.payment_cards:
                    out.append(VaultItemMeta(
                        id=f"{self.prefix}{item_id}", kind="payment", label=str(item.get("title") or "Card"),
                        origin=None, created_at=str(item.get("created_at") or ""), any_origin=True,
                        last4=card_last4_from_list(str(item.get("additional_information") or ""))))
                continue
            urls = [str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")]
            origins = _all_origins(urls)
            if not origins:
                continue
            username = str(item.get("additional_information") or "").strip() or None
            out.append(VaultItemMeta(
                id=f"{self.prefix}{item_id}", kind="login", label=str(item.get("title") or origins[0]),
                origin=origins[0], created_at=str(item.get("created_at") or ""),
                identifier_type="username" if username else None, identifier=username,
                allowed_origins=_web_origins(origins)))
        return out

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        return next((m for m in self.list_items() if m.id == handle), None)

    def resolve_password(self, handle: str) -> str:
        return self._item_get(handle, "--fields", "label=password", "--reveal").rstrip("\r\n")

    def resolve_otp(self, handle: str) -> Optional[str]:
        # `--otp` mints the current TOTP from the item's one-time-password field; items without one error out.
        try:
            code = self._item_get(handle, "--otp").strip()
        except Exception:
            return None
        return code if code.isdigit() else None

    def card_limit_text(self, handle: str) -> Optional[str]:
        """The Credit Card item's built-in "credit limit" (or a custom "Limit") field. Plain text fields only:
        ``--fields`` selects them by label, so no concealed value is ever read for this."""
        for label in ("credit limit", "Limit"):
            try:
                value = self._item_get(handle, "--fields", f"label={label}").strip()
            except Exception:
                continue
            if value:
                return value[:40]
        return None

    def resolve_secret(self, handle: str) -> dict[str, str]:
        """Card payload for a Credit Card item (server-side only); the password shape otherwise."""
        if not self.payment_cards:
            return super().resolve_secret(handle)
        item = json.loads(self._item_get(handle, "--format", "json", "--reveal") or "{}")
        if str(item.get("category") or "").upper() != "CREDIT_CARD":
            return super().resolve_secret(handle)
        return card_payload(item.get("fields") or [])


def card_last4_from_list(masked: str) -> Optional[str]:
    """Last four digits from 1Password's masked card subtitle (``"4111 **** 4821"``). An unmasked string is
    refused, so a full card number can never pass through here into metadata."""
    m = re.fullmatch(r"(?:\d{4}\s*)?[*•·]+\s*(\d{4})", (masked or "").strip())
    return m.group(1) if m else None


def card_payload(fields: list) -> dict[str, str]:
    """Map 1Password Credit Card fields to PAYMENT_FIELDS names. Raises with a value-free message
    when a required field is missing or the expiry cannot be parsed."""
    out: dict[str, str] = {}
    for f in fields:
        if not isinstance(f, dict):
            continue
        fid, value = str(f.get("id") or ""), str(f.get("value") or "").strip()
        if not value:
            continue
        if fid in _CARD_FIELD_IDS:
            out[_CARD_FIELD_IDS[fid]] = re.sub(r"[\s-]", "", value) if fid == "ccnum" else value
        elif fid == "expiry" and (parsed := parse_expiry(value)):
            out["exp_month"], out["exp_year"] = parsed
    missing = [k for k in ("card_number", "exp_month", "exp_year", "cvc") if k not in out]
    if missing:
        raise RuntimeError(f"1Password card item is missing {', '.join(missing)}")
    return out


def parse_expiry(value: str) -> Optional[tuple[str, str]]:
    """1Password MONTH_YEAR values: ``YYYYMM`` (CLI JSON), also ``MM/YYYY`` / ``MM/YY``. → (MM, YYYY)."""
    v = value.strip()
    if re.fullmatch(r"\d{6}", v):
        year, month = v[:4], v[4:]
    elif m := re.fullmatch(r"(\d{1,2})\s*/\s*(\d{2}|\d{4})", v):
        month, year = m.group(1), m.group(2)
        year = f"20{year}" if len(year) == 2 else year
    else:
        return None
    if not 1 <= int(month) <= 12:
        return None
    return f"{int(month):02d}", year


def _web_origins(origins: list[str]) -> tuple:
    """Fill targets are browser pages, so app URIs (``androidapp://`` etc.) never
    widen the fill set; an item whose only URI is an app URI keeps its single
    (unfillable-from-a-page) origin exactly as before."""
    web = tuple(o for o in origins if o.startswith(("http://", "https://")))
    return web or (origins[0],)


def _all_origins(urls: list[str]) -> list[str]:
    """Every normalized origin saved on the item, deduped, order preserved.

    A 1Password Login item can carry several websites; each of them is a place the
    user told 1Password the credential belongs, so all of them are valid fill targets.
    """
    out: list[str] = []
    for u in urls:
        try:
            origin = normalize_origin(u)
        except Exception:
            continue
        if origin not in out:
            out.append(origin)
    return out

"""Local Fernet vault as a login backend (the always-on default)."""

from __future__ import annotations

from typing import Dict, List, Optional

from agent.vault_backends.base import LoginBackend
from agent.vault_store import VaultItemMeta


def _store():
    # Late import: tests and callers patch ``agent.vault_store.get_vault_store``; binding it here
    # at call time keeps that facade name the single seam.
    from agent.vault_store import get_vault_store
    return get_vault_store()


class LocalLoginBackend(LoginBackend):
    name = "local"
    display_name = "Hermes vault"
    prefix = "vault_"

    def list_items(self) -> list[VaultItemMeta]:
        return _store().list_items()

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        return _store().get_meta(handle)

    def resolve_password(self, handle: str) -> str:
        return str(_store().resolve_secret(handle).get("password") or "")

    def resolve_otp(self, handle: str) -> Optional[str]:
        from agent.vault_store import totp_now
        seed = str(_store().resolve_secret(handle).get("otp_secret") or "")
        return totp_now(seed) if seed else None

    def resolve_secret(self, handle: str) -> dict[str, str]:
        return {k: str(v) for k, v in _store().resolve_secret(handle).items()}

    def card_last4(self, handle: str) -> Optional[str]:
        """The local vault stores the card encrypted with no masked subtitle: decrypt in process, keep only
        the last four digits, drop the rest immediately."""
        meta = self.get_meta(handle)
        if meta is None or meta.kind != "payment":
            return None
        digits = "".join(ch for ch in str(_store().resolve_secret(handle).get("card_number") or "") if ch.isdigit())
        return digits[-4:] if len(digits) >= 12 else None

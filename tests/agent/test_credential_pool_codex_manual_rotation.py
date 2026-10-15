"""An independent Codex account (``manual:device_code``) must survive a peer's rotation.

Codex refresh tokens are single-use. When two pool instances (two processes, or a named
profile borrowing the root pool) hold the same labeled row and one rotates it, the other
must adopt the persisted pair instead of POSTing the consumed refresh token. Replaying it
returns ``refresh_token_reused``, the row goes DEAD, and 24h later it is pruned: the
"my backup ChatGPT accounts vanished" incident.

The token endpoint is faked with genuine single-use semantics: a refresh token redeems once.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

import hermes_cli.auth as auth_mod
from agent.credential_pool import STATUS_DEAD, load_pool


def _jwt(account: str, sub: str, exp: float) -> str:
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    payload = {"exp": int(exp), "sub": sub, "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    return f"{seg({'alg': 'none'})}.{seg(payload)}.sig"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@pytest.fixture
def codex(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None  # type: ignore[attr-defined]

    past = time.time() - 3600
    singleton = {"access_token": _jwt("acct-owner", "sub-owner", time.time() + 864000), "refresh_token": "rt-owner"}
    (root / "auth.json").write_text(json.dumps({
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {"openai-codex": {"tokens": singleton, "last_refresh": _iso(past), "auth_mode": "chatgpt"}},
        "credential_pool": {"openai-codex": [
            {"id": "owner", "label": "owner", "auth_type": "oauth", "priority": 0,
             "source": "device_code", **singleton, "last_refresh": _iso(past)},
            {"id": "backup", "label": "BACKUP-Codex", "auth_type": "oauth", "priority": 1,
             "source": "manual:device_code",
             "access_token": _jwt("acct-backup", "sub-backup", past), "refresh_token": "rt-backup-0",
             "last_refresh": _iso(past - 864000)},
        ]},
    }), encoding="utf-8")

    server = {"valid": {"rt-owner", "rt-backup-0"}, "n": 0, "log": []}

    def fake_refresh(access_token, refresh_token):
        if refresh_token not in server["valid"]:
            server["log"].append(("REUSE", refresh_token))
            raise auth_mod.AuthError(
                "Codex refresh token was already consumed by another client",
                provider="openai-codex", code="refresh_token_reused", relogin_required=True)
        server["valid"].discard(refresh_token)
        server["n"] += 1
        new_rt = f"rt-backup-{server['n']}"
        server["valid"].add(new_rt)
        server["log"].append(("ROTATE", refresh_token, new_rt))
        return {"access_token": _jwt("acct-backup", "sub-backup", time.time() + 864000),
                "refresh_token": new_rt, "last_refresh": _iso(time.time())}

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", fake_refresh)

    def use(home):
        monkeypatch.setenv("HERMES_HOME", str(home))
        hermes_constants._default_hermes_root_memo = None  # type: ignore[attr-defined]
        auth_mod._global_auth_store_cache = None

    def backup_row(home=root):
        rows = json.loads((home / "auth.json").read_text())["credential_pool"]["openai-codex"]
        return next(r for r in rows if r["id"] == "backup")

    return {"root": root, "server": server, "use": use, "backup_row": backup_row}


def _backup(pool):
    return next(e for e in pool.entries() if e.id == "backup")


def test_stale_pool_adopts_peer_rotation_of_independent_account(codex):
    """Process A loaded the pool before process B rotated BACKUP; A must not replay rt-backup-0."""
    stale = load_pool("openai-codex")  # process A: long-lived, holds rt-backup-0 in memory
    peer = load_pool("openai-codex")   # process B
    assert peer.try_refresh_matching(credential_id="backup") is not None
    assert codex["backup_row"]()["refresh_token"] == "rt-backup-1"

    stale.try_refresh_matching(credential_id="backup")

    assert ("REUSE", "rt-backup-0") not in codex["server"]["log"], codex["server"]["log"]
    row = codex["backup_row"]()
    assert row.get("last_status") != STATUS_DEAD, row
    assert row["refresh_token"] in codex["server"]["valid"]


def test_profile_borrowing_root_pool_writes_rotation_back_to_root(codex, tmp_path):
    """A named profile with no Codex rows of its own rotates a root row; root must keep the new pair."""
    profile = codex["root"] / "profiles" / "vox"
    profile.mkdir(parents=True)
    (profile / "auth.json").write_text(json.dumps({"version": 1, "providers": {}, "credential_pool": {}}))

    codex["use"](profile)
    borrowed = load_pool("openai-codex")
    assert borrowed.try_refresh_matching(credential_id="backup") is not None

    codex["use"](codex["root"])
    assert codex["backup_row"]()["refresh_token"] == "rt-backup-1"
    root_pool = load_pool("openai-codex")
    root_pool.try_refresh_matching(credential_id="backup")
    assert ("REUSE", "rt-backup-0") not in codex["server"]["log"], codex["server"]["log"]

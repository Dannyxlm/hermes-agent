"""Card file grants: the files an accepted card shows stay fetchable by the app that draws it.

``show_card`` accepts a file this server can read (``validate.server_file_problem``: ``read_file``'s two
guards, a regular file, no secret-bearing name). The app then fetches it through the profile's authenticated
media route (``plugins/cloudseed_mobile`` ``workspace-files/download?media_path=``), which on its own serves
only Hermes's media roots (``images``, ``screenshots``, ``cache`` …). A card showing a workspace file therefore
passed validation and drew an error tile on the phone.

Accepting a card now records one grant per file it references, so *accepted* means *fetchable* without opening
a workspace, a home directory or any other root to the route: only the exact resolved files a card showed, each
re-checked against the validator's own predicate and opened without following a symlink at every fetch.

Layout, per profile: ``<HERMES_HOME>/card_file_grants/<sha256 of the resolved path>``, a 0600 file holding
that resolved path. No expiry, so an old chat's cards keep drawing; deleting the directory revokes every grant.
Cards accepted before grants existed get theirs from ``python -m tools.cards.file_grants backfill`` (read-only
over ``state.db``; each file is re-validated before it is granted).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

GRANT_DIR_NAME = "card_file_grants"

# Directory and file names whose contents are keys or credentials. The read guards already refuse Hermes's
# own credential stores, device/process files and project ``.env`` files; these cover the rest of the disk, so
# a fetch route that skips tool-output redaction never hands key material to a client.
_SECRET_DIRS = frozenset({".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store",
                          "mcp-tokens", "secrets", "credentials"})
_SECRET_FILES = frozenset({".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc", "credentials.json",
                           "auth.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".keystore", ".jks")


def secret_bearing(path: str) -> bool:
    """True when any component of *path* (resolved, absolute) names key or credential material."""
    names = [part.casefold() for part in Path(path).parts[1:]]
    if any(name in _SECRET_DIRS for name in names[:-1]):
        return True
    leaf = names[-1] if names else ""
    return leaf in _SECRET_FILES or leaf.endswith(_SECRET_SUFFIXES)


def grant_dir(home: Optional[Path] = None) -> Path:
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return Path(home) / GRANT_DIR_NAME


def _key(resolved: str) -> str:
    return hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()


def grant_files(paths: Iterable[str], *, home: Optional[Path] = None) -> list[str]:
    """Grant each absolute *path* (as resolved now); returns the resolved paths granted.

    Best effort per file: a write that fails is logged, and that one file stays unfetchable, as before."""
    granted: list[str] = []
    directory = grant_dir(home)
    for path in paths:
        if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
            continue
        resolved = os.path.realpath(path)
        target = directory / _key(resolved)
        try:
            if target.is_file() and target.read_text(encoding="utf-8-sig", errors="surrogatepass") == resolved:
                granted.append(resolved)
                continue
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".grant-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", errors="surrogatepass") as handle:
                    handle.write(resolved)
                os.replace(tmp, target)  # mkstemp made it 0600
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            granted.append(resolved)
        except OSError as exc:
            logger.warning("card file grant not recorded for %s: %s", resolved, exc)
    return granted


def grant_card_files(card: dict, *, home: Optional[Path] = None) -> list[str]:
    """Grant every file an accepted *card* references (``validate.card_files``)."""
    from tools.cards.validate import card_files
    return grant_files((value for _field, value in card_files(card)), home=home)


def is_granted(resolved: str, *, home: Optional[Path] = None) -> bool:
    target = grant_dir(home) / _key(resolved)
    try:
        st = os.lstat(target)
        if not stat.S_ISREG(st.st_mode):
            return False
        return target.read_text(encoding="utf-8-sig", errors="surrogatepass") == resolved
    except OSError:
        return False


def _open_without_symlinks(resolved: str) -> int:
    """A read-only fd for the regular file at *resolved*, walking from ``/`` with ``O_NOFOLLOW`` at every
    step, so a component swapped for a symlink after the check fails instead of being followed."""
    parts = Path(resolved).parts[1:]
    if not parts:
        raise PermissionError("not a file")
    cloexec = getattr(os, "O_CLOEXEC", 0)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | cloexec)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | cloexec | (os.O_NONBLOCK if last else os.O_DIRECTORY)
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PermissionError("not a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_granted_file(path: str, *, home: Optional[Path] = None) -> Optional[int]:
    """A read-only fd for *path* when an accepted card granted it, else None.

    The caller owns the fd. Re-applies the validator's predicate on every open, so a grant never outlives a
    tightened guard. Raises ``FileNotFoundError`` for a granted file that is gone (the route answers 404)
    and ``PermissionError`` when it is no longer a plain regular file reachable without a symlink."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path or len(path) > 4096:
        return None
    resolved = os.path.realpath(path)
    if not is_granted(resolved, home=home):
        return None
    from tools.cards.validate import server_file_problem
    problem = server_file_problem(resolved)
    if problem:
        if problem == "does not exist on this server":
            raise FileNotFoundError(resolved)
        raise PermissionError(problem)
    return _open_without_symlinks(resolved)


def _accepted_card_calls(db) -> Iterable[tuple[str, dict]]:
    """(tool call id, card) for every stored ``show_card`` call whose receipt says ``ok``."""
    receipts = db.execute("SELECT session_id, tool_call_id, content FROM messages "
                          "WHERE role = 'tool' AND tool_name = 'show_card'").fetchall()
    accepted: dict[str, set[str]] = {}
    for session_id, call_id, content in receipts:
        try:
            ok = isinstance(call_id, str) and json.loads(content or "").get("ok") is True
        except (ValueError, AttributeError):
            ok = False
        if ok:
            accepted.setdefault(session_id, set()).add(call_id)
    for session_id, call_ids in accepted.items():
        rows = db.execute("SELECT tool_calls FROM messages WHERE session_id = ? AND role = 'assistant' "
                          "AND tool_calls LIKE '%show_card%'", (session_id,))
        for (raw,) in rows:
            try:
                calls = json.loads(raw or "[]")
            except ValueError:
                continue
            for call in calls if isinstance(calls, list) else []:
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict) or function.get("name") != "show_card":
                    continue
                if call.get("id") not in call_ids and call.get("call_id") not in call_ids:
                    continue
                try:
                    card = json.loads(function.get("arguments") or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(card, dict):
                    yield call.get("id") or call.get("call_id"), card


def backfill(*, home: Optional[Path] = None, db_path: Optional[Path] = None) -> dict:
    """Grant the files of every card this profile already accepted. Read-only over ``state.db``; a file that
    fails today's validator (gone, unreadable, secret-bearing) is skipped, never granted."""
    from hermes_cli.sqlite_safe_read import connect_tracked
    from tools.cards.validate import card_files, server_file_problem
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    db_path = Path(db_path) if db_path is not None else Path(home) / "state.db"
    cards = granted = skipped = 0
    db = connect_tracked(db_path.resolve().as_uri() + "?mode=ro", tracking_path=db_path, uri=True, timeout=5)
    try:
        for _call_id, card in _accepted_card_calls(db):
            cards += 1
            for _field, path in card_files(card):
                if not os.path.isabs(path) or server_file_problem(path):
                    skipped += 1
                    continue
                granted += len(grant_files([path], home=home))
    finally:
        db.close()
    return {"cards": cards, "granted": granted, "skipped": skipped}


if __name__ == "__main__":  # python -m tools.cards.file_grants backfill
    import sys

    if sys.argv[1:] != ["backfill"]:
        sys.exit("usage: python -m tools.cards.file_grants backfill")
    print(json.dumps(backfill()))

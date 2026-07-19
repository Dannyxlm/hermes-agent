#!/usr/bin/env python3
"""Refusing updater for CloudSeed-managed Hermes releases.

The stock Hermes updater is intentionally unsuitable for a checkout carrying
reviewed private integration history because its divergence fallback can reset
the branch. This updater accepts one exact approval manifest, fetches only the
approved release tag, and advances the approved branch only by fast-forward.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator, Sequence


_MANIFEST_KEYS = frozenset({
    "schema_version",
    "release_remote",
    "release_url",
    "branch",
    "approved_tag",
    "approved_commit",
})
_TRUST_POLICY_KEYS = frozenset({
    "schema_version",
    "release_remote",
    "release_url",
    "branch",
    "approval_path",
    "approval_signature_path",
    "approval_keyring_path",
    "approval_sha256",
    "approval_owner_uid",
    "approval_issuer_fingerprint",
    "tag_signer_fingerprint",
})
_TRUST_POLICY_PATH = Path("/etc/hermes/managed-update-policy.json")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FINGERPRINT_RE = re.compile(r"^(?:[0-9A-F]{40}|[0-9A-F]{64})$")
_VALIDSIG_RE = re.compile(
    r"(?:^|\n)\[GNUPG:\] VALIDSIG ([0-9A-Fa-f]{40,64})(?:\s|$)"
)
_SAFE_GIT_COMMANDS = frozenset({
    "cat-file",
    "check-ref-format",
    "diff-files",
    "diff-index",
    "fetch",
    "merge-base",
    "read-tree",
    "remote",
    "rev-parse",
    "status",
    "symbolic-ref",
    "update-ref",
    "verify-tag",
})
_TRANSACTION_MARKER_NAME = "hermes-managed-release.transaction.json"
_TRANSACTION_MARKER_MAX_BYTES = 16 * 1024
_TRANSACTION_MARKER_KEYS = frozenset({
    "schema_version",
    "repo_path",
    "git_dir_device",
    "git_dir_inode",
    "branch_ref",
    "initial_head",
    "target_head",
    "trust_binding_sha256",
})


class UpdateRefused(RuntimeError):
    """A fail-closed precondition prevented the update."""


class UpdateFailed(RuntimeError):
    """An approved update began but did not complete as expected."""


def _normalize_fingerprint(value: str, *, field: str) -> str:
    normalized = str(value or "").replace(" ", "").upper()
    if not _FINGERPRINT_RE.fullmatch(normalized):
        raise UpdateRefused(f"{field} must be a full signing-key fingerprint")
    return normalized


def _read_protected_bytes(
    path: Path,
    *,
    expected_owner_uid: int,
    purpose: str,
    max_bytes: int = 64 * 1024,
) -> bytes:
    """Open a protected regular file once and read only from that descriptor."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise UpdateRefused(
            f"{purpose} could not be opened safely (symlink or access error): {exc}"
        ) from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UpdateRefused(f"{purpose} must be a regular file")
        if info.st_uid != expected_owner_uid:
            raise UpdateRefused(f"{purpose} owner is not the pinned owner")
        if stat.S_IMODE(info.st_mode) & 0o222:
            raise UpdateRefused(f"{purpose} must not be writable")
        if info.st_size <= 0 or info.st_size > max_bytes:
            raise UpdateRefused(f"{purpose} size is invalid")

        chunks = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise UpdateRefused(f"{purpose} changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise UpdateRefused(f"{purpose} grew while being read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _parse_validsig_fingerprint(output: str, *, purpose: str) -> str:
    fingerprints = {
        match.group(1).upper()
        for match in _VALIDSIG_RE.finditer(output or "")
    }
    if len(fingerprints) != 1:
        raise UpdateRefused(f"{purpose} did not report one unambiguous VALIDSIG")
    return next(iter(fingerprints))


def _verify_detached_approval_signature(
    approval_bytes: bytes,
    signature_bytes: bytes,
    keyring_bytes: bytes,
) -> str:
    """Verify the exact loaded approval bytes against a pinned keyring copy."""
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-managed-approval-") as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "approval.json"
            signature_path = tmp_path / "approval.sig"
            keyring_path = tmp_path / "approval-keyring.gpg"
            for path, payload in (
                (data_path, approval_bytes),
                (signature_path, signature_bytes),
                (keyring_path, keyring_bytes),
            ):
                path.write_bytes(payload)
                path.chmod(0o600)
            result = subprocess.run(
                [
                    "gpgv",
                    "--status-fd=1",
                    "--keyring",
                    str(keyring_path),
                    str(signature_path),
                    str(data_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateRefused(f"approval detached signature could not be verified: {exc}") from exc
    if result.returncode != 0:
        raise UpdateRefused("approval detached signature is invalid or untrusted")
    return _parse_validsig_fingerprint(
        (result.stdout or "") + "\n" + (result.stderr or ""),
        purpose="approval detached signature",
    )


@dataclass(frozen=True)
class TrustPolicy:
    """Root-owned trust anchors that an approval manifest cannot redefine."""

    release_remote: str
    release_url: str
    branch: str
    approval_path: Path
    approval_signature_path: Path
    approval_keyring_path: Path
    approval_sha256: str
    approval_owner_uid: int
    approval_issuer_fingerprint: str
    tag_signer_fingerprint: str

    @classmethod
    def load(
        cls,
        path: Path = _TRUST_POLICY_PATH,
        *,
        expected_owner_uid: int = 0,
    ) -> "TrustPolicy":
        raw_bytes = _read_protected_bytes(
            path,
            expected_owner_uid=expected_owner_uid,
            purpose="managed-update trust policy",
        )
        try:
            raw = json.loads(raw_bytes)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise UpdateRefused(f"managed-update trust policy is invalid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise UpdateRefused("managed-update trust policy must be a JSON object")
        unknown = set(raw) - _TRUST_POLICY_KEYS
        missing = _TRUST_POLICY_KEYS - set(raw)
        if unknown or missing:
            raise UpdateRefused(
                "managed-update trust policy fields rejected: "
                f"missing={','.join(sorted(missing))} "
                f"unknown={','.join(sorted(unknown))}"
            )
        if raw.get("schema_version") != 1:
            raise UpdateRefused("unsupported managed-update trust policy schema_version")
        if not isinstance(raw.get("approval_owner_uid"), int) or raw["approval_owner_uid"] < 0:
            raise UpdateRefused("trust policy approval_owner_uid is invalid")
        for field in ("release_remote", "release_url", "branch"):
            if not isinstance(raw.get(field), str) or not raw[field].strip():
                raise UpdateRefused(f"trust policy {field} must be non-empty")
        if not _REMOTE_RE.fullmatch(raw["release_remote"]):
            raise UpdateRefused("trust policy release_remote is invalid")
        digest = str(raw.get("approval_sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UpdateRefused("trust policy approval_sha256 is invalid")
        paths = {}
        for field in (
            "approval_path",
            "approval_signature_path",
            "approval_keyring_path",
        ):
            value = raw.get(field)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise UpdateRefused(f"trust policy {field} must be an absolute path")
            paths[field] = Path(value)
        return cls(
            release_remote=raw["release_remote"],
            release_url=raw["release_url"],
            branch=raw["branch"],
            approval_path=paths["approval_path"],
            approval_signature_path=paths["approval_signature_path"],
            approval_keyring_path=paths["approval_keyring_path"],
            approval_sha256=digest,
            approval_owner_uid=raw["approval_owner_uid"],
            approval_issuer_fingerprint=_normalize_fingerprint(
                raw["approval_issuer_fingerprint"],
                field="approval_issuer_fingerprint",
            ),
            tag_signer_fingerprint=_normalize_fingerprint(
                raw["tag_signer_fingerprint"],
                field="tag_signer_fingerprint",
            ),
        )


@dataclass(frozen=True)
class Approval:
    release_remote: str
    release_url: str
    branch: str
    approved_tag: str
    approved_commit: str

    @classmethod
    def from_bytes(cls, payload: bytes) -> "Approval":
        try:
            raw = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise UpdateRefused(f"approval manifest is invalid JSON: {exc}") from exc

        if not isinstance(raw, dict):
            raise UpdateRefused("approval manifest must be a JSON object")
        unknown = set(raw) - _MANIFEST_KEYS
        missing = _MANIFEST_KEYS - set(raw)
        if unknown or missing:
            details = []
            if missing:
                details.append(f"missing={','.join(sorted(missing))}")
            if unknown:
                details.append(f"unknown={','.join(sorted(unknown))}")
            raise UpdateRefused("approval manifest fields rejected: " + " ".join(details))
        if raw.get("schema_version") != 1:
            raise UpdateRefused("unsupported approval manifest schema_version")

        values = {
            key: raw.get(key)
            for key in _MANIFEST_KEYS - {"schema_version"}
        }
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise UpdateRefused("approval manifest string fields must be non-empty")
        if any("\n" in value or "\r" in value for value in values.values()):
            raise UpdateRefused("approval manifest string fields must be single-line")

        remote = values["release_remote"]
        commit = values["approved_commit"].lower()
        if not _REMOTE_RE.fullmatch(remote):
            raise UpdateRefused("approval release_remote is not a safe Git remote name")
        if not _COMMIT_RE.fullmatch(commit):
            raise UpdateRefused("approval approved_commit must be a full Git object digest")

        return cls(
            release_remote=remote,
            release_url=values["release_url"],
            branch=values["branch"],
            approved_tag=values["approved_tag"],
            approved_commit=commit,
        )


def load_approval_from_policy(
    policy: TrustPolicy,
    *,
    signature_verifier: Callable[[bytes, bytes, bytes], str] = (
        _verify_detached_approval_signature
    ),
) -> Approval:
    """Load one digest-registered, detached-signed approval from pinned paths."""
    owner = policy.approval_owner_uid
    approval_bytes = _read_protected_bytes(
        policy.approval_path,
        expected_owner_uid=owner,
        purpose="approval manifest",
    )
    actual_digest = hashlib.sha256(approval_bytes).hexdigest()
    if actual_digest != policy.approval_sha256:
        raise UpdateRefused("approval manifest digest is not registered by trust policy")
    signature_bytes = _read_protected_bytes(
        policy.approval_signature_path,
        expected_owner_uid=owner,
        purpose="approval detached signature",
    )
    keyring_bytes = _read_protected_bytes(
        policy.approval_keyring_path,
        expected_owner_uid=owner,
        purpose="approval issuer keyring",
        max_bytes=4 * 1024 * 1024,
    )
    signer = _normalize_fingerprint(
        signature_verifier(approval_bytes, signature_bytes, keyring_bytes),
        field="approval signer fingerprint",
    )
    if signer != _normalize_fingerprint(
        policy.approval_issuer_fingerprint,
        field="approval_issuer_fingerprint",
    ):
        raise UpdateRefused("approval signer does not match pinned approval issuer")

    approval = Approval.from_bytes(approval_bytes)
    if approval.release_remote != policy.release_remote:
        raise UpdateRefused("approval release remote does not match trust policy")
    if approval.release_url != policy.release_url:
        raise UpdateRefused("approval release URL does not match trust policy")
    if approval.branch != policy.branch:
        raise UpdateRefused("approval release branch does not match trust policy")
    return approval


class GitRepository:
    """Narrow Git command surface that cannot invoke reset-like operations."""

    def __init__(self, path: Path):
        self.path = path.resolve()

    def run(
        self,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if not args or args[0] not in _SAFE_GIT_COMMANDS:
            command = args[0] if args else "<empty>"
            raise UpdateRefused(f"managed updater prohibits Git command: {command}")
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "Git command failed").strip()
            raise UpdateRefused(f"git {args[0]} refused: {detail}")
        return result

    def text(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def head(self) -> str:
        return self.text("rev-parse", "--verify", "HEAD^{commit}").lower()

    def require_clean(self) -> None:
        if self.text("status", "--porcelain=v1", "--untracked-files=all"):
            raise UpdateRefused("checkout is dirty")

    def symbolic_head_ref(self) -> str:
        return self.text("symbolic-ref", "--quiet", "HEAD")

    def remote_urls(self, remote: str) -> list[str]:
        return [
            line.strip()
            for line in self.text("remote", "get-url", "--all", remote).splitlines()
            if line.strip()
        ]

    def git_path(self, name: str) -> Path:
        """Return a worktree-scoped Git administrative path without resolving it."""
        raw_path = Path(self.text("rev-parse", "--git-path", name))
        if raw_path.is_absolute():
            return raw_path
        return Path(os.path.abspath(self.path / raw_path))

    def git_dir_identity(self) -> tuple[int, int]:
        raw_path = Path(self.text("rev-parse", "--git-dir"))
        git_dir = raw_path if raw_path.is_absolute() else self.path / raw_path
        try:
            info = git_dir.stat()
        except OSError as exc:
            raise UpdateRefused("Git administrative directory is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise UpdateRefused("Git administrative path is not a directory")
        return int(info.st_dev), int(info.st_ino)

    def transaction_marker_path(self) -> Path:
        return self.git_path(_TRANSACTION_MARKER_NAME)

    @contextmanager
    def lock_managed_update(self) -> Iterator[None]:
        """Serialize this worktree with a kernel-released advisory lock."""
        lock_path = self.git_path("hermes-managed-release.lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise UpdateRefused(f"could not acquire managed release lock: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UpdateRefused("managed release lock is not a private regular file")
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise UpdateRefused("managed release lock ownership or mode is unsafe")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise UpdateRefused("another managed update is already running") from None
                raise UpdateRefused("managed release lock could not be acquired") from exc
            os.ftruncate(fd, 0)
            os.write(fd, f"managed-update pid={os.getpid()}\n".encode())
            os.fsync(fd)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def is_ancestor(self, older: str, newer: str) -> bool:
        result = self.run("merge-base", "--is-ancestor", older, newer, check=False)
        if result.returncode not in {0, 1}:
            detail = (result.stderr or result.stdout or "merge-base failed").strip()
            raise UpdateRefused(f"git merge-base refused: {detail}")
        return result.returncode == 0


@dataclass(frozen=True)
class UpdateResult:
    status: str
    initial_head: str
    target_head: str
    applied: bool


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _transaction_trust_binding(
    git: GitRepository,
    approval: Approval,
    trust_policy: TrustPolicy,
) -> str:
    """Fingerprint the protected approval and every relevant trust anchor."""
    git_device, git_inode = git.git_dir_identity()
    payload = {
        "repo_path": str(git.path),
        "git_dir_device": git_device,
        "git_dir_inode": git_inode,
        "approval": {
            "release_remote": approval.release_remote,
            "release_url": approval.release_url,
            "branch": approval.branch,
            "approved_tag": approval.approved_tag,
            "approved_commit": approval.approved_commit,
        },
        "trust_policy": {
            "release_remote": trust_policy.release_remote,
            "release_url": trust_policy.release_url,
            "branch": trust_policy.branch,
            "approval_path": str(trust_policy.approval_path),
            "approval_signature_path": str(trust_policy.approval_signature_path),
            "approval_keyring_path": str(trust_policy.approval_keyring_path),
            "approval_sha256": trust_policy.approval_sha256,
            "approval_owner_uid": trust_policy.approval_owner_uid,
            "approval_issuer_fingerprint": trust_policy.approval_issuer_fingerprint,
            "tag_signer_fingerprint": trust_policy.tag_signer_fingerprint,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _build_transaction_marker(
    git: GitRepository,
    approval: Approval,
    trust_policy: TrustPolicy,
    *,
    branch_ref: str,
    initial_head: str,
    target_head: str,
) -> dict[str, Any]:
    git_device, git_inode = git.git_dir_identity()
    return {
        "schema_version": 1,
        "repo_path": str(git.path),
        "git_dir_device": git_device,
        "git_dir_inode": git_inode,
        "branch_ref": branch_ref,
        "initial_head": initial_head,
        "target_head": target_head,
        "trust_binding_sha256": _transaction_trust_binding(
            git, approval, trust_policy
        ),
    }


def _read_transaction_marker(path: Path) -> dict[str, Any] | None:
    """Read one private marker without following a substituted symlink."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise UpdateRefused("managed update transaction marker is unsafe") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UpdateRefused("managed update transaction marker is not a regular file")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise UpdateRefused("managed update transaction marker ownership or mode is unsafe")
        if info.st_size <= 0 or info.st_size > _TRANSACTION_MARKER_MAX_BYTES:
            raise UpdateRefused("managed update transaction marker size is invalid")
        remaining = info.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise UpdateRefused("managed update transaction marker changed while read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise UpdateRefused("managed update transaction marker grew while read")
    finally:
        os.close(fd)

    try:
        marker = json.loads(b"".join(chunks))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateRefused("managed update transaction marker is invalid") from exc
    if not isinstance(marker, dict) or set(marker) != _TRANSACTION_MARKER_KEYS:
        raise UpdateRefused("managed update transaction marker fields are invalid")
    if marker.get("schema_version") != 1:
        raise UpdateRefused("managed update transaction marker version is unsupported")
    string_fields = (
        "repo_path",
        "branch_ref",
        "initial_head",
        "target_head",
        "trust_binding_sha256",
    )
    if any(not isinstance(marker.get(field), str) for field in string_fields):
        raise UpdateRefused("managed update transaction marker values are invalid")
    if not isinstance(marker.get("git_dir_device"), int) or not isinstance(
        marker.get("git_dir_inode"), int
    ):
        raise UpdateRefused("managed update transaction marker identity is invalid")
    if not _COMMIT_RE.fullmatch(marker["initial_head"]) or not _COMMIT_RE.fullmatch(
        marker["target_head"]
    ):
        raise UpdateRefused("managed update transaction marker commits are invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", marker["trust_binding_sha256"]):
        raise UpdateRefused("managed update transaction marker trust binding is invalid")
    return marker


def _write_transaction_marker(path: Path, marker: dict[str, Any]) -> None:
    """Atomically publish and durably fsync the pre-CAS recovery record."""
    encoded = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
    if not encoded or len(encoded) > _TRANSACTION_MARKER_MAX_BYTES:
        raise UpdateFailed("managed update transaction marker is too large")
    try:
        parent_info = path.parent.stat()
    except OSError as exc:
        raise UpdateFailed("managed update transaction directory is unavailable") from exc
    if not stat.S_ISDIR(parent_info.st_mode):
        raise UpdateFailed("managed update transaction directory is invalid")

    fd = -1
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=".hermes-managed-release.",
            suffix=".tmp",
        )
        os.fchmod(fd, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short transaction marker write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_name, path)
        tmp_name = None
        _fsync_directory(path.parent)
    except OSError as exc:
        raise UpdateFailed("managed update transaction marker could not be persisted") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _remove_transaction_marker(path: Path) -> None:
    try:
        os.unlink(path)
        _fsync_directory(path.parent)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UpdateFailed("managed update transaction marker could not be cleared") from exc


def _git_tree_matches_index(git: GitRepository, commit: str) -> bool:
    result = git.run("diff-index", "--quiet", "--cached", commit, "--", check=False)
    if result.returncode not in {0, 1}:
        raise UpdateFailed("managed update could not inspect the recovery index")
    return result.returncode == 0


def _recovery_worktree_matches_index(git: GitRepository) -> bool:
    result = git.run("diff-files", "--quiet", "--", check=False)
    if result.returncode not in {0, 1}:
        raise UpdateFailed("managed update could not inspect the recovery worktree")
    if result.returncode != 0:
        return False
    status = git.text("status", "--porcelain=v1", "--untracked-files=all")
    return not any(line.startswith("??") for line in status.splitlines())


def _recover_interrupted_update(
    git: GitRepository,
    approval: Approval,
    trust_policy: TrustPolicy,
    *,
    branch_ref: str,
) -> None:
    """Reconcile only the two trees bound by a trusted interrupted transaction."""
    marker_path = git.transaction_marker_path()
    marker = _read_transaction_marker(marker_path)
    if marker is None:
        return

    expected = _build_transaction_marker(
        git,
        approval,
        trust_policy,
        branch_ref=branch_ref,
        initial_head=marker["initial_head"],
        target_head=marker["target_head"],
    )
    if marker != expected or marker["target_head"] != approval.approved_commit:
        raise UpdateRefused("managed update transaction marker does not match approval")
    if git.symbolic_head_ref() != branch_ref:
        raise UpdateRefused("managed update recovery branch does not match approval")
    if git.remote_urls(trust_policy.release_remote) != [trust_policy.release_url]:
        raise UpdateRefused("managed update recovery remote does not match approval")

    initial_head = marker["initial_head"]
    target_head = marker["target_head"]
    for commit in (initial_head, target_head):
        if git.text("rev-parse", "--verify", f"{commit}^{{commit}}").lower() != commit:
            raise UpdateRefused("managed update recovery commit is unavailable")
    if initial_head == target_head or not git.is_ancestor(initial_head, target_head):
        raise UpdateRefused("managed update transaction ancestry is invalid")

    actual_head = git.head()
    if actual_head not in {initial_head, target_head}:
        raise UpdateFailed("managed update recovery found an unrelated branch head")
    index_is_initial = _git_tree_matches_index(git, initial_head)
    index_is_target = _git_tree_matches_index(git, target_head)
    if not index_is_initial and not index_is_target:
        raise UpdateFailed("managed update recovery index is ambiguous")
    if not _recovery_worktree_matches_index(git):
        raise UpdateFailed(
            "managed update recovery found unrelated or ambiguous dirty work"
        )

    if index_is_initial and index_is_target:
        # Empty/release-metadata-only commits legitimately have different OIDs
        # but the same tree. In that case the clean index matches both marker
        # boundaries and needs no materialization regardless of which side of
        # the ref CAS survived. Confirm the trees really are identical before
        # treating this as a coherent state; otherwise keep failing closed.
        initial_tree = git.text("rev-parse", "--verify", f"{initial_head}^{{tree}}")
        target_tree = git.text("rev-parse", "--verify", f"{target_head}^{{tree}}")
        if initial_tree != target_tree:
            raise UpdateFailed("managed update recovery index is ambiguous")
        index_head = actual_head
    else:
        index_head = initial_head if index_is_initial else target_head
    if index_head != actual_head:
        result = git.run(
            "read-tree",
            "-u",
            "-m",
            index_head,
            actual_head,
            check=False,
        )
        if result.returncode != 0:
            raise UpdateFailed("managed update recovery could not materialize branch HEAD")
    if git.head() != actual_head:
        raise UpdateFailed("managed update recovery branch changed while reconciling")
    try:
        git.require_clean()
    except UpdateRefused as exc:
        raise UpdateFailed("managed update recovery did not produce a clean checkout") from exc
    _remove_transaction_marker(marker_path)


def _validate_requested_target(
    approval: Approval,
    trust_policy: TrustPolicy,
    *,
    release_remote: str,
    target_tag: str,
    target_commit: str,
) -> None:
    if approval.release_remote != trust_policy.release_remote:
        raise UpdateRefused("approval release remote is outside pinned trust policy")
    if approval.release_url != trust_policy.release_url:
        raise UpdateRefused("approval release URL is outside pinned trust policy")
    if approval.branch != trust_policy.branch:
        raise UpdateRefused("approval branch is outside pinned trust policy")
    if release_remote != approval.release_remote:
        raise UpdateRefused("requested release remote is not approved")
    if target_tag != approval.approved_tag:
        raise UpdateRefused("requested release tag is not approved")
    if target_commit.lower() != approval.approved_commit:
        raise UpdateRefused("requested release digest is not approved")


def _require_update_bindings(
    git: GitRepository,
    *,
    branch_ref: str,
    initial_head: str,
    trust_policy: TrustPolicy,
) -> None:
    if git.symbolic_head_ref() != branch_ref:
        raise UpdateRefused("release branch changed during managed update")
    if git.head() != initial_head:
        raise UpdateRefused("HEAD changed during managed update")
    if git.remote_urls(trust_policy.release_remote) != [trust_policy.release_url]:
        raise UpdateRefused("release remote URL changed during managed update")


def _synchronize_checkout_to_head(
    git: GitRepository,
    *,
    known_worktree_tree: str,
) -> None:
    """Align a known-clean worktree snapshot with the branch's actual HEAD."""
    actual_head = git.head()
    if actual_head != known_worktree_tree:
        materialize = git.run(
            "read-tree",
            "-u",
            "-m",
            known_worktree_tree,
            actual_head,
            check=False,
        )
        if materialize.returncode != 0:
            detail = (
                materialize.stderr
                or materialize.stdout
                or "worktree synchronization failed"
            ).strip()
            raise UpdateFailed(
                "managed update could not synchronize the worktree to actual HEAD: "
                f"{detail}"
            )
    try:
        git.require_clean()
    except UpdateRefused as exc:
        raise UpdateFailed(
            "managed update refusal left a worktree that does not match actual HEAD"
        ) from exc


def managed_update(
    *,
    repo_path: Path,
    approval: Approval,
    trust_policy: TrustPolicy,
    release_remote: str,
    target_tag: str,
    target_commit: str,
    apply: bool,
) -> UpdateResult:
    """Validate and optionally fast-forward to one exact approved release."""
    _validate_requested_target(
        approval,
        trust_policy,
        release_remote=release_remote,
        target_tag=target_tag,
        target_commit=target_commit,
    )
    git = GitRepository(repo_path)

    top_level = Path(git.text("rev-parse", "--show-toplevel")).resolve()
    if top_level != git.path:
        raise UpdateRefused("--repo must name the exact Git worktree root")
    if git.run("check-ref-format", f"refs/tags/{approval.approved_tag}", check=False).returncode:
        raise UpdateRefused("approved tag is not a valid Git tag name")
    if git.run("check-ref-format", f"refs/heads/{approval.branch}", check=False).returncode:
        raise UpdateRefused("approved branch is not a valid Git branch name")
    branch_ref = f"refs/heads/{approval.branch}"
    lock_context = git.lock_managed_update() if apply else nullcontext()
    with lock_context:
        # Recovery runs under the same kernel lock as mutation and before the
        # ordinary clean-check: after a crash between ref CAS and read-tree,
        # Git correctly reports the old index/worktree as dirty against the
        # new HEAD. The durable marker lets us distinguish that exact state
        # from unrelated user edits without resetting either blindly.
        if apply:
            _recover_interrupted_update(
                git,
                approval,
                trust_policy,
                branch_ref=branch_ref,
            )

        initial_head = git.head()
        git.require_clean()
        if git.symbolic_head_ref() != branch_ref:
            raise UpdateRefused("checkout is not on the approved release branch")
        if git.remote_urls(approval.release_remote) != [trust_policy.release_url]:
            raise UpdateRefused("release remote URL does not exactly match approval")

        # Fetch only the approved tag into a private namespace. A local tag
        # with the same name cannot substitute for the object supplied by the
        # release remote, and fetching never moves HEAD.
        candidate_ref = f"refs/hermes-managed/candidates/{approval.approved_commit}"
        source_ref = f"refs/tags/{approval.approved_tag}"
        git.run(
            "fetch",
            "--no-tags",
            approval.release_remote,
            f"{source_ref}:{candidate_ref}",
        )

        if git.text("cat-file", "-t", candidate_ref) != "tag":
            raise UpdateRefused("approved release tag is lightweight or not annotated")
        signature = git.run("verify-tag", "--raw", candidate_ref, check=False)
        if signature.returncode != 0:
            raise UpdateRefused(
                "approved release tag is unsigned or its signature is untrusted"
            )
        tag_signer = _parse_validsig_fingerprint(
            (signature.stdout or "") + "\n" + (signature.stderr or ""),
            purpose="approved release tag signature",
        )
        if tag_signer != _normalize_fingerprint(
            trust_policy.tag_signer_fingerprint,
            field="tag_signer_fingerprint",
        ):
            raise UpdateRefused("approved release tag signer does not match pinned signer")
        resolved_target = git.text(
            "rev-parse",
            "--verify",
            f"{candidate_ref}^{{commit}}",
        ).lower()
        if resolved_target != approval.approved_commit:
            raise UpdateRefused("approved tag does not resolve to the approved digest")

        if initial_head != resolved_target and not git.is_ancestor(
            initial_head, resolved_target
        ):
            if git.is_ancestor(resolved_target, initial_head):
                raise UpdateRefused("checkout contains commits beyond the approved release")
            raise UpdateRefused("checkout and approved release have diverged")

        git.require_clean()
        if not apply or initial_head == resolved_target:
            _require_update_bindings(
                git,
                branch_ref=branch_ref,
                initial_head=initial_head,
                trust_policy=trust_policy,
            )
            status = "already_current" if initial_head == resolved_target else "approved"
            return UpdateResult(status, initial_head, resolved_target, False)

        _require_update_bindings(
            git,
            branch_ref=branch_ref,
            initial_head=initial_head,
            trust_policy=trust_policy,
        )
        git.require_clean()

        marker_path = git.transaction_marker_path()
        marker = _build_transaction_marker(
            git,
            approval,
            trust_policy,
            branch_ref=branch_ref,
            initial_head=initial_head,
            target_head=resolved_target,
        )
        _write_transaction_marker(marker_path, marker)

        # Move only the approved branch ref first, using the frozen initial OID
        # as a compare-and-swap guard. Until this succeeds the target tree is
        # never materialized into the checkout.
        move = git.run(
            "update-ref",
            "--no-deref",
            "-m",
            f"hermes-managed-release {approval.approved_tag}",
            branch_ref,
            resolved_target,
            initial_head,
            check=False,
        )
        if move.returncode != 0:
            _synchronize_checkout_to_head(
                git,
                known_worktree_tree=initial_head,
            )
            _remove_transaction_marker(marker_path)
            detail = (move.stderr or move.stdout or "branch compare-and-swap failed").strip()
            raise UpdateRefused(f"approved branch compare-and-swap refused: {detail}")

        # The CAS made symbolic HEAD resolve to the approved target while the
        # index/worktree still represent the clean initial tree. Materialize
        # only now; a failure rolls the branch ref back before returning.
        checkout = git.run(
            "read-tree",
            "-u",
            "-m",
            initial_head,
            resolved_target,
            check=False,
        )
        if checkout.returncode != 0:
            rollback = git.run(
                "update-ref",
                "--no-deref",
                "-m",
                "rollback-failed-hermes-materialization",
                branch_ref,
                initial_head,
                resolved_target,
                check=False,
            )
            if rollback.returncode != 0:
                _synchronize_checkout_to_head(
                    git,
                    known_worktree_tree=initial_head,
                )
                raise UpdateFailed(
                    "approved ref moved but failed materialization could not be "
                    "rolled back safely"
                )
            _synchronize_checkout_to_head(
                git,
                known_worktree_tree=initial_head,
            )
            _remove_transaction_marker(marker_path)
            detail = (
                checkout.stderr
                or checkout.stdout
                or "fast-forward materialization failed"
            ).strip()
            raise UpdateRefused(
                f"approved fast-forward materialization refused and ref rolled back: {detail}"
            )

        try:
            if git.symbolic_head_ref() != branch_ref:
                raise UpdateRefused("release branch changed after managed update")
            if git.remote_urls(trust_policy.release_remote) != [trust_policy.release_url]:
                raise UpdateRefused("release remote URL changed after managed update")
            final_head = git.head()
            if final_head != resolved_target:
                raise UpdateRefused("fast-forward did not reach the approved digest")
            git.require_clean()
        except UpdateRefused as exc:
            rollback = git.run(
                "update-ref",
                "--no-deref",
                "-m",
                "rollback-refused-hermes-managed-release",
                branch_ref,
                initial_head,
                resolved_target,
                check=False,
            )
            if rollback.returncode != 0:
                _synchronize_checkout_to_head(
                    git,
                    known_worktree_tree=resolved_target,
                )
                raise UpdateFailed(
                    f"{exc}; approved branch could not be rolled back safely"
                ) from exc
            _synchronize_checkout_to_head(
                git,
                known_worktree_tree=resolved_target,
            )
            _remove_transaction_marker(marker_path)
            raise UpdateRefused(f"{exc}; fast-forward rolled back") from exc
        _remove_transaction_marker(marker_path)
    return UpdateResult("updated", initial_head, final_head, True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--release-remote", required=True)
    parser.add_argument("--target-tag", required=True)
    parser.add_argument("--target-commit", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="fast-forward after all checks; default is verification only",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    print(payload.get("message") or payload.get("status") or "managed update")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    initial_head: str | None = None
    try:
        # The trust-policy location is a code contract, not a caller-selected
        # CLI input. The root-owned policy in turn pins the approval path,
        # digest, detached-signature issuer, release URL, branch, and tag key.
        trust_policy = TrustPolicy.load()
        approval = load_approval_from_policy(trust_policy)
        repo = GitRepository(args.repo)
        try:
            initial_head = repo.head()
        except UpdateRefused:
            initial_head = None
        result = managed_update(
            repo_path=args.repo,
            approval=approval,
            trust_policy=trust_policy,
            release_remote=args.release_remote,
            target_tag=args.target_tag,
            target_commit=args.target_commit,
            apply=args.apply,
        )
    except UpdateRefused as exc:
        _emit(
            {
                "ok": False,
                "status": "refused",
                "message": str(exc),
                "head": initial_head,
            },
            as_json=args.as_json,
        )
        return 2
    except UpdateFailed as exc:
        _emit(
            {
                "ok": False,
                "status": "failed",
                "message": str(exc),
                "head": initial_head,
            },
            as_json=args.as_json,
        )
        return 1

    _emit(
        {
            "ok": True,
            "status": result.status,
            "message": (
                f"managed update {result.status}: {result.target_head}"
            ),
            "initial_head": result.initial_head,
            "target_head": result.target_head,
            "applied": result.applied,
        },
        as_json=args.as_json,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

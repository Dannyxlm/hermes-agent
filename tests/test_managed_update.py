"""Behavioral tests for the refusing CloudSeed-managed updater."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from scripts import managed_update


_REMOTE = "cloudseed-release"
_TAG = "ava/memory-v3-readonly-core/2026-07-19.1"
_APPROVAL_SIGNER = "A" * 40
_TAG_SIGNER = "B" * 40


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class _ReleaseRepo:
    checkout: Path
    remote: Path
    approval_file: Path
    approval_signature_file: Path
    approval_keyring_file: Path
    trust_policy: managed_update.TrustPolicy
    base_commit: str
    target_commit: str

    def head(self) -> str:
        return _git(self.checkout, "rev-parse", "HEAD")

    def approval(
        self,
        *,
        policy: managed_update.TrustPolicy | None = None,
        signer: str = _APPROVAL_SIGNER,
    ) -> managed_update.Approval:
        return managed_update.load_approval_from_policy(
            policy or self.trust_policy,
            signature_verifier=lambda *_args: signer,
        )

    def update(self, *, apply: bool = True, **overrides):
        values = {
            "release_remote": _REMOTE,
            "target_tag": _TAG,
            "target_commit": self.target_commit,
        }
        values.update(overrides)
        return managed_update.managed_update(
            repo_path=self.checkout,
            approval=self.approval(),
            trust_policy=self.trust_policy,
            apply=apply,
            **values,
        )


@pytest.fixture()
def release_repo(tmp_path: Path) -> _ReleaseRepo:
    remote = tmp_path / "release.git"
    checkout = tmp_path / "checkout"
    remote.mkdir()
    checkout.mkdir()
    _git(remote, "init", "--bare")
    _git(checkout, "init", "--initial-branch=main")
    _git(checkout, "config", "user.name", "Managed Update Test")
    _git(checkout, "config", "user.email", "managed-update@example.invalid")

    tracked = checkout / "release.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git(checkout, "add", "release.txt")
    _git(checkout, "commit", "-m", "base")
    base_commit = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "remote", "add", _REMOTE, str(remote))
    _git(checkout, "push", "-u", _REMOTE, "main")

    tracked.write_text("approved target\n", encoding="utf-8")
    _git(checkout, "commit", "-am", "approved target")
    target_commit = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "tag", "-a", _TAG, "-m", "approved release")
    _git(checkout, "push", _REMOTE, "main", f"refs/tags/{_TAG}")

    # Production-like checkout starts behind the published candidate. Tests
    # exercise only the updater from here; this fixture reset is isolated to
    # the disposable repository.
    _git(checkout, "reset", "--hard", base_commit)

    approval_file = tmp_path / "approval.json"
    approval_bytes = json.dumps({
            "schema_version": 1,
            "release_remote": _REMOTE,
            "release_url": str(remote),
            "branch": "main",
            "approved_tag": _TAG,
            "approved_commit": target_commit,
        }).encode()
    approval_file.write_bytes(approval_bytes)
    approval_file.chmod(0o400)
    approval_signature_file = tmp_path / "approval.json.sig"
    approval_signature_file.write_bytes(b"detached-signature")
    approval_signature_file.chmod(0o400)
    approval_keyring_file = tmp_path / "approval-keyring.gpg"
    approval_keyring_file.write_bytes(b"test-keyring")
    approval_keyring_file.chmod(0o400)
    trust_policy = managed_update.TrustPolicy(
        release_remote=_REMOTE,
        release_url=str(remote),
        branch="main",
        approval_path=approval_file,
        approval_signature_path=approval_signature_file,
        approval_keyring_path=approval_keyring_file,
        approval_sha256=hashlib.sha256(approval_bytes).hexdigest(),
        approval_owner_uid=os.geteuid(),
        approval_issuer_fingerprint=_APPROVAL_SIGNER,
        tag_signer_fingerprint=_TAG_SIGNER,
    )
    return _ReleaseRepo(
        checkout=checkout,
        remote=remote,
        approval_file=approval_file,
        approval_signature_file=approval_signature_file,
        approval_keyring_file=approval_keyring_file,
        trust_policy=trust_policy,
        base_commit=base_commit,
        target_commit=target_commit,
    )


@pytest.fixture()
def trust_test_tag(monkeypatch):
    """Treat the disposable annotated tag as signed after shape verification.

    Production never takes this path: ``GitRepository.run`` executes real
    ``git verify-tag --raw``. The fixture isolates refusal tests from developer
    GPG keyrings while the unsigned-tag test below exercises the real failure.
    """
    original = managed_update.GitRepository.run

    def _run(self, *args, **kwargs):
        if args and args[0] == "verify-tag":
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr=f"[GNUPG:] VALIDSIG {_TAG_SIGNER} 2026-07-19 0 4 0 1 10 00 {_TAG_SIGNER}\n",
            )
        return original(self, *args, **kwargs)

    monkeypatch.setattr(managed_update.GitRepository, "run", _run)


def test_apply_fast_forwards_to_exact_approved_digest(release_repo, trust_test_tag):
    result = release_repo.update(apply=True)

    assert result.status == "updated"
    assert result.applied is True
    assert release_repo.head() == release_repo.target_commit


def test_check_only_never_moves_head(release_repo, trust_test_tag):
    before = release_repo.head()

    result = release_repo.update(apply=False)

    assert result.status == "approved"
    assert result.applied is False
    assert release_repo.head() == before


def test_dirty_checkout_is_refused_without_moving_head(release_repo, trust_test_tag):
    before = release_repo.head()
    (release_repo.checkout / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(managed_update.UpdateRefused, match="dirty"):
        release_repo.update()

    assert release_repo.head() == before


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"target_tag": "ava/not-approved"}, "tag is not approved"),
        ({"target_commit": "0" * 40}, "digest is not approved"),
        ({"release_remote": "origin"}, "remote is not approved"),
    ],
)
def test_unapproved_target_is_refused_without_moving_head(
    release_repo, override, message
):
    before = release_repo.head()

    with pytest.raises(managed_update.UpdateRefused, match=message):
        release_repo.update(**override)

    assert release_repo.head() == before


def test_release_remote_url_mismatch_is_refused_without_fetch(
    release_repo, trust_test_tag, tmp_path
):
    before = release_repo.head()
    other = tmp_path / "other.git"
    other.mkdir()
    _git(other, "init", "--bare")
    _git(release_repo.checkout, "remote", "set-url", _REMOTE, str(other))

    with pytest.raises(managed_update.UpdateRefused, match="URL"):
        release_repo.update()

    assert release_repo.head() == before


def test_unsigned_annotated_tag_is_refused_without_moving_head(release_repo):
    before = release_repo.head()

    with pytest.raises(managed_update.UpdateRefused, match="unsigned|untrusted"):
        release_repo.update()

    assert release_repo.head() == before


def test_unpublished_commits_beyond_release_are_refused(
    release_repo, trust_test_tag
):
    release_repo.update()
    tracked = release_repo.checkout / "release.txt"
    tracked.write_text("local-only\n", encoding="utf-8")
    _git(release_repo.checkout, "commit", "-am", "local only")
    before = release_repo.head()

    with pytest.raises(managed_update.UpdateRefused, match="beyond"):
        release_repo.update()

    assert release_repo.head() == before


def test_divergent_checkout_is_refused_without_moving_head(
    release_repo, trust_test_tag
):
    tracked = release_repo.checkout / "release.txt"
    tracked.write_text("divergent local\n", encoding="utf-8")
    _git(release_repo.checkout, "commit", "-am", "divergent local")
    before = release_repo.head()

    with pytest.raises(managed_update.UpdateRefused, match="diverged"):
        release_repo.update()

    assert release_repo.head() == before


def test_git_wrapper_refuses_reset_command(release_repo):
    before = release_repo.head()
    git = managed_update.GitRepository(release_repo.checkout)

    with pytest.raises(managed_update.UpdateRefused, match="prohibits"):
        git.run("reset", "--hard", release_repo.target_commit)

    assert release_repo.head() == before


def test_manifest_unknown_fields_fail_closed(release_repo):
    release_repo.approval_file.chmod(0o600)
    manifest = json.loads(release_repo.approval_file.read_text(encoding="utf-8"))
    manifest["allow_reset"] = True
    changed = json.dumps(manifest).encode()
    release_repo.approval_file.write_bytes(changed)
    release_repo.approval_file.chmod(0o400)
    policy = replace(
        release_repo.trust_policy,
        approval_sha256=hashlib.sha256(changed).hexdigest(),
    )

    with pytest.raises(managed_update.UpdateRefused, match="unknown=allow_reset"):
        release_repo.approval(policy=policy)


def test_writable_approval_manifest_is_refused(release_repo):
    release_repo.approval_file.chmod(0o600)

    with pytest.raises(managed_update.UpdateRefused, match="writable"):
        release_repo.approval()


def test_symlink_approval_manifest_is_refused(release_repo):
    real_manifest = release_repo.approval_file.with_name("approval-real.json")
    release_repo.approval_file.rename(real_manifest)
    release_repo.approval_file.symlink_to(real_manifest)

    with pytest.raises(managed_update.UpdateRefused, match="symlink|open"):
        release_repo.approval()


def test_approval_digest_is_registered_outside_manifest(release_repo):
    policy = replace(release_repo.trust_policy, approval_sha256="0" * 64)

    with pytest.raises(managed_update.UpdateRefused, match="digest"):
        release_repo.approval(policy=policy)


def test_wrong_approval_issuer_is_refused(release_repo):
    with pytest.raises(managed_update.UpdateRefused, match="approval signer"):
        release_repo.approval(signer="C" * 40)


def test_release_url_is_pinned_outside_approval_manifest(release_repo):
    release_repo.approval_file.chmod(0o600)
    manifest = json.loads(release_repo.approval_file.read_text(encoding="utf-8"))
    manifest["release_url"] = "/attacker/release.git"
    changed = json.dumps(manifest).encode()
    release_repo.approval_file.write_bytes(changed)
    release_repo.approval_file.chmod(0o400)
    policy = replace(
        release_repo.trust_policy,
        approval_sha256=hashlib.sha256(changed).hexdigest(),
    )

    with pytest.raises(managed_update.UpdateRefused, match="release URL"):
        release_repo.approval(policy=policy)


def test_manifest_path_swap_after_open_does_not_change_parsed_approval(release_repo):
    original = release_repo.approval_file.with_name("approval-original.json")

    def _swap_after_read(*_args):
        release_repo.approval_file.rename(original)
        release_repo.approval_file.write_text(
            json.dumps({"schema_version": 1, "release_url": "/attacker"}),
            encoding="utf-8",
        )
        return _APPROVAL_SIGNER

    approval = managed_update.load_approval_from_policy(
        release_repo.trust_policy,
        signature_verifier=_swap_after_read,
    )

    assert approval.approved_commit == release_repo.target_commit
    assert approval.release_url == str(release_repo.remote)


def test_wrong_tag_signer_is_refused(release_repo, monkeypatch):
    original = managed_update.GitRepository.run

    def _run(self, *args, **kwargs):
        if args and args[0] == "verify-tag":
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"[GNUPG:] VALIDSIG {'C' * 40} 2026-07-19 0 4 0 1 10 00 {'C' * 40}\n",
                stderr="",
            )
        return original(self, *args, **kwargs)

    monkeypatch.setattr(managed_update.GitRepository, "run", _run)

    with pytest.raises(managed_update.UpdateRefused, match="tag signer"):
        release_repo.update()


def test_same_oid_branch_switch_race_is_refused_before_any_branch_moves(
    release_repo, trust_test_tag, monkeypatch
):
    _git(release_repo.checkout, "branch", "same-oid-race")
    original = managed_update.GitRepository.require_clean
    checks = 0

    def _switch_on_locked_recheck(self):
        nonlocal checks
        checks += 1
        result = original(self)
        if checks == 2:
            _git(release_repo.checkout, "checkout", "same-oid-race")
        return result

    monkeypatch.setattr(
        managed_update.GitRepository,
        "require_clean",
        _switch_on_locked_recheck,
    )

    with pytest.raises(managed_update.UpdateRefused, match="branch changed"):
        release_repo.update()

    assert _git(release_repo.checkout, "rev-parse", "main") == release_repo.base_commit
    assert (
        _git(release_repo.checkout, "rev-parse", "same-oid-race")
        == release_repo.base_commit
    )


def test_remote_change_after_ref_move_rolls_back_exact_fast_forward(
    release_repo, trust_test_tag, tmp_path, monkeypatch
):
    other = tmp_path / "other-after-move.git"
    other.mkdir()
    _git(other, "init", "--bare")
    original = managed_update.GitRepository.remote_urls
    reads = 0

    def _change_on_post_move(self, remote):
        nonlocal reads
        reads += 1
        if reads == 3:
            _git(self.path, "remote", "set-url", remote, str(other))
        return original(self, remote)

    monkeypatch.setattr(
        managed_update.GitRepository,
        "remote_urls",
        _change_on_post_move,
    )

    with pytest.raises(managed_update.UpdateRefused, match="remote URL changed.*rolled back"):
        release_repo.update()

    assert release_repo.head() == release_repo.base_commit
    assert (release_repo.checkout / "release.txt").read_text(encoding="utf-8") == "base\n"


def test_competing_branch_ref_move_refuses_and_materializes_actual_head(
    release_repo, trust_test_tag, monkeypatch
):
    _git(release_repo.checkout, "checkout", "-b", "competing-release")
    tracked = release_repo.checkout / "release.txt"
    tracked.write_text("competing release\n", encoding="utf-8")
    _git(release_repo.checkout, "commit", "-am", "competing release")
    competing_commit = _git(release_repo.checkout, "rev-parse", "HEAD")
    _git(release_repo.checkout, "checkout", "main")

    original = managed_update.GitRepository.run
    injected = False

    def _move_ref_before_approved_cas(self, *args, **kwargs):
        nonlocal injected
        if (
            not injected
            and args
            and args[0] == "update-ref"
            and "refs/heads/main" in args
            and release_repo.target_commit in args
        ):
            injected = True
            _git(
                release_repo.checkout,
                "update-ref",
                "refs/heads/main",
                competing_commit,
                release_repo.base_commit,
            )
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        managed_update.GitRepository,
        "run",
        _move_ref_before_approved_cas,
    )

    with pytest.raises(managed_update.UpdateRefused, match="compare-and-swap"):
        release_repo.update()

    assert release_repo.head() == competing_commit
    assert tracked.read_text(encoding="utf-8") == "competing release\n"
    assert _git(release_repo.checkout, "status", "--porcelain") == ""


def test_materialization_failure_occurs_after_cas_and_rolls_ref_back(
    release_repo, trust_test_tag, monkeypatch
):
    original = managed_update.GitRepository.run
    events = []

    def _fail_materialization(self, *args, **kwargs):
        if (
            args
            and args[0] == "update-ref"
            and "refs/heads/main" in args
            and release_repo.target_commit in args
        ):
            events.append("cas")
        if args and args[0] == "read-tree" and "materialize" not in events:
            events.append("materialize")
            return subprocess.CompletedProcess(
                args=list(args),
                returncode=1,
                stdout="",
                stderr="forced materialization failure",
            )
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        managed_update.GitRepository,
        "run",
        _fail_materialization,
    )

    with pytest.raises(managed_update.UpdateRefused, match="materialization"):
        release_repo.update()

    assert events[:2] == ["cas", "materialize"]
    assert release_repo.head() == release_repo.base_commit
    assert (release_repo.checkout / "release.txt").read_text(encoding="utf-8") == "base\n"
    assert _git(release_repo.checkout, "status", "--porcelain") == ""


def test_stale_kernel_lock_file_does_not_block_next_update(
    release_repo, trust_test_tag
):
    git = managed_update.GitRepository(release_repo.checkout)
    lock_path = git.git_path("hermes-managed-release.lock")
    lock_path.write_text("stale pid from killed updater\n", encoding="utf-8")
    lock_path.chmod(0o600)

    result = release_repo.update()

    assert result.status == "updated"
    assert release_repo.head() == release_repo.target_commit
    # The inode is intentionally stable; kernel flock ownership, not file
    # existence or stale PID content, controls exclusivity.
    assert lock_path.is_file()


def test_symlink_transaction_marker_is_refused_without_mutation(
    release_repo, trust_test_tag, tmp_path
):
    git = managed_update.GitRepository(release_repo.checkout)
    marker_path = git.transaction_marker_path()
    attacker_file = tmp_path / "attacker-marker.json"
    attacker_file.write_text("{}", encoding="utf-8")
    marker_path.symlink_to(attacker_file)

    with pytest.raises(managed_update.UpdateRefused, match="marker is unsafe"):
        release_repo.update()

    assert release_repo.head() == release_repo.base_commit
    assert marker_path.is_symlink()
    assert attacker_file.read_text(encoding="utf-8") == "{}"


def test_mismatched_stale_transaction_marker_fails_closed(
    release_repo, trust_test_tag
):
    git = managed_update.GitRepository(release_repo.checkout)
    marker = managed_update._build_transaction_marker(
        git,
        release_repo.approval(),
        release_repo.trust_policy,
        branch_ref="refs/heads/main",
        initial_head=release_repo.base_commit,
        target_head=release_repo.target_commit,
    )
    marker["trust_binding_sha256"] = "0" * 64
    managed_update._write_transaction_marker(git.transaction_marker_path(), marker)

    with pytest.raises(managed_update.UpdateRefused, match="does not match approval"):
        release_repo.update()

    assert release_repo.head() == release_repo.base_commit
    assert (release_repo.checkout / "release.txt").read_text(encoding="utf-8") == "base\n"


@pytest.mark.parametrize(
    ("boundary", "expected_status"),
    [
        ("before_cas", "updated"),
        ("after_materialization", "already_current"),
    ],
)
def test_valid_stale_marker_recovers_coherent_transaction_boundaries(
    release_repo, trust_test_tag, boundary, expected_status
):
    git = managed_update.GitRepository(release_repo.checkout)
    marker = managed_update._build_transaction_marker(
        git,
        release_repo.approval(),
        release_repo.trust_policy,
        branch_ref="refs/heads/main",
        initial_head=release_repo.base_commit,
        target_head=release_repo.target_commit,
    )
    marker_path = git.transaction_marker_path()
    managed_update._write_transaction_marker(marker_path, marker)
    if boundary == "after_materialization":
        _git(
            release_repo.checkout,
            "update-ref",
            "refs/heads/main",
            release_repo.target_commit,
            release_repo.base_commit,
        )
        _git(
            release_repo.checkout,
            "read-tree",
            "-u",
            "-m",
            release_repo.base_commit,
            release_repo.target_commit,
        )

    result = release_repo.update()

    assert result.status == expected_status
    assert release_repo.head() == release_repo.target_commit
    assert (release_repo.checkout / "release.txt").read_text(encoding="utf-8") == "approved target\n"
    assert _git(release_repo.checkout, "status", "--porcelain") == ""
    assert not marker_path.exists()


@pytest.mark.parametrize(
    ("after_cas", "expected_status"),
    [(False, "updated"), (True, "already_current")],
)
def test_stale_marker_recovers_same_tree_release_commit(
    release_repo, trust_test_tag, after_cas, expected_status
):
    """An empty approved commit is coherent on either side of the ref CAS."""
    _git(release_repo.checkout, "commit", "--allow-empty", "-m", "approved empty target")
    target_commit = _git(release_repo.checkout, "rev-parse", "HEAD")
    assert _git(
        release_repo.checkout,
        "rev-parse",
        f"{release_repo.base_commit}^{{tree}}",
    ) == _git(
        release_repo.checkout,
        "rev-parse",
        f"{target_commit}^{{tree}}",
    )
    _git(release_repo.checkout, "tag", "-f", "-a", _TAG, "-m", "approved empty release")
    _git(release_repo.checkout, "push", "--force", _REMOTE, f"refs/tags/{_TAG}")
    _git(release_repo.checkout, "reset", "--hard", release_repo.base_commit)

    approval_bytes = json.dumps(
        {
            "schema_version": 1,
            "release_remote": _REMOTE,
            "release_url": str(release_repo.remote),
            "branch": "main",
            "approved_tag": _TAG,
            "approved_commit": target_commit,
        }
    ).encode()
    release_repo.approval_file.chmod(0o600)
    release_repo.approval_file.write_bytes(approval_bytes)
    release_repo.approval_file.chmod(0o400)
    same_tree_repo = replace(
        release_repo,
        target_commit=target_commit,
        trust_policy=replace(
            release_repo.trust_policy,
            approval_sha256=hashlib.sha256(approval_bytes).hexdigest(),
        ),
    )

    git = managed_update.GitRepository(same_tree_repo.checkout)
    marker = managed_update._build_transaction_marker(
        git,
        same_tree_repo.approval(),
        same_tree_repo.trust_policy,
        branch_ref="refs/heads/main",
        initial_head=same_tree_repo.base_commit,
        target_head=same_tree_repo.target_commit,
    )
    marker_path = git.transaction_marker_path()
    managed_update._write_transaction_marker(marker_path, marker)
    if after_cas:
        _git(
            same_tree_repo.checkout,
            "update-ref",
            "refs/heads/main",
            same_tree_repo.target_commit,
            same_tree_repo.base_commit,
        )

    result = same_tree_repo.update()

    assert result.status == expected_status
    assert same_tree_repo.head() == same_tree_repo.target_commit
    assert _git(same_tree_repo.checkout, "status", "--porcelain") == ""
    assert not marker_path.exists()


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="requires POSIX SIGKILL")
@pytest.mark.parametrize(
    "death_signal",
    [signal.SIGTERM, signal.SIGKILL],
    ids=["sigterm", "sigkill"],
)
def test_process_death_after_ref_cas_is_recovered_by_next_run(
    release_repo, trust_test_tag, death_signal
):
    payload = {
        "repo": str(release_repo.checkout),
        "remote": str(release_repo.remote),
        "approval_path": str(release_repo.approval_file),
        "approval_signature_path": str(release_repo.approval_signature_file),
        "approval_keyring_path": str(release_repo.approval_keyring_file),
        "approval_sha256": release_repo.trust_policy.approval_sha256,
        "owner_uid": os.geteuid(),
        "base": release_repo.base_commit,
        "target": release_repo.target_commit,
        "death_signal": death_signal,
    }
    child = r'''
import json
import os
import signal
import subprocess
from pathlib import Path
from scripts import managed_update as updater

p = json.loads(os.environ["HERMES_MANAGED_UPDATE_TEST"])
approval = updater.Approval(
    release_remote="cloudseed-release",
    release_url=p["remote"],
    branch="main",
    approved_tag="ava/memory-v3-readonly-core/2026-07-19.1",
    approved_commit=p["target"],
)
policy = updater.TrustPolicy(
    release_remote="cloudseed-release",
    release_url=p["remote"],
    branch="main",
    approval_path=Path(p["approval_path"]),
    approval_signature_path=Path(p["approval_signature_path"]),
    approval_keyring_path=Path(p["approval_keyring_path"]),
    approval_sha256=p["approval_sha256"],
    approval_owner_uid=p["owner_uid"],
    approval_issuer_fingerprint="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    tag_signer_fingerprint="BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
)
original_run = updater.GitRepository.run

def run_then_die(self, *args, **kwargs):
    if args and args[0] == "verify-tag":
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="",
            stderr="[GNUPG:] VALIDSIG " + "B" * 40 + " 2026-07-19 0 4 0 1 10 00 " + "B" * 40 + "\n",
        )
    result = original_run(self, *args, **kwargs)
    if (
        args
        and args[0] == "update-ref"
        and "refs/heads/main" in args
        and p["target"] in args
        and result.returncode == 0
    ):
        os.kill(os.getpid(), p["death_signal"])
    return result

updater.GitRepository.run = run_then_die
updater.managed_update(
    repo_path=Path(p["repo"]),
    approval=approval,
    trust_policy=policy,
    release_remote="cloudseed-release",
    target_tag=approval.approved_tag,
    target_commit=p["target"],
    apply=True,
)
'''
    env = dict(os.environ)
    env["HERMES_MANAGED_UPDATE_TEST"] = json.dumps(payload)
    killed = subprocess.run(
        [sys.executable, "-c", child],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    git = managed_update.GitRepository(release_repo.checkout)
    marker_path = git.transaction_marker_path()
    assert killed.returncode == -death_signal, killed.stderr
    assert release_repo.head() == release_repo.target_commit
    assert (release_repo.checkout / "release.txt").read_text(encoding="utf-8") == "base\n"
    assert marker_path.is_file()

    recovered = release_repo.update()

    assert recovered.status == "already_current"
    assert release_repo.head() == release_repo.target_commit
    assert (release_repo.checkout / "release.txt").read_text(encoding="utf-8") == "approved target\n"
    assert _git(release_repo.checkout, "status", "--porcelain") == ""
    assert not marker_path.exists()


def test_recovery_never_resets_unrelated_dirty_work(release_repo, trust_test_tag):
    git = managed_update.GitRepository(release_repo.checkout)
    marker = managed_update._build_transaction_marker(
        git,
        release_repo.approval(),
        release_repo.trust_policy,
        branch_ref="refs/heads/main",
        initial_head=release_repo.base_commit,
        target_head=release_repo.target_commit,
    )
    marker_path = git.transaction_marker_path()
    managed_update._write_transaction_marker(marker_path, marker)
    _git(
        release_repo.checkout,
        "update-ref",
        "refs/heads/main",
        release_repo.target_commit,
        release_repo.base_commit,
    )
    unrelated = release_repo.checkout / "do-not-reset.txt"
    unrelated.write_text("user work\n", encoding="utf-8")

    with pytest.raises(managed_update.UpdateFailed, match="ambiguous dirty work"):
        release_repo.update()

    assert unrelated.read_text(encoding="utf-8") == "user work\n"
    assert (release_repo.checkout / "release.txt").read_text(encoding="utf-8") == "base\n"
    assert marker_path.is_file()

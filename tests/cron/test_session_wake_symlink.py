"""Pending wake paths retain their exact binding through profile directory aliases."""
from tests.cron.test_session_wake_owner import wake_owner as wake_owner, queue  # noqa: PLC0414
from cron import bot_chat_legacy as pending


def test_profile_cron_directory_symlink_retains_exact_wake(wake_owner):
    cron = wake_owner.home / 'cron'
    target = wake_owner.home / 'cron-storage'
    if cron.exists():
        cron.rename(target)
    else:
        target.mkdir()
    cron.symlink_to(target, target_is_directory=True)
    path, _ = queue(wake_owner)
    wake_owner.ready = True
    saved = pending._drain_one(path)
    assert saved['status'] == 'transferred', saved
    assert wake_owner.methods.count('prompt.submit') == 1

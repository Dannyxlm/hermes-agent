"""Per-send shape audit uses a private file, never ordinary application logs."""
import json
import os
import stat

import pytest

from tests.tui_gateway.test_mobile_push import delivery as delivery  # noqa: PLC0414
from tui_gateway.mobile_push_payloads import AlertPreview


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("existing", [False, True])
def test_shape_audit_is_owner_only_across_reopen_and_rotation(delivery, tmp_path, existing):
    import hermes_logging

    service, _sender, now, ids, scope = delivery
    audit = service.store.path.parent / "push-shape-audit.jsonl"
    if existing:
        audit.touch(mode=0o644)
        audit.chmod(0o644)
    old_umask = os.umask(0o022)
    try:
        logs = hermes_logging.setup_logging(hermes_home=tmp_path / "log-home", mode="gui", force=True)
        service.register("p", scope, **ids, token="aa" * 32, environment="production", preview_enabled=True)
        for attempt in range(3):
            run = service.start_run(scope)
            service.record(scope, run["run_id"], "PRIVATE_EVENT", "complete",
                           preview=AlertPreview(title="PRIVATE_TITLE", reply="PRIVATE_BODY"))
            service.drain_once()
            hermes_logging.drain_log_queue()
            hermes_logging.flush_log_queue()
            assert audit.is_file()
            assert stat.S_IMODE(audit.stat().st_mode) == 0o600
            records = [json.loads(line) for line in audit.read_text().splitlines()]
            assert len(records) == (2 if attempt == 1 else 1)
            assert all(record.get("kind") == "alert" and record.get("outcome") == "accepted" for record in records)
            all_text = "\n".join(p.read_text() for p in logs.glob("*.log"))
            assert "mobile push shape" not in all_text
            assert not any(record["event_id"] in all_text for record in records)
            assert all(value not in audit.read_text() + all_text for value in
                       ("PRIVATE_TITLE", "PRIVATE_BODY", "PRIVATE_EVENT", "aa" * 32))
            if attempt == 0:
                audit.chmod(0o644)  # an existing restored file must be tightened on reopen
            elif attempt == 1:
                rotated = audit.with_suffix(".jsonl.1")
                audit.rename(rotated)
                assert stat.S_IMODE(rotated.stat().st_mode) == 0o600
            now[0] += 1
    finally:
        os.umask(old_umask)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("target_kind", ["symlink", "fifo"])
def test_shape_audit_rejects_non_regular_targets_without_general_log_fallback(delivery, tmp_path, target_kind, caplog):
    service, _sender, _, ids, scope = delivery
    audit = service.store.path.parent / "push-shape-audit.jsonl"
    target = tmp_path / "untouched"
    target.write_text("unchanged")
    if target_kind == "symlink":
        audit.symlink_to(target)
    else:
        os.mkfifo(audit)
    service.register("p", scope, **ids, token="aa" * 32, environment="production")
    run = service.start_run(scope)
    service.record(scope, run["run_id"], "done", "complete")
    # Audit failures must not undo accepted sends or leak the record to a logger.
    with caplog.at_level("INFO", logger="tui_gateway.mobile_push"):
        assert service.drain_once() == 1
    assert "mobile push shape" not in caplog.text
    assert target.read_text() == "unchanged"
    assert stat.S_ISFIFO(audit.stat().st_mode) if target_kind == "fifo" else audit.is_symlink()
    assert service.drain_once() == 0

"""A refused checkpoint diff is an RPC error, never a successful empty comparison."""
from types import SimpleNamespace

from agent.checkpoints import manager as cm
from tui_gateway import server


def test_rollback_diff_reports_staging_failure(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("original\n", encoding="utf-8")
    manager = cm.CheckpointManager(enabled=True)
    assert manager.ensure_checkpoint(str(work), "before edit")
    target = manager.list_checkpoints(str(work))[0]["hash"]
    (work / "a.txt").write_text("edited\n", encoding="utf-8")
    # Use a real Git index failure: unborn nested repositories are supported.
    cm._index_path(cm._store_path(), cm._project_hash(str(work))).write_bytes(b"invalid index\n")
    monkeypatch.setitem(server._sessions, "diff-failure", {
        "agent": SimpleNamespace(_checkpoint_mgr=manager),
        "session_key": "diff-failure", "cwd": str(work), "cols": 80,
    })

    response = server.handle_request({
        "id": "diff", "method": "rollback.diff",
        "params": {"session_id": "diff-failure", "hash": target},
    })

    assert "result" not in response, response
    assert response["error"]["code"] == 5022
    assert "Could not inspect untracked paths" in response["error"]["message"]
    assert (work / "a.txt").read_text(encoding="utf-8") == "edited\n"

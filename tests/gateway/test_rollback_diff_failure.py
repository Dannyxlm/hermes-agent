"""The shared owner's rollback.diff preserves checkpoint staging refusals."""
import asyncio
import subprocess

import pytest

from tests.gateway.test_session_busy_controls import owner
from tests.gateway.test_session_tui_ops import _checkpoint, _git_rpc
from tests.gateway.fixtures.local_recovery_probe import rpc, websocket


@pytest.mark.platforms("linux")
def test_rollback_diff_reports_staging_failure(tmp_path):
    with owner(tmp_path) as (home, _peer, desc):
        work = tmp_path / "work"
        work.mkdir()
        (work / "a.txt").write_text("original\n", encoding="utf-8")

        async def probe():
            async with websocket(home, desc) as ws:
                created = await rpc(ws, "session.create", request_id="diff-failure", source="tui",
                                    toolsets=[], cwd=str(work), checkpoints=True)
                sid = created["result"]["session_id"]
                await asyncio.to_thread(_checkpoint, home, work, "before edit")
                (work / "a.txt").write_text("edited\n", encoding="utf-8")
                nested = work / "nested"
                nested.mkdir()
                await asyncio.to_thread(subprocess.run, ["git", "init", str(nested)],
                                        check=True, capture_output=True)

                response = await _git_rpc(ws, "rollback.diff", session_id=sid, hash="1")

                assert "result" not in response, response
                assert response["error"]["message"] == "checkpoint_diff_failed"
                assert "Could not stage current files" in response["error"]["data"]["detail"]
                assert (work / "a.txt").read_text(encoding="utf-8") == "edited\n"

        asyncio.run(probe())

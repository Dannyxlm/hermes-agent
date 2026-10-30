"""Unfinished system units add read-only live signals on both Family transports."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from gateway.session_authority import SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch
from tests.waiting import async_wait_until

pytestmark = pytest.mark.platforms("linux")


@pytest.fixture
def fake_systemctl(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    calls = tmp_path / "calls"
    started = tmp_path / "started"
    executable = fake / "systemctl"
    executable.write_text(f"#!{sys.executable}\n" +
        "import json,os,sys,threading\nfrom pathlib import Path\n" +
        f"with Path({str(calls)!r}).open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n" +
        f"Path({str(started)!r}).touch()\n" +
        "mode=os.environ['UNIT_TEST_STATE']\n" +
        "if mode=='slow': threading.Event().wait(10)\n" +
        "for unit in sys.argv[sys.argv.index('--')+1:]:\n" +
        " print('Id='+unit.removesuffix('.service')+'.service')\n" +
        " print('LoadState='+('not-found' if mode=='not-found' else 'loaded'))\n" +
        " print('ActiveState='+mode)\n print('SubState=running')\n print('Result=exit-code')\n" +
        " print('ExecMainStatus=23')\n print('Description=Unit description')\n print()\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake))
    return calls, started


@pytest.mark.asyncio
@pytest.mark.parametrize("state,status", [("active", "working"), ("activating", "working"),
    ("deactivating", "working"), ("failed", "failed"), ("not-found", "unknown"), ("slow", "unknown"), ("missing", "unknown")])
async def test_native_systemd_family_signals(tmp_path, monkeypatch, fake_systemctl, state, status):
    import json
    calls, started = fake_systemctl
    monkeypatch.setenv("UNIT_TEST_STATE", state)
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    if state == "missing":
        (tmp_path / "bin/systemctl").unlink()
    with SessionDB(home / "state.db") as db:
        db.create_session("root", "cli")
        db.record_launch("unit", kind="process", launcher_profile="default", launcher_session_id="root",
                         child_profile="default", backend_ref="systemd:example")
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(home), instance_id="test",
                                     db=db, epoch=begin_runtime_epoch(db, instance_id="test"))
        conn = AuthorityConnection(authority, object(), {"user_id": "owner"}, operator=True)
        async def read():
            return await conn.dispatch({"id": 1, "method": "session.family", "params": {
                "session_id": "root", "profile": "default"}})
        try:
            request = asyncio.create_task(read())
            if state == "slow":
                # The event loop can observe the launched subprocess while the RPC is in flight.
                await async_wait_until(started.exists, timeout=4, what="systemctl started off-loop")
                assert not request.done()
            reply = await asyncio.wait_for(request, 5)
            assert "result" in reply, reply
            node, = reply["result"]["nodes"]
            assert node["status"] == status
            assert "stop" not in node["actions"]
            if state in {"active", "activating", "deactivating"}:
                assert node["command"] == "Unit description"
            elif state == "failed":
                assert node["exit_code"] == 23 and node["terminal_cause"] == "error"
            if state != "missing":
                await read()
                captured = [json.loads(line) for line in calls.read_text().splitlines()]
                assert len(captured) == 1, captured
                assert captured[0] == ["show", "--property=Id,LoadState,ActiveState,SubState,Result,ExecMainStatus,Description",
                                       "--", "example"]
        finally:
            await conn.close()

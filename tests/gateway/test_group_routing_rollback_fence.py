"""v0.23 rollback fence: no new Group Chat settings event may carry 'auto' routing yet.

The release's rollback target plans only 'everyone' and 'lead' rooms; a stored 'auto' would stall the
room there. So this release reads 'auto' but refuses to admit it from a client (canonical socket or the
settings setter every handler uses), a replica page, a promotion or an import. Ordinary settings edits
keep working (state probe: outputs/releases/2026-11-08-hermes-v0.23/state).
"""
import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import gateway.hosted_room_replicas as replicas
from gateway import hosted_room_context as ctx
from gateway import hosted_rooms as rooms
from gateway.hosted_rooms_legacy_import import _copy_rows
from tests.gateway.fixtures.local_recovery_probe import child_env, daemon, rpc, websocket

MEMBERS = [{"member_id": "one", "profile": "one", "handle": "one"},
           {"member_id": "two", "profile": "two", "handle": "two"}]
REMOTE = "install:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
AUTO = {"purpose": "Ship", "notes": "", "routing": "auto", "lead_member_id": ""}
SYSTEM = {"kind": "system", "id": "room-control"}


def _room(db: Path, room_id: str = "room-fence", gateway_id: str | None = None) -> None:
    rooms.create_room(db, room_id=room_id, name="Fence", members=MEMBERS,
                      authority_gateway_id=gateway_id or rooms.local_authority_gateway_id())


def _stored_auto(db: Path, room_id: str = "room-fence", gateway_id: str | None = None) -> int:
    """A settings event an unfenced runtime wrote, appended below the setter."""
    return rooms.append_event(
        db, room_id=room_id, event_id="foreign-auto", kind=ctx.CONTEXT_EVENT_KIND, actor=SYSTEM, payload=AUTO,
        authority_gateway_id=gateway_id or rooms.local_authority_gateway_id(), authority_epoch=1)["seq"]


def _log(db: Path, room_id: str = "room-fence") -> list:
    return rooms.read_events(db, room_id=room_id, since_seq=0, limit=100)["events"]


def test_setter_refuses_new_auto_and_keeps_ordinary_edits(tmp_path):
    db = tmp_path / "shared-state.db"
    _room(db)
    before = _log(db)
    with pytest.raises(rooms.HostedRoomError, match="next Hermes update"):
        ctx.set_room_context(db, room_id="room-fence", event_id="auto", purpose="Ship", notes="", routing="auto")
    assert _log(db) == before
    assert ctx.room_context(db, room_id="room-fence")["routing"] == "everyone"

    lead = ctx.set_room_context(db, room_id="room-fence", event_id="lead", purpose="Ship", notes="",
                                routing="lead", lead_member_id="one")
    assert lead["context"]["routing"] == "lead"
    kept = ctx.set_room_context(db, room_id="room-fence", event_id="purpose", purpose="Ship it", notes="")
    assert kept["context"]["routing"] == "lead"


def test_inherited_auto_is_read_but_not_carried_into_a_new_event(tmp_path):
    db = tmp_path / "shared-state.db"
    _room(db)
    seq = _stored_auto(db)
    # Readable: the planner's validator and as-of reads still accept the stored value.
    assert ctx.room_context(db, room_id="room-fence")["routing"] == "auto"
    assert ctx.room_context(db, room_id="room-fence", before_seq=seq + 1)["routing"] == "auto"
    assert ctx.validate_context("", "", "auto")["routing"] == "auto"
    before = _log(db)
    with pytest.raises(rooms.HostedRoomError, match="next Hermes update"):
        ctx.set_room_context(db, room_id="room-fence", event_id="purpose-only", purpose="New", notes="")
    assert _log(db) == before
    moved = ctx.set_room_context(db, room_id="room-fence", event_id="everyone", purpose="New", notes="",
                                 routing="everyone")
    assert moved["context"]["routing"] == "everyone"


def _remote_page(tmp_path: Path) -> dict:
    remote = tmp_path / "remote.db"
    _room(remote, gateway_id=REMOTE)
    rooms.append_event(remote, room_id="room-fence", event_id="hello", kind="message.user",
                       actor={"kind": "user", "id": "desktop"}, payload={"text": "hi", "thread_id": "main"},
                       authority_gateway_id=REMOTE, authority_epoch=1)
    _stored_auto(remote, gateway_id=REMOTE)
    return rooms.read_events(remote, room_id="room-fence", since_seq=0, limit=100)


def test_replica_page_with_auto_is_refused_whole(tmp_path):
    page = _remote_page(tmp_path)
    replica = tmp_path / "replica.db"
    with pytest.raises(rooms.HostedRoomError, match="next Hermes update"):
        replicas.ingest_page(replica, room_id="room-fence", room_name="Fence", members=MEMBERS, page=page)
    with pytest.raises(replicas.ReplicaError):
        replicas.replica_state(replica, room_id="room-fence")


def test_promotion_refuses_a_replica_already_holding_auto(tmp_path, monkeypatch):
    page = _remote_page(tmp_path)
    replica = tmp_path / "replica.db"
    with monkeypatch.context() as unfenced:  # a replica stored before this release's fence
        unfenced.setattr(ctx, "WRITABLE_ROUTING_MODES", ctx.ROUTING_MODES)
        replicas.ingest_page(replica, room_id="room-fence", room_name="Fence", members=MEMBERS, page=page)
    with pytest.raises(rooms.HostedRoomError, match="next Hermes update"):
        replicas.promote_replica(replica, room_id="room-fence")
    assert replicas.replica_state(replica, room_id="room-fence")["last_seq"] == len(page["events"])
    with pytest.raises(rooms.HostedRoomError):
        rooms.room_state(replica, room_id="room-fence")


def test_import_refuses_a_source_room_holding_auto(tmp_path):
    source = tmp_path / "standalone.db"
    _room(source)
    _stored_auto(source)
    target = tmp_path / "shared-state.db"
    _room(target, room_id="room-already-here")
    with closing(sqlite3.connect(target)) as conn:
        with pytest.raises(rooms.HostedRoomError, match="next Hermes update"):
            _copy_rows(conn, source)
        conn.rollback()
    assert [r["room_id"] for r in rooms.list_rooms(target)] == ["room-already-here"]


def test_canonical_socket_refuses_auto_and_offers_only_writable_modes(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    (home / 'profiles' / 'helper').mkdir(parents=True)
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'hosted_rooms': {'profiles': {'helper': str(home / 'profiles' / 'helper')}},
        'model': {'provider': 'custom', 'default': 'no-inference', 'base_url': 'http://127.0.0.1:1/v1'},
        'auxiliary': {'title_generation': {'enabled': False}},
    }))
    env = child_env() | dict(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
                             PYTHONPATH=str(root), PYTHONUNBUFFERED='1')

    async def probe(desc):
        async with websocket(home, desc) as ws:
            members = [{'member_id': 'one', 'profile': 'default', 'handle': 'one'},
                       {'member_id': 'two', 'profile': 'helper', 'handle': 'two'}]
            created = await rpc(ws, 'groups.create', room_id='room-fence', name='Fence', members=members)
            assert 'result' in created, created
            log_before = await rpc(ws, 'groups.log', room_id='room-fence')

            refused = await rpc(ws, 'groups.context', room_id='room-fence', event_id='auto',
                                purpose='Ship', notes='', routing='auto')
            assert refused['error']['code'] == 4001, refused
            assert await rpc(ws, 'groups.log', room_id='room-fence') == {**log_before, 'id': 'groups.log'}
            state = (await rpc(ws, 'groups.state', room_id='room-fence'))['result']
            assert state['context']['routing'] == 'everyone'
            assert state['driver_status']['routing_modes'] == ['everyone', 'lead']

            lead = await rpc(ws, 'groups.context', room_id='room-fence', event_id='lead',
                             purpose='Ship', notes='', routing='lead', lead_member_id='one')
            assert lead['result']['context']['routing'] == 'lead', lead
            kept = await rpc(ws, 'groups.context', room_id='room-fence', event_id='purpose',
                             purpose='Ship it', notes='')
            assert kept['result']['context']['routing'] == 'lead', kept

    with daemon(root, home, env, barrier=False) as (_, desc):
        asyncio.run(probe(desc))

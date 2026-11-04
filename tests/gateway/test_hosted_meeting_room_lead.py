"""Meeting lead routing keeps shared-thread owner posts separate from automation."""

import json
from pathlib import Path

import pytest

from gateway import hosted_rooms
from tests.gateway.test_hosted_room_lead import MEMBERS, PROFILES, ROOM_ID, _events, _finish, _lead_room, _plan

MEETING = {"kind": "standup", "schedule_local": "08:15", "timezone": "America/Edmonton"}


def _create(db: Path, *, meeting: bool, routing: str = "lead") -> None:
    hosted_rooms.create_room(
        db, room_id=ROOM_ID, name="Meeting", members=MEMBERS,
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(), meeting=MEETING if meeting else None)
    if routing == "lead":
        _lead_room(db)


def _post(db: Path, *, actor: str, text: str, event_id: str) -> int:
    return hosted_rooms.append_event(
        db, room_id=ROOM_ID, event_id=event_id, kind="message.user", actor={"kind": "user", "id": actor},
        payload={"text": text, "thread_id": "main"},
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(), authority_epoch=1)["seq"]


def _round(db: Path, seq: int) -> list[str]:
    members = []
    for _ in PROFILES:
        decision = _plan(db, seq)
        if decision.status != "task":
            break
        assert decision.reason == "member_turn"
        members.append(decision.task.member.handle)
        _finish(db, decision, "(pass)")
    assert _plan(db, seq).status == "settled"
    return members


def test_owner_in_existing_meeting_main_routes_once_then_only_the_assignee_answers(tmp_path: Path):
    db = tmp_path / "state.db"
    _create(db, meeting=True)
    previous = _post(db, actor="team-room", text="@everyone prior batch", event_id="prior")
    assert _round(db, previous) == list(PROFILES)
    seq = _post(db, actor="desktop", text="Can someone inspect this?", event_id="owner")

    routing = _plan(db, seq)
    assert (routing.reason, routing.task.member.handle, routing.thread_id) == ("lead_routing", "ada", "main")
    assert _plan(db, seq).task.identity == routing.task.identity
    publication = _finish(db, routing, '{"assign": ["linus"]}')
    retry = _finish(db, routing, '{"assign": ["linus"]}')
    assert [e["kind"] for e in publication] == ["room.assignment", "turn.settled"]
    assert publication[0]["payload"]["thread_id"] == "main"
    assert publication[0]["payload"]["member_ids"] == ["member-linus"]
    assert [e["event_id"] for e in retry] == [e["event_id"] for e in publication]
    assert all(e["idempotent"] for e in retry)
    assert _round(db, seq) == ["linus"]
    assert len([e for e in _events(db) if e["kind"] == "room.assignment"]) == 1


@pytest.mark.parametrize(("meeting", "routing", "actor", "prior", "text", "expected"), [
    (True, "lead", "desktop", False, "hello", "lead_routing"),
    (True, "lead", "team-room", False, "hello", "everyone"),
    (True, "lead", "team-room", True, "hello", "everyone"),
    (True, "lead", "team-room", False, "@grace hello", "grace"),
    (True, "lead", "other-user", False, "hello", "everyone"),
    (True, "lead", "desktop", True, "@grace hello", "grace"),
    (True, "lead", "desktop", True, "@all hello", "everyone"),
    (True, "lead", "desktop", True, "@everyone hello", "everyone"),
    (True, "lead", "desktop", True, "@unknown hello", "lead_routing"),
    (True, "lead", "cron:job1:run1", False, "hello", "silent"),
    (True, "lead", "cron:job1:run1", True, "hello", "silent"),
    (True, "everyone", "desktop", True, "hello", "everyone"),
    (False, "lead", "desktop", False, "hello", "lead_routing"),
    (False, "lead", "desktop", True, "hello", "everyone"),
    (False, "lead", "team-room", False, "hello", "lead_routing"),
    (False, "lead", "team-room", True, "hello", "everyone"),
])
def test_routing_boundaries(tmp_path: Path, meeting: bool, routing: str, actor: str,
                            prior: bool, text: str, expected: str):
    db = tmp_path / "state.db"
    _create(db, meeting=meeting, routing=routing)
    if prior:
        seq = _post(db, actor="desktop", text="@everyone previous", event_id="prior")
        assert _round(db, seq) == list(PROFILES)
    seq = _post(db, actor=actor, text=text, event_id="next")
    decision = _plan(db, seq)
    if expected == "lead_routing":
        assert decision.reason == "lead_routing"
        assert decision.task.member.handle == "ada"
    else:
        members = _round(db, seq)
        assert members == (list(PROFILES) if expected == "everyone" else [] if expected == "silent" else [expected])


def test_canonical_meeting_send_normalizes_main_and_runs_the_leads_assignment(tmp_path: Path, monkeypatch):
    from gateway import session_finite
    from tests.gateway.test_hosted_group_members import _desktop, _gateway
    from tests.waiting import wait_until

    async def execute(authority, ref, row):
        if "You are the lead of this room." in json.dumps(row["payload"]):
            return '{"assign": ["linus"]}'
        return "Fixture member answer."

    monkeypatch.setattr(session_finite, "execute_finite_admission", execute)
    members = [{**m, "profile": "default" if m["profile"] == "ada" else m["profile"]} for m in MEMBERS]
    with _gateway(tmp_path, monkeypatch, served=("linus", "grace")) as (runner, _homes, call):
        desktop = _desktop(runner, call)
        created = desktop("default", "groups.create", room_id=ROOM_ID, name="Meeting", members=members, meeting=MEETING)
        assert "result" in created, created
        for authority in runner.session_authorities:
            service = authority.hosted_room_service
            service.runtime.poll_interval_seconds = 0.01
            service.runtime.active_poll_interval_seconds = 0.01
        settings = desktop("default", "groups.context", room_id=ROOM_ID, event_id="lead",
                           purpose="", notes="", routing="lead", lead_member_id="member-ada")
        assert settings["result"]["context"]["routing"] == "lead"
        db = runner.session_authority.db.db_path

        def settled(event_id):
            return any(e["kind"] == "room.activity" and e["payload"].get("discussion_event_id") == event_id
                       for e in _events(db))

        first = desktop("default", "groups.send", room_id=ROOM_ID, event_id="previous",
                        payload={"thread_id": "phone-old", "text": "@grace prior question"})["result"]["event"]
        wait_until(lambda: settled(first["event_id"]), what="prior meeting post settles")
        sent = desktop("default", "groups.send", room_id=ROOM_ID, event_id="owner",
                       payload={"thread_id": "phone-new", "text": "Can someone inspect this?"})["result"]["event"]
        assert sent["payload"]["thread_id"] == "main"
        assert sent["actor"] == {"kind": "user", "id": "desktop"}
        wait_until(lambda: settled(sent["event_id"]), what="lead assignment and assignee answer settle")
        effects = [e for e in _events(db) if e["payload"].get("discussion_event_id") == sent["event_id"]]
        assert [e["payload"]["member_ids"] for e in effects if e["kind"] == "room.assignment"] == [["member-linus"]]
        assert [e["payload"]["member_id"] for e in effects if e["kind"] == "message.member"] == ["member-linus"]

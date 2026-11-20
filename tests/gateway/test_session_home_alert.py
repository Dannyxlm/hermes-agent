"""A producer turn's Bot Chat reply alerts the phone only when it opens with [NEEDS YOU].

Real ``SessionDB`` and a real ``PushService`` outbox; only Apple (the sender) is a stand-in.
"""
import asyncio
import uuid
from types import SimpleNamespace

import pytest

from gateway import session_home_alert
from gateway.session_local import LocalSessionAdapter
from hermes_state import SessionDB
from tui_gateway.mobile_push import PushService, Scope
from tui_gateway.mobile_push_provider import DeliveryResult


class Sender:
    def __init__(self):
        self.jobs = []

    def send(self, job):
        self.jobs.append(job)
        return DeliveryResult("accepted")

    def close(self):
        pass


@pytest.fixture()
def home(tmp_path, monkeypatch):
    sender = Sender()
    service = PushService(tmp_path / "outbox.sqlite3", sender)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("bot-root", source="tui")
    db.set_session_title("bot-root", "Bot Chat")
    db.create_session("other-chat", source="tui")
    profile = SimpleNamespace(name="default", path=tmp_path, bot_title="", display_name="")
    monkeypatch.setattr(session_home_alert, "push_service", lambda: service)
    monkeypatch.setattr(session_home_alert, "_profile", lambda _home: profile)
    scope = Scope("native", "default", "bot-root")
    ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
    service.register("principal", scope, **ids, token="aa" * 32, environment="production",
                     preview_enabled=True)
    yield SimpleNamespace(db=db, service=service, sender=sender, scope=scope,
                          adapter=LocalSessionAdapter(SimpleNamespace(db=db, runner=None)))
    service.close()
    db.close()


def _deliver(home, chat_id, text):
    asyncio.run(home.adapter.send(chat_id, text))
    home.service.drain_once()
    return home.sender.jobs


def test_a_needs_you_reply_in_the_bot_chat_alerts_its_device_once(home):
    jobs = _deliver(home, "bot-root", "[NEEDS YOU] The lease renewal needs your signature by Friday.")
    assert len(jobs) == 1
    payload = jobs[0]["payload"]
    assert payload["hermex.status"] == "complete"
    assert payload["hermex.destination"]["surface"] == "native"
    assert payload["hermex.destination"]["canonical_root_id"] == "bot-root"
    # The marker is the producer's signal, never the alert's words; the agent titles it, not "default".
    assert payload["aps"]["alert"]["body"] == "The lease renewal needs your signature by Friday."
    assert payload["aps"]["alert"]["title"] == "Ava"


@pytest.mark.parametrize("text", [
    "Box doctor's report checks out. Nothing here needs you tonight.",
    "Routine summary.\n[NEEDS YOU] quoted later is prose, not the marker.",
    "",
])
def test_routine_replies_only_stay_unread(home, text):
    assert _deliver(home, "bot-root", text) == []


def test_a_needs_you_reply_in_any_other_chat_sends_nothing(home):
    assert _deliver(home, "other-chat", "[NEEDS YOU] not the Bot Chat") == []


def test_a_broken_outbox_never_fails_the_reply(home, monkeypatch):
    def broken():
        raise OSError("disk gone")
    monkeypatch.setattr(session_home_alert, "push_service", broken)
    result = asyncio.run(home.adapter.send("bot-root", "[NEEDS YOU] still delivered"))
    assert result.success


def test_marker_is_read_case_insensitively_after_leading_whitespace():
    assert session_home_alert.needs_you_body("  [needs you]  Pick a date.") == "Pick a date."
    assert session_home_alert.needs_you_body("[NEEDS YOU]") == ""
    assert session_home_alert.needs_you_body("[NEEDS] YOU") is None
    assert session_home_alert.needs_you_body(None) is None

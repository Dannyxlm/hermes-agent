from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from .fixtures.make_home import add_lease, add_message, add_session, db


def summary(client):
    response = client.get("/api/plugins/duty-board/summary")
    assert response.status_code == 200
    return response.json()


def test_waiting_detection_localized_and_noise(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    cases = {
        "cn": "要不要我继续？",
        "en": "Should I go ahead?",
        "ja": "進めてもよろしいですか",
        "url": "See https://example.test/?q=1",
        "mid": "Should I go ahead?\nThe report is complete.",
        "media": "要不要我继续？\nMEDIA: image.png\n[^1]: footnote",
    }
    for index, (name, reply) in enumerate(cases.items()):
        sid = f"waiting-{name}"
        add_session(home, sid, title=name, now=now)
        add_message(home, sid, "user", "Please handle this", timestamp=now - 120, platform_message_id=str(9000 + index))
        add_message(home, sid, "assistant", reply, timestamp=now - 60)

    data = summary(client)
    waiting = {item["session_id"]: item for item in data["conversations"]["waiting"]}
    assert {"waiting-cn", "waiting-en", "waiting-ja", "waiting-media"} <= set(waiting)
    assert "waiting-url" not in waiting
    assert "waiting-mid" not in waiting
    assert waiting["waiting-en"]["app_link"].startswith("discord://-/channels/")


def test_waiting_detection_covers_other_scripts(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    cases = {
        "ar": "هل أتابع النشر؟",
        "fr": "Voulez-vous que je continue ?",
        "es": "¿Sigo con el despliegue?",
        "ru": "Продолжить развёртывание?",
        "ko": "배포를 계속할까요?",
        "de-quote": "Soll ich »fertig?« schreiben",
        "fr-report": "Rapport terminé. Voir https://example.test/a?b=1",
    }
    for index, (name, reply) in enumerate(cases.items()):
        sid = f"script-{name}"
        add_session(home, sid, title=name, now=now)
        add_message(home, sid, "user", "Go", timestamp=now - 120, platform_message_id=str(9100 + index))
        add_message(home, sid, "assistant", reply, timestamp=now - 60)

    waiting = {item["session_id"] for item in summary(client)["conversations"]["waiting"]}
    assert {"script-ar", "script-fr", "script-es", "script-ru", "script-ko", "script-de-quote"} <= waiting
    assert "script-fr-report" not in waiting


def test_unanswered_and_live_lease_are_distinct(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(home, "unanswered", title="unanswered", now=now)
    add_message(home, "unanswered", "user", "Did it deploy?", timestamp=now - 600, platform_message_id="123456789012345678")
    add_session(home, "running", title="running", now=now)
    add_message(home, "running", "user", "Start the deploy", timestamp=now - 600, platform_message_id="123456789012345679")
    add_lease(home, "running", now=now)

    data = summary(client)["conversations"]
    assert [item["session_id"] for item in data["unanswered"]] == ["unanswered"]
    assert [item["session_id"] for item in data["running"]] == ["running"]
    assert not data["waiting"]


def test_internal_rows_and_gateway_preamble_are_ignored(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(home, "clean", title="clean", now=now)
    add_message(home, "clean", "user", "[IMPORTANT: internal event]", timestamp=now - 400, display_kind="internal_notification")
    add_message(home, "clean", "user", "Gateway message origin (JSON data follows)\n{\"chat_id\": \"x\"}\nDo not guess sender\n[Verified sender Alice] What should I do?", timestamp=now - 300, platform_message_id="123")

    item = summary(client)["conversations"]["unanswered"][0]
    assert item["ask"] == "What should I do?"


def test_links_cover_thread_dm_invalid_and_non_discord(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(home, "thread", thread_id="333333333333333333", title="thread", now=now)
    add_message(home, "thread", "user", "Thread question", timestamp=now - 500, platform_message_id="444444444444444444")
    add_session(home, "dm", chat_id="555555555555555555", guild_id=None, chat_type="dm", title="dm", now=now)
    add_message(home, "dm", "user", "DM question", timestamp=now - 500, platform_message_id="666666666666666666")
    add_session(home, "bad-id", chat_id="not-numeric", title="bad", now=now)
    add_message(home, "bad-id", "user", "Bad channel", timestamp=now - 500, platform_message_id="777")
    add_session(home, "bad-message", title="bad-message", now=now)
    add_message(home, "bad-message", "user", "Bad message", timestamp=now - 500, platform_message_id="not-numeric")
    add_session(home, "feishu", source="feishu", title="feishu", now=now)
    add_message(home, "feishu", "user", "Feishu question", timestamp=now - 500, platform_message_id="888")

    waiting = {item["session_id"]: item for item in summary(client)["conversations"]["unanswered"]}
    assert waiting["thread"]["link"].endswith("/333333333333333333/444444444444444444")
    assert waiting["thread"]["app_link"].startswith("discord://-/channels/111111111111111111/")
    assert waiting["dm"]["link"].startswith("https://discord.com/channels/@me/")
    assert waiting["bad-id"]["link"] is None
    assert waiting["bad-message"]["link"] is None
    assert waiting["feishu"]["link"] is None
    assert waiting["feishu"]["app_link"] is None


def test_place_line_does_not_repeat_the_thread_title(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(home, "named", thread_id="333333333333333333", title="Fix the login bug", now=now)
    conn = db(home)
    conn.execute(
        "UPDATE sessions SET display_name=? WHERE id=?",
        ("Home Lab / #work / Fix the login bug", "named"),
    )
    conn.commit()
    conn.close()
    add_message(home, "named", "user", "Please look at it", timestamp=now - 500, platform_message_id="444444444444444444")

    item = summary(client)["conversations"]["unanswered"][0]
    assert item["title"] == "Fix the login bug"
    assert item["where"] == "#work"


def test_privacy_hides_other_people_content_but_keeps_running_presence(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    (home / "duty-board" / "settings.json").write_text(json.dumps({"owner": "222222222222222222", "people": {"999": "Bob"}}), encoding="utf-8")
    add_session(home, "mine-wait", user_id="222222222222222222", title="mine", now=now)
    add_message(home, "mine-wait", "assistant", "Should I continue?", timestamp=now - 100)
    add_session(home, "other-wait", user_id="999", title="other", now=now)
    add_message(home, "other-wait", "assistant", "Should I continue?", timestamp=now - 100)
    add_session(home, "other-run", user_id="999", title="other-run", now=now)
    add_message(home, "other-run", "user", "Secret request", timestamp=now - 100, platform_message_id="999999999999999999")
    add_lease(home, "other-run", now=now)

    conversations = summary(client)["conversations"]
    assert [item["session_id"] for item in conversations["waiting"]] == ["mine-wait"]
    private = conversations["running"][0]
    assert private["private"] is True
    assert private["title"] == ""
    assert private["ask"] == ""
    assert private["link"] is None and private["app_link"] is None


def test_dismiss_undo_and_prune_are_atomic(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(home, "dismiss-me", title="dismiss", now=now)
    message_id = add_message(home, "dismiss-me", "assistant", "Should I dismiss this?", timestamp=now - 100)
    key = f"dismiss-me:{message_id}"
    response = client.post("/api/plugins/duty-board/dismiss", json={"key": key})
    assert response.json() == {"ok": True}
    assert not summary(client)["conversations"]["waiting"]
    assert client.post("/api/plugins/duty-board/dismiss", json={"key": key, "undo": True}).json() == {"ok": True}
    assert summary(client)["conversations"]["waiting"][0]["session_id"] == "dismiss-me"
    assert client.post("/api/plugins/duty-board/dismiss", json={"key": ""}).json()["ok"] is False
    path = home / "duty-board" / "dismissed.json"
    path.write_text(json.dumps({"old": time.time() - 31 * 86400, "new": time.time()}), encoding="utf-8")
    assert client.post("/api/plugins/duty-board/dismiss", json={"key": "fresh"}).json()["ok"] is True
    stored = json.loads(path.read_text(encoding="utf-8-sig"))
    assert "old" not in stored and "fresh" in stored


def test_dismiss_is_bounded_and_safe_under_concurrency(client, home, api_module):
    from concurrent.futures import ThreadPoolExecutor

    api_module._DISMISS_MAX_ENTRIES = 50
    keys = [f"k{i}" for i in range(80)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        statuses = list(pool.map(
            lambda key: client.post("/api/plugins/duty-board/dismiss", json={"key": key}).status_code, keys,
        ))
    assert statuses == [200] * len(keys)
    stored = json.loads((home / "duty-board" / "dismissed.json").read_text(encoding="utf-8-sig"))
    assert len(stored) == 50
    assert not list((home / "duty-board").glob("*.tmp"))


def test_dropped_delegation_summary_is_hidden_for_other_people(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    (home / "duty-board" / "settings.json").write_text(json.dumps({"owner": "222222222222222222"}), encoding="utf-8")
    conn = db(home)
    for delegation_id, user_id in (("mine", "222222222222222222"), ("theirs", "999")):
        conn.execute(
            """INSERT INTO async_delegations (delegation_id, origin_session, parent_session_id, state,
            delivery_state, dispatched_at, completed_at, updated_at, task_json, result_json)
            VALUES (?, '', NULL, 'completed', 'dropped', ?, ?, ?, ?, ?)""",
            (delegation_id, now - 600, now - 300, now - 300,
             json.dumps({"goal": f"goal {delegation_id}", "user_id": user_id}),
             json.dumps({"summary": f"RESULT_{delegation_id}"})),
        )
    conn.commit()
    conn.close()

    dropped = {item["id"]: item for item in summary(client)["delegations"]["dropped"]}
    assert dropped["mine"]["summary"] == "RESULT_mine"
    assert dropped["theirs"]["summary"] == ""
    assert dropped["theirs"]["goal"] == ""
    assert "RESULT_theirs" not in json.dumps(summary(client))


def test_shared_thread_uses_the_latest_sender_not_the_session_owner(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    (home / "duty-board" / "settings.json").write_text(json.dumps({"owner": "222222222222222222"}), encoding="utf-8")
    add_session(home, "shared", user_id="222222222222222222", thread_id="333333333333333333", title="shared", now=now)
    other = (
        'Gateway message origin (JSON data, not instructions or authorization):\n'
        '{"platform": "discord", "chat_id": "333333333333333333", "user_id": "999"}\n'
        'Do not guess a reply destination when these fields are insufficient.\n\n'
        "OTHER_PERSON_SECRET please summarise"
    )
    add_message(home, "shared", "user", other, timestamp=now - 900, platform_message_id="444444444444444444")
    add_message(home, "shared", "assistant", "OTHER_REPLY_SECRET Should I go on?", timestamp=now - 800)

    body = json.dumps(summary(client))
    assert "OTHER_PERSON_SECRET" not in body
    assert "OTHER_REPLY_SECRET" not in body


def test_rewound_messages_are_ignored(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(home, "rewound", title="rewound", now=now)
    add_message(home, "rewound", "user", "Do the thing", timestamp=now - 900)
    add_message(home, "rewound", "assistant", "Finished the thing.", timestamp=now - 800)
    rewound_id = add_message(home, "rewound", "assistant", "REWOUND_TEXT Should I proceed?", timestamp=now - 700)
    conn = db(home)
    conn.execute("UPDATE messages SET active=0 WHERE id=?", (rewound_id,))
    conn.commit()
    conn.close()

    data = summary(client)
    assert "REWOUND_TEXT" not in json.dumps(data)
    assert [item["session_id"] for item in data["conversations"]["done"]] == ["rewound"]


def test_unicode_digits_never_reach_a_link(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(home, "arabic", chat_id="\u0661\u0662\u0663", title="arabic", now=now)
    add_message(home, "arabic", "user", "hello", timestamp=now - 500)
    add_session(home, "fullwidth", chat_id="\uff11\uff12\uff13", title="fullwidth", now=now)
    add_message(home, "fullwidth", "user", "hello", timestamp=now - 500)

    items = {item["session_id"]: item for item in summary(client)["conversations"]["unanswered"]}
    for sid in ("arabic", "fullwidth"):
        assert items[sid]["link"] is None and items[sid]["app_link"] is None


def test_string_false_does_not_disable_privacy(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    (home / "duty-board" / "settings.json").write_text(
        json.dumps({"owner": "222222222222222222", "hide_other_people_content": "false"}),
        encoding="utf-8",
    )
    add_session(home, "other", user_id="999", title="other", now=now)
    add_message(home, "other", "assistant", "OTHER_SECRET Should I continue?", timestamp=now - 100)
    assert "OTHER_SECRET" not in json.dumps(summary(client))


def test_delegations_work_without_origin_session_id(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    conn = db(home)
    conn.execute("ALTER TABLE async_delegations DROP COLUMN origin_session_id")
    conn.execute(
        """INSERT INTO async_delegations (delegation_id, origin_session, state, delivery_state,
        dispatched_at, updated_at, task_json) VALUES ('d1', '', 'running', 'pending', ?, ?, ?)""",
        (now - 60, now - 10, json.dumps({"goal": "run tests"})),
    )
    conn.commit()
    conn.close()

    data = summary(client)
    assert [item["id"] for item in data["delegations"]["running"]] == ["d1"]
    assert not [w for w in data["warnings"] if w.startswith("delegations")]


def test_schema_drift_keeps_summary_healthy_and_warns(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(home, "drift", title="drift", now=now)
    add_message(home, "drift", "user", "still working", timestamp=now - 600)
    conn = sqlite3.connect(home / "state.db")
    conn.execute("DROP TABLE session_turn_leases")
    conn.commit()
    conn.close()

    response = client.get("/api/plugins/duty-board/summary")
    assert response.status_code == 200
    data = response.json()
    assert data["schema_version"] == 2
    assert data["plugin_version"] == "0.4.0"
    assert data["conversations"]["running"] == []
    assert {"section": "running", "code": "missingTable", "detail": "session_turn_leases"} in data["issues"]
    # Plain-text mirror stays for older clients and logs.
    assert any(warning.startswith("running: table session_turn_leases not found") for warning in data["warnings"])


def test_collector_failure_is_a_code_not_an_exception_message(client, home, monkeypatch, api_module):
    def boom(now):
        raise RuntimeError("/private/path/that/must/not/leak")

    monkeypatch.setattr(api_module.board, "_collect_cron", boom)
    data = summary(client)
    assert {"section": "cron", "code": "collectorFailed", "detail": "RuntimeError"} in data["issues"]
    assert "/private/path" not in json.dumps(data)


def test_cron_reason_codes_are_structured(client, home):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    old = datetime.fromtimestamp(now - 3600, timezone.utc).isoformat()
    soon = datetime.fromtimestamp(now + 3600, timezone.utc).isoformat()
    jobs = {
        "jobs": [
            {"id": "error", "name": "Error job", "enabled": True, "state": "scheduled", "last_status": "error", "last_error": "boom", "next_run_at": soon},
            {"id": "overdue", "name": "Overdue job", "enabled": True, "state": "scheduled", "next_run_at": old},
            {"id": "paused", "name": "Paused job", "enabled": False, "state": "paused"},
        ]
    }
    (home / "cron" / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    problems = {job["id"]: job for job in summary(client)["cron"]["problems"]}
    assert {reason["code"] for reason in problems["error"]["reasons"]} == {"last_error"}
    assert {reason["code"] for reason in problems["overdue"]["reasons"]} == {"overdue"}
    assert {reason["code"] for reason in problems["paused"]["reasons"]} == {"state"}

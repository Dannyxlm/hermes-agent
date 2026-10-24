"""Build a realistic, fully fictional Hermes home for screenshots and demos.

Every id, name and message here is invented. Run:

    python dev/demo_home.py /tmp/duty-board-demo
    python dev/harness/serve.py --fixture /tmp/duty-board-demo
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures.make_home import add_lease, add_message, add_session, make_home  # noqa: E402

GUILD = "100000000000000001"
OWNER = "200000000000000001"


def _thread(n: int) -> str:
    return str(300000000000000000 + n)


def _msg(n: int) -> str:
    return str(400000000000000000 + n)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def build(path: Path) -> Path:
    now = time.time()
    home = make_home(path, now=now)
    (home / "duty-board" / "settings.json").write_text(
        json.dumps({"owner": OWNER, "people": {OWNER: "You"}}), encoding="utf-8"
    )

    def convo(i, title, turns, *, user=OWNER, name="Sam", lease=None, activity=None, channel="#projects"):
        sid = f"demo_{i:02d}"
        add_session(home, sid, user_id=user, user_name=name, chat_id=_thread(i), thread_id=_thread(i),
                    guild_id=GUILD, title=title, now=now)
        conn = sqlite3.connect(home / "state.db")
        conn.execute("UPDATE sessions SET display_name=? WHERE id=?", (f"Home Lab / {channel} / {title}", sid))
        conn.commit()
        conn.close()
        for k, (role, text, ago) in enumerate(turns):
            add_message(home, sid, role, text, timestamp=now - ago,
                        platform_message_id=_msg(i * 10 + k) if role == "user" else None)
        if lease is not None:
            add_lease(home, sid, now=now - lease, ttl=lease + 900)
        if activity:
            conn = sqlite3.connect(home / "state.db")
            conn.execute("UPDATE sessions SET last_activity_at=?, last_activity_description=? WHERE id=?",
                         (now - activity[1], activity[0], sid))
            conn.commit()
            conn.close()

    h = 3600
    convo(1, "Migrate the blog to Astro", [
        ("user", "Can you move the blog from Jekyll to Astro and keep the URLs?", 27 * h),
        ("assistant", "Done with the content migration; 142 posts build cleanly.\n\nTwo old URLs collide after slugging. "
                      "Should I add redirects for both, or rename the newer post?", 26 * h),
    ], channel="#website")
    convo(2, "Weekly grocery order", [
        ("user", "Build this week's grocery list from the meal plan.", 5 * h),
        ("assistant", "List is ready: 31 items, about $118.\n\nThe store is out of oat milk. Want me to swap in almond milk, "
                      "or skip it this week?", 4.6 * h),
    ], channel="#home")
    convo(3, "Flaky CI on the payments service", [
        ("user", "CI on payments fails about one run in five. Find out why.", 3 * h),
        ("assistant", "Found it: two tests share a temp port. I have a fix on a branch.\n\n"
                      "Your call: merge it now, or wait for Jo's review tomorrow?", 2.2 * h),
    ], channel="#work")
    convo(4, "Trip planning: Kyoto in November", [
        ("user", "Shortlist three ryokan near Arashiyama under $250 a night.", 50 * 60),
        ("assistant", "Shortlist is below. Two of them need a deposit by Friday.\n\nWhich one should I hold?", 40 * 60),
    ], channel="#travel")
    convo(5, "Invoice reminders", [
        ("user", "Remind the three overdue clients again, politely.", 25 * 60),
    ], channel="#admin")
    convo(6, "Refactor the auth middleware", [
        ("user", "Split the auth middleware into token and session parts, with tests.", 38 * 60),
    ], lease=38 * 60, activity=("tool running: terminal", 40), channel="#work")
    convo(7, "Summarise this week's support tickets", [
        ("user", "Summarise this week's support tickets by theme.", 12 * 60),
    ], lease=12 * 60, activity=("receiving stream response", 8), channel="#support")
    convo(8, "Back up the photo library", [
        ("user", "Back up the photo library to the NAS and verify checksums.", 21 * 60),
    ], lease=21 * 60, activity=("terminal command running (840s elapsed)", 14 * 60), channel="#home")
    convo(9, "Draft the release notes", [
        ("user", "Draft release notes for v2.4 from the merged PRs.", 6 * h),
        ("assistant", "Release notes are in docs/releases/2.4.md: 3 features, 11 fixes, 1 breaking change, "
                      "with links to each PR.", 5.5 * h),
    ], channel="#work")
    convo(10, "Dependency upgrade", [
        ("user", "Upgrade React to the latest minor and run the full test suite.", 9 * h),
        ("assistant", "Upgraded, and all 418 tests pass. I'll report back after tonight's e2e run.", 8.8 * h),
    ], channel="#work")
    convo(11, "Book club notes", [
        ("user", "Can you summarise chapter 4 for tonight?", 30 * 60),
    ], user="200000000000000002", name="Alex", lease=30 * 60, activity=("waiting for provider response", 20),
          channel="#books")

    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        """INSERT INTO async_delegations (delegation_id, origin_session, parent_session_id, state, delivery_state,
        dispatched_at, updated_at, task_json, result_json) VALUES (?,?,?,?,?,?,?,?,?)""",
        ("deleg_demo1", "demo_06", "demo_06", "running", "pending", now - 9 * 60, now - 30,
         json.dumps({"goal": "Run the integration tests against the new middleware", "user_id": OWNER}), None),
    )
    conn.execute(
        """INSERT INTO async_delegations (delegation_id, origin_session, parent_session_id, state, delivery_state,
        dispatched_at, updated_at, task_json, result_json) VALUES (?,?,?,?,?,?,?,?,?)""",
        ("deleg_demo2", "demo_09", "demo_09", "completed", "dropped", now - 7 * h, now - 6.5 * h,
         json.dumps({"goal": "Collect screenshots for the release notes", "user_id": OWNER}),
         json.dumps({"summary": "Captured 6 screenshots into docs/releases/img/."})),
    )
    conn.commit()
    conn.close()

    jobs = [
        {"id": "j1", "name": "Morning briefing", "enabled": True, "schedule_display": "every day 08:00",
         "next_run_at": _iso(now + 11 * h), "last_run_at": _iso(now - 13 * h), "last_status": "ok",
         "state": "scheduled"},
        {"id": "j2", "name": "Nightly backup check", "enabled": True, "schedule_display": "every day 02:00",
         "next_run_at": _iso(now + 5 * h), "last_run_at": _iso(now - 19 * h), "last_status": "error",
         "last_error": "rsync: connection to nas.local timed out", "state": "scheduled"},
        {"id": "j3", "name": "Inbox triage", "enabled": True, "schedule_display": "every 30m",
         "next_run_at": _iso(now + 17 * 60), "last_run_at": _iso(now - 13 * 60), "last_status": "ok",
         "state": "scheduled"},
        {"id": "j4", "name": "Price watch: standing desk", "enabled": True, "schedule_display": "every 6h",
         "next_run_at": _iso(now + 2 * h), "last_run_at": _iso(now - 4 * h), "last_status": "ok",
         "state": "scheduled"},
    ]
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    (home / "gateway_state.json").write_text(json.dumps({
        "gateway_state": "running",
        "platforms": {"discord": {"state": "connected"}, "telegram": {"state": "connected"}},
    }), encoding="utf-8")
    return home


if __name__ == "__main__":
    from tempfile import gettempdir

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(gettempdir()) / "duty-board-demo"
    if target.exists():
        raise SystemExit(f"{target} already exists; pick an empty path")
    print(build(target))

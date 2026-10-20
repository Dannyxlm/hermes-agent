"""Registered ActivityKit cleanup ownership, independent of ordinary outbox/lease TTLs."""
import hashlib
import json
import uuid

from .mobile_push_payloads import (
    SWIFT_EPOCH, TERMINAL, AlertPreview, Scope, activity_payload, agent_name, completion_summary, content_state,
)

ACTIVITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_cleanup (
 subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
 cleanup_until REAL NOT NULL, finished_at REAL NOT NULL DEFAULT 0,
 payload TEXT NOT NULL DEFAULT '', announced_at REAL NOT NULL DEFAULT 0,
 end_due REAL NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0);
"""


def _device_event(sub, run_id, purpose):
    identity = (sub["principal"], sub["installation_id"], sub["connection_id"], sub["scope"], run_id, purpose)
    return hashlib.sha256("\0".join(identity).encode()).hexdigest()


def _insert_job(db, sub, run_id, event_id, payload, expires, due, urgent=True):
    payload["event_id"] = hashlib.sha256(event_id.encode()).hexdigest()
    db.execute("""INSERT OR IGNORE INTO outbox
        (job_id, subscription_id, event_id, run_id, payload, urgent, collapse_id, expires_at, next_attempt, category)
        VALUES (?,?,?,?,?,?,?,?,?,'completion')""",
        (str(uuid.uuid4()), sub["id"], event_id, run_id, json.dumps(payload, separators=(",", ":")),
         int(urgent), sub["id"], expires, due))


class ActivityPushStore:
    def _register_activity_cleanup(self, db, sub, now):
        db.execute("""INSERT OR IGNORE INTO activity_cleanup (subscription_id, cleanup_until)
            VALUES (?,?)""", (sub["id"], now + 12 * 3600))
        db.execute("INSERT OR IGNORE INTO events VALUES (?,?)",
                   (_device_event(sub, sub["run_id"], "carried"), now))

    @staticmethod
    def _activity_carried(db, sub, run_id):
        marker = db.execute("SELECT 1 FROM events WHERE event_id=?",
                            (_device_event(sub, run_id, "carried"),)).fetchone()
        registered = db.execute("""SELECT 1 FROM subscriptions WHERE kind='activity' AND principal=?
            AND installation_id=? AND connection_id=? AND scope=? AND run_id=?""",
            (sub["principal"], sub["installation_id"], sub["connection_id"], sub["scope"], run_id)).fetchone()
        return marker is not None or registered is not None

    def _quiet_activity_completion(self, db, sub, run_id, payload):
        if payload.get("hermex.status") == "complete" and self._activity_carried(db, sub, run_id):
            payload["aps"]["interruption-level"] = "passive"
            payload["aps"].pop("sound", None)

    def _claim_completion_alert(self, db, job, now):
        payload = job["payload"]
        self._quiet_activity_completion(db, job, job["run_id"], payload)
        if payload.get("hermex.status") == "complete" and payload["aps"].get("sound"):
            first = db.execute("INSERT OR IGNORE INTO events VALUES (?,?)",
                               (_device_event(job, job["run_id"], "done-alert"), now)).rowcount
            if not first:
                payload["aps"]["interruption-level"] = "passive"
                payload["aps"].pop("sound", None)

    def _enqueue_terminal_activity(self, db, sub, run, scope, preview=None, *, announce=True):
        now = self.clock()
        self._register_activity_cleanup(db, sub, now)
        cleanup = db.execute("SELECT * FROM activity_cleanup WHERE subscription_id=?", (sub["id"],)).fetchone()
        if cleanup["cleanup_until"] <= now:
            return
        if cleanup["finished_at"]:
            # A recovered/rotated token still gets terminal replay, never another Done alert.
            db.execute("UPDATE activity_cleanup SET completed=0 WHERE subscription_id=?", (sub["id"],))
            if cleanup["completed"] or self._end_ready(cleanup, now):
                self._queue_activity_end(db, sub, cleanup["cleanup_until"], json.loads(cleanup["payload"]), now)
            return
        preview = preview or AlertPreview.from_json(run.get("presentation"))
        # Cleanup retains public words/counts only, not the reply/chat held for the Island beat.
        end = activity_payload(run, scope, now, preview=preview)
        done = (announce and run["status"] == "complete" and not run.get("activity_expired")
                and run["updated_at"] + 120 > now and sub["expires_at"] > now)
        db.execute("""UPDATE activity_cleanup SET finished_at=?, payload=?, end_due=?, announced_at=?
            WHERE subscription_id=?""", (run["updated_at"], json.dumps(end), now + 8 if done else now,
                                        0 if done else -1, sub["id"]))
        db.execute("DELETE FROM outbox WHERE subscription_id=? AND state='pending'", (sub["id"],))
        if done:
            update = activity_payload(run, scope, now, preview=preview, previews=self._device_previews(db, sub))
            aps = update["aps"]
            aps.pop("dismissal-date")
            aps.update(event="update", **{"stale-date": int(now + 120), "relevance-score": 100})
            _insert_job(db, sub, run["run_id"], f"{sub['id']}:{run['run_id']}:done", update,
                        run["updated_at"] + 120, now)
        else:
            self._queue_activity_end(db, sub, cleanup["cleanup_until"], end, now)

    @staticmethod
    def _queue_activity_end(db, sub, expires, payload, now):
        event_id = f"{sub['id']}:{sub['run_id']}:end:{sub['version']}"
        # A failed/reaped job must not discharge the durable obligation.
        db.execute("DELETE FROM outbox WHERE subscription_id=? AND event_id=? AND (state='failed' OR expires_at<=?)",
                   (sub["id"], event_id, now))
        _insert_job(db, sub, sub["run_id"], event_id, payload, expires, now)

    def _claim_activity_finish(self, db, job, now):
        aps = job["payload"]["aps"]
        if aps["event"] == "end":
            self._restore_final_preview(db, job, now)
            return
        if not aps["content-state"]["isFinal"]:
            return
        db.execute("""UPDATE activity_cleanup SET announced_at=?, end_due=?
            WHERE subscription_id=? AND announced_at=0""", (now, now + 8, job["subscription_id"]))
        # Persist BEFORE sending: a timeout/crash can lose the beat, never duplicate its alert.
        inserted = db.execute("INSERT OR IGNORE INTO events VALUES (?,?)",
                              (_device_event(job, job["run_id"], "done-alert"), now)).rowcount
        if inserted:
            content = aps["content-state"]
            run = {"started_at": content["startedAt"], "updated_at": content["updatedAt"]}
            plan = content.get("plan") or {}
            aps["alert"] = {"title": agent_name(job["profile"]) + " · Done",
                            "body": completion_summary(run, AlertPreview(plan_total=plan.get("total", 0))),
                            "sound": "default"}

    def _restore_final_preview(self, db, job, now):
        from .mobile_push_store import _RUNS
        aps = job["payload"]["aps"]
        if now >= aps["dismissal-date"] or not self._device_previews(db, job):
            return
        run = db.execute(f"{_RUNS} WHERE run_id=?", (job["run_id"],)).fetchone()
        if run is None:
            return
        content = aps["content-state"]
        projected = {**dict(run), "status": content["status"],
                     "started_at": content["startedAt"] + SWIFT_EPOCH,
                     "updated_at": content["updatedAt"] + SWIFT_EPOCH,
                     "activity_expired": content.get("activityExpired", False)}
        scope = Scope(run["surface"], run["profile"], run["session_id"])
        aps["content-state"] = content_state(projected, scope,
            preview=AlertPreview.from_json(run["presentation"]), previews=True)

    def _maintain_activity_cleanup(self, db, now):
        from .mobile_push_store import _RUNS
        # Import existing registered activities once on upgrade. Keep the predecessor's tables intact.
        db.execute("""INSERT OR IGNORE INTO activity_cleanup (subscription_id, cleanup_until)
            SELECT id, ? FROM subscriptions WHERE kind='activity'""", (now + 12 * 3600,))
        rows = db.execute("""SELECT s.*, c.finished_at, c.payload AS end_payload, c.cleanup_until,
            c.end_due, c.announced_at, c.completed FROM subscriptions s
            JOIN activity_cleanup c ON c.subscription_id=s.id WHERE c.cleanup_until>?""", (now,)).fetchall()
        for sub in rows:
            if not sub["finished_at"]:
                run = db.execute(f"{_RUNS} WHERE run_id=?", (sub["run_id"],)).fetchone()
                if run is None:
                    continue
                self._register_activity_cleanup(db, sub, now)
                if run["status"] in TERMINAL or sub["expires_at"] <= now + 120:
                    projected = dict(run)
                    if run["status"] not in TERMINAL:
                        projected.update(status="cancelled", updated_at=min(now, sub["expires_at"]), activity_expired=True)
                    self._enqueue_terminal_activity(db, sub, projected,
                        Scope(sub["surface"], sub["profile"], sub["session_id"]), announce=False)
                else:
                    self._heartbeat_activity(db, sub, dict(run), now)
            elif not sub["completed"] and self._end_ready(sub, now):
                db.execute("DELETE FROM outbox WHERE subscription_id=? AND state='pending' AND json_extract(payload, '$.aps.event')='update'",
                           (sub["id"],))
                self._queue_activity_end(db, sub, sub["cleanup_until"], json.loads(sub["end_payload"]), now)
        db.execute("DELETE FROM subscriptions WHERE id IN (SELECT subscription_id FROM activity_cleanup WHERE cleanup_until<=?)", (now,))

    @staticmethod
    def _end_ready(sub, now):
        # Never let a delayed worker show an old Done alert; the final card's deadline is fixed.
        return sub["finished_at"] + 120 <= now or sub["announced_at"] != 0 and sub["end_due"] <= now

    def _heartbeat_activity(self, db, sub, run, now):
        if run["status"] != "usingTool" or sub["expires_at"] <= now + 120:
            return
        preview = AlertPreview.from_json(run.get("presentation"))
        started = preview.step_started_at if preview and preview.step_started_at else run["updated_at"]
        if now - started <= 120 or now - sub["last_timestamp"] < 100:
            return
        if db.execute("SELECT 1 FROM outbox WHERE subscription_id=? AND state='pending'", (sub["id"],)).fetchone():
            return
        scope = Scope(sub["surface"], sub["profile"], sub["session_id"])
        payload = activity_payload(run, scope, now, preview=preview, previews=self._device_previews(db, sub))
        _insert_job(db, sub, run["run_id"], f"{sub['id']}:heartbeat:{int(now)}", payload,
                    min(sub["expires_at"], now + 120), now, urgent=False)

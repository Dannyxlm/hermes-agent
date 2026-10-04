"""Private, device-scoped photo previews and OCR. Apple Photos owns originals."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_PREVIEW = 256 * 1024
MIN_FREE_BYTES = 5 * 1024**3


class PhotoError(ValueError):
    pass


class PhotoStorageFull(PhotoError):
    pass


def token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise PhotoError("Invalid identifier")
    return value


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise PhotoError("Invalid date")
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise PhotoError("Invalid count")
    return value


class PhotoCatalog:
    def __init__(self, path: Path, free_bytes=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.free_bytes = free_bytes or (
            lambda: shutil.disk_usage(self.path.parent).free
        )
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, profile TEXT NOT NULL,
                    secret TEXT NOT NULL, enabled INTEGER NOT NULL, epoch TEXT,
                    expected INTEGER NOT NULL DEFAULT 0, finished REAL, seen REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS manifest (
                    device TEXT NOT NULL, asset TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    epoch TEXT NOT NULL, PRIMARY KEY(device,asset));
                CREATE INDEX IF NOT EXISTS manifest_epoch ON manifest(device,epoch);
                CREATE TABLE IF NOT EXISTS photos (
                    device TEXT NOT NULL, asset TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    created REAL NOT NULL, albums TEXT NOT NULL, text TEXT NOT NULL,
                    favorite INTEGER NOT NULL, screenshot INTEGER NOT NULL,
                    preview BLOB NOT NULL, PRIMARY KEY(device,asset));
                CREATE INDEX IF NOT EXISTS photo_date ON photos(device,created DESC,asset);
                CREATE VIRTUAL TABLE IF NOT EXISTS photo_text USING fts5(text, albums, content='photos', content_rowid='rowid');
                CREATE TRIGGER IF NOT EXISTS photo_insert AFTER INSERT ON photos BEGIN
                    INSERT INTO photo_text(rowid,text,albums) VALUES(new.rowid,new.text,new.albums); END;
                CREATE TRIGGER IF NOT EXISTS photo_delete AFTER DELETE ON photos BEGIN
                    INSERT INTO photo_text(photo_text,rowid,text,albums) VALUES('delete',old.rowid,old.text,old.albums); END;
                CREATE TRIGGER IF NOT EXISTS photo_update AFTER UPDATE ON photos BEGIN
                    INSERT INTO photo_text(photo_text,rowid,text,albums) VALUES('delete',old.rowid,old.text,old.albums);
                    INSERT INTO photo_text(rowid,text,albums) VALUES(new.rowid,new.text,new.albums); END;
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _device(self, db, device, profile, principal=None, secret=None, disabled=False):
        try:
            if device != str(uuid.UUID(device)):
                raise ValueError("Noncanonical device")
        except (ValueError, TypeError, AttributeError) as exc:
            raise PhotoError("Invalid device") from exc
        row = db.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
        if (
            not row
            or row["profile"] != profile
            or (not disabled and not row["enabled"])
            or (principal is not None and row["principal"] != principal)
            or (
                secret is not None
                and not hmac.compare_digest(
                    row["secret"], hashlib.sha256(token(secret).encode()).hexdigest()
                )
            )
        ):
            raise PhotoError("Device unavailable")
        return row

    def register(self, principal, profile, device, secret):
        try:
            if device != str(uuid.UUID(device)):
                raise ValueError("Noncanonical device")
        except (ValueError, TypeError, AttributeError) as exc:
            raise PhotoError("Invalid device") from exc
        digest = hashlib.sha256(token(secret).encode()).hexdigest()
        with self.connection() as db:
            if db.execute("SELECT 1 FROM devices WHERE id=?", (device,)).fetchone():
                self._device(db, device, profile, principal, secret)
            else:
                db.execute(
                    "INSERT INTO devices(id,principal,profile,secret,enabled,seen) VALUES(?,?,?,?,1,?)",
                    (device, principal, profile, digest, time.time()),
                )
        return self.status(profile, device, principal)

    def rebind(self, principal, profile, device, secret):
        """Explicit owner-authorized migration; prove the old device secret first.

        Disabled rows are tombstones, never revived. Catalog/epochs stay intact.
        """
        with self.connection() as db:
            self._device(db, device, profile, secret=secret)
            db.execute("UPDATE devices SET principal=? WHERE id=?", (principal, device))
        return self.status(profile, device, principal)

    def _epoch(self, db, device, profile, principal, secret, epoch):
        row = self._device(db, device, profile, principal, token(secret))
        if not isinstance(epoch, str) or not epoch or row["epoch"] != epoch:
            raise PhotoError("Sync superseded")
        return row

    def begin(self, principal, profile, device, secret, count):
        count = integer(count, 0, 10_000_000)
        epoch = str(uuid.uuid4())
        with self.connection() as db:
            self._device(db, device, profile, principal, token(secret))
            db.execute(
                "UPDATE devices SET epoch=?,expected=?,finished=NULL,seen=? WHERE id=?",
                (epoch, count, time.time(), device),
            )
        return {"ok": True, "protocol_version": 1, "epoch": epoch}

    def manifest(self, principal, profile, device, secret, epoch, items):
        if not isinstance(items, list) or len(items) > 500:
            raise PhotoError("Invalid manifest")
        pairs = [
            (token(a.get("id")), token(a.get("fingerprint")))
            for a in items
            if isinstance(a, dict)
        ]
        if len(pairs) != len(items) or len({a for a, _ in pairs}) != len(pairs):
            raise PhotoError("Invalid manifest")
        with self.connection() as db:
            self._epoch(db, device, profile, principal, secret, epoch)
            missing = []
            for asset, fingerprint in pairs:
                db.execute(
                    "INSERT INTO manifest VALUES(?,?,?,?) ON CONFLICT(device,asset) DO UPDATE SET fingerprint=excluded.fingerprint,epoch=excluded.epoch",
                    (device, asset, fingerprint, epoch),
                )
                old = db.execute(
                    "SELECT fingerprint FROM photos WHERE device=? AND asset=?",
                    (device, asset),
                ).fetchone()
                if not old or old["fingerprint"] != fingerprint:
                    missing.append(asset)
        return {"ok": True, "protocol_version": 1, "missing": missing}

    def put(self, principal, profile, device, secret, epoch, item):
        if not isinstance(item, dict):
            raise PhotoError("Invalid photo")
        asset, fingerprint = token(item.get("id")), token(item.get("fingerprint"))
        created = number(item.get("created"))
        albums, text = item.get("albums"), item.get("text")
        if (
            not isinstance(text, str)
            or len(text.encode()) > 65536
            or not isinstance(albums, list)
            or len(albums) > 100
            or any(not isinstance(a, str) or len(a) > 1000 for a in albums)
            or type(item.get("favorite")) is not bool
            or type(item.get("screenshot")) is not bool
        ):
            raise PhotoError("Invalid metadata")
        encoded = item.get("preview")
        if not isinstance(encoded, str) or len(encoded) > 350000:
            raise PhotoError("Invalid preview")
        try:
            preview = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise PhotoError("Invalid preview") from exc
        if (
            not 4 <= len(preview) <= MAX_PREVIEW
            or not preview.startswith(b"\xff\xd8\xff")
            or not preview.endswith(b"\xff\xd9")
        ):
            raise PhotoError("Invalid JPEG preview")
        if self.free_bytes() < MIN_FREE_BYTES + len(preview) * 3:
            raise PhotoStorageFull("Server storage low; sync paused")
        with self.connection() as db:
            self._epoch(db, device, profile, principal, secret, epoch)
            authorized = db.execute(
                "SELECT 1 FROM manifest WHERE device=? AND asset=? AND fingerprint=? AND epoch=?",
                (device, asset, fingerprint, epoch),
            ).fetchone()
            if not authorized:
                raise PhotoError("Photo not in authorized manifest")
            db.execute(
                """INSERT INTO photos VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(device,asset) DO UPDATE SET
                fingerprint=excluded.fingerprint,created=excluded.created,albums=excluded.albums,text=excluded.text,
                favorite=excluded.favorite,screenshot=excluded.screenshot,preview=excluded.preview""",
                (
                    device,
                    asset,
                    fingerprint,
                    created,
                    json.dumps(albums, ensure_ascii=False),
                    text,
                    item["favorite"],
                    item["screenshot"],
                    preview,
                ),
            )
        return {"ok": True, "protocol_version": 1}

    def finish(self, principal, profile, device, secret, epoch):
        with self.connection() as db:
            row = self._epoch(db, device, profile, principal, secret, epoch)
            count = db.execute(
                "SELECT count(*) FROM manifest WHERE device=? AND epoch=?",
                (device, epoch),
            ).fetchone()[0]
            if count != row["expected"]:
                raise PhotoError("Manifest incomplete")
            db.execute(
                "DELETE FROM photos WHERE device=? AND asset NOT IN (SELECT asset FROM manifest WHERE device=? AND epoch=?)",
                (device, device, epoch),
            )
            db.execute(
                "DELETE FROM manifest WHERE device=? AND epoch!=?", (device, epoch)
            )
            db.execute(
                "UPDATE devices SET finished=?,seen=? WHERE id=?",
                (time.time(), time.time(), device),
            )
        return self.status(profile, device, principal)

    @staticmethod
    def _visible():
        return " FROM photos p JOIN manifest m ON m.device=p.device AND m.asset=p.asset AND m.fingerprint=p.fingerprint JOIN devices d ON d.id=p.device AND d.epoch=m.epoch WHERE d.enabled=1 AND d.profile=? AND d.id=? "

    def _stats(self, db, row):
        count, size = db.execute(
            "SELECT count(*),coalesce(sum(length(p.preview)+length(CAST(p.text AS BLOB))+length(CAST(p.albums AS BLOB))),0)"
            + self._visible(),
            (row["profile"], row["id"]),
        ).fetchone()
        return {
            "id": row["id"],
            "count": count,
            "total": row["expected"],
            "storage_bytes": size,
            "last_sync": row["finished"],
            "complete": row["finished"] is not None and count == row["expected"],
            "epoch": row["epoch"],
        }

    def status(self, profile, device, principal=None):
        with self.connection() as db:
            row = self._device(db, device, profile, principal)
            return {"ok": True, "protocol_version": 1, **self._stats(db, row)}

    def devices(self, profile):
        with self.connection() as db:
            return [
                self._stats(db, row)
                for row in db.execute(
                    "SELECT * FROM devices WHERE profile=? AND enabled=1 ORDER BY seen DESC",
                    (profile,),
                ).fetchall()
            ]

    def search(self, profile, device, query, principal=None):
        if not isinstance(query, dict):
            raise PhotoError("Invalid query")
        limit = integer(query.get("limit", 30), 1, 100)
        cursor = query.get("cursor")
        text = query.get("text", "")
        if not isinstance(text, str) or len(text) > 500:
            raise PhotoError("Invalid query")
        clauses = []
        args = [profile, device]
        words = re.findall(r"\w+", text, flags=re.UNICODE)
        if words:
            clauses.append(
                "p.rowid IN (SELECT rowid FROM photo_text WHERE photo_text MATCH ?)"
            )
            args.append(" AND ".join('"' + w + '"' for w in words))
        for key, op in [("after", ">="), ("before", "<")]:
            if key in query:
                clauses.append("p.created " + op + " ?")
                args.append(number(query[key]))
        for key in ["favorite", "screenshot"]:
            if key in query:
                if type(query[key]) is not bool:
                    raise PhotoError("Invalid filter")
                clauses.append("p." + key + "=?")
                args.append(query[key])
        with self.connection() as db:
            row = self._device(db, device, profile, principal)
            signature = hashlib.sha256(
                json.dumps(
                    {k: v for k, v in query.items() if k != "cursor"}, sort_keys=True
                ).encode()
            ).hexdigest()
            if cursor is not None:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise PhotoError("Invalid cursor")
                try:
                    page = json.loads(base64.b64decode(cursor, validate=True))
                    if (
                        page["epoch"] != row["epoch"]
                        or page["device"] != device
                        or page["query"] != signature
                    ):
                        raise PhotoError("Catalog changed; restart search")
                    created = number(page["created"])
                    asset = token(page["asset"])
                except (ValueError, TypeError, KeyError) as exc:
                    raise PhotoError("Invalid cursor") from exc
                clauses.append("(p.created < ? OR (p.created = ? AND p.asset > ?))")
                args.extend([created, created, asset])
            rows = db.execute(
                "SELECT p.asset AS id,p.created,p.albums,p.text,p.favorite,p.screenshot"
                + self._visible()
                + "".join(" AND " + c for c in clauses)
                + " ORDER BY p.created DESC,p.asset LIMIT ?",
                (*args, limit + 1),
            ).fetchall()
            items = [
                {**dict(r), "albums": json.loads(r["albums"])} for r in rows[:limit]
            ]
            return {
                "ok": True,
                "protocol_version": 1,
                "items": items,
                "next_cursor": base64.b64encode(
                    json.dumps(
                        {
                            "epoch": row["epoch"],
                            "device": device,
                            "query": signature,
                            "created": items[-1]["created"],
                            "asset": items[-1]["id"],
                        }
                    ).encode()
                ).decode()
                if len(rows) > limit
                else None,
                "catalog": self._stats(db, row),
                "search_kind": "metadata_and_ocr",
            }

    def preview(self, profile, device, asset, principal=None):
        token(asset)
        with self.connection() as db:
            self._device(db, device, profile, principal)
            row = db.execute(
                "SELECT p.preview" + self._visible() + " AND p.asset=?",
                (profile, device, asset),
            ).fetchone()
            if not row:
                raise PhotoError("Photo unavailable")
            return row["preview"]

    def disconnect(self, principal, profile, device, secret):
        with self.connection() as db:
            self._device(db, device, profile, principal, token(secret), disabled=True)
            db.execute("UPDATE devices SET enabled=0 WHERE id=?", (device,))
            db.execute("DELETE FROM photos WHERE device=?", (device,))
            db.execute("DELETE FROM manifest WHERE device=?", (device,))
        return {"ok": True, "protocol_version": 1}

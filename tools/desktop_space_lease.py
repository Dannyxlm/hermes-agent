"""Durable resource coordination, not a Cua input authorization mechanism.

A backend must stop in-flight input and invalidate old activation tickets before
calling acquire. These primitives alone cannot establish either condition.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time


RESOURCES = frozenset({'desktop', 'studio'})


class LeaseConflict(RuntimeError):
    pass


def resource_id(value: str) -> str:
    if value not in RESOURCES:
        raise ValueError('Invalid desktop resource')
    return value


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class SpaceLease:
    holder: str = 'none'
    epoch: int = 0
    scope_hash: str = ''
    viewer_hash: str = ''
    expires_at: float = 0

    def human_matches(self, scope: str, viewer: str, epoch: int, now: float) -> bool:
        return (self.holder == 'human' and self.epoch == epoch and now < self.expires_at
                and self.scope_hash == digest(scope) and self.viewer_hash == digest(viewer))

    def public(self, scope: str, now: float) -> dict:
        expired = self.holder == 'human' and now >= self.expires_at
        return {'holder': 'none' if self.holder == 'human_pending' else self.holder, 'epoch': self.epoch,
                'mine': self.holder == 'human' and not expired and self.scope_hash == digest(scope),
                'expires_at': self.expires_at if self.holder == 'human' else None}


class LeaseStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._initialize_at(self.path)
            return
        with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='.desktop-', suffix='.sqlite', delete=False) as file:
            staging = Path(file.name)
        try:
            self._initialize_at(staging)
            try:
                os.link(staging, self.path)
            except FileExistsError:
                pass
        finally:
            staging.unlink()

    @staticmethod
    def _initialize_at(path: Path) -> None:
        # Publish the first database only after its schema is committed, at mode 0600.
        with closing(sqlite3.connect(path, timeout=5)) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS leases (
                    resource TEXT PRIMARY KEY, holder TEXT NOT NULL, epoch INTEGER NOT NULL,
                    scope_hash TEXT NOT NULL, viewer_hash TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions (
                    resource TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS signins (
                    resource TEXT NOT NULL, scope_hash TEXT NOT NULL, request_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, origin TEXT NOT NULL, reason TEXT NOT NULL,
                    expires_at REAL NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (resource, scope_hash));
                CREATE TABLE IF NOT EXISTS signin_bindings (
                    resource TEXT NOT NULL, scope_hash TEXT NOT NULL, request_id TEXT NOT NULL,
                    login_handle TEXT, masked_identifier TEXT, task_id TEXT NOT NULL,
                    target_id TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY (resource, scope_hash));
                CREATE TABLE IF NOT EXISTS receipts (
                    resource TEXT NOT NULL, scope_hash TEXT NOT NULL, action_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (resource, scope_hash, action_id));
                CREATE TABLE IF NOT EXISTS desktop_operations (
                    resource TEXT NOT NULL, scope_hash TEXT NOT NULL, action_id TEXT NOT NULL,
                    action TEXT NOT NULL, viewer_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL,
                    PRIMARY KEY (resource, scope_hash, action_id));
                CREATE TABLE IF NOT EXISTS activity (
                    id TEXT PRIMARY KEY, resource TEXT NOT NULL, scope_hash TEXT NOT NULL,
                    label TEXT NOT NULL, at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS activity_scope ON activity(resource, scope_hash, at DESC);
            ''')

    @contextmanager
    def connection(self, *, write: bool = False):
        if write:
            self.initialize()
        if not self.path.exists():
            yield None
            return
        uri = self.path.resolve().as_uri() + ('?mode=rw' if write else '?mode=ro')
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def lease_in(db, resource: str) -> SpaceLease:
        row = db.execute('SELECT * FROM leases WHERE resource=?', (resource,)).fetchone() if db else None
        return SpaceLease(**{key: row[key] for key in ('holder', 'epoch', 'scope_hash', 'viewer_hash', 'expires_at')}) if row else SpaceLease()

    def read(self, resource: str) -> SpaceLease:
        resource_id(resource)
        with self.connection() as db:
            return self.lease_in(db, resource)

    def acquire(self, resource: str, scope: str, viewer: str, *, expected_epoch: int,
                now: float | None = None, ttl: float = 60) -> SpaceLease:
        resource_id(resource)
        now = time.time() if now is None else now
        if not scope or not viewer or not math.isfinite(now) or not 0 < ttl <= 300:
            raise ValueError('Invalid desktop lease')
        with self.connection(write=True) as db:
            previous = self.lease_in(db, resource)
            if previous.epoch != expected_epoch or (previous.holder == 'human' and now < previous.expires_at):
                raise LeaseConflict('Desktop lease changed')
            lease = SpaceLease('human', previous.epoch + 1, digest(scope), digest(viewer), now + ttl)
            self._save(db, resource, lease)
            return lease

    def release(self, resource: str, scope: str, viewer: str, *, expected_epoch: int,
                now: float | None = None) -> SpaceLease:
        resource_id(resource)
        now = time.time() if now is None else now
        with self.connection(write=True) as db:
            previous = self.lease_in(db, resource)
            if not previous.human_matches(scope, viewer, expected_epoch, now):
                raise LeaseConflict('Desktop lease changed')
            lease = SpaceLease('agent', previous.epoch + 1)
            self._save(db, resource, lease)
            return lease

    @staticmethod
    def _save(db, resource: str, lease: SpaceLease) -> None:
        db.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?,?)',
                   (resource, lease.holder, lease.epoch, lease.scope_hash, lease.viewer_hash, lease.expires_at))
        db.execute('INSERT INTO revisions VALUES(?,2) ON CONFLICT(resource) DO UPDATE SET revision=revision+1', (resource,))

    def permits(self, resource: str, scope: str, viewer: str, epoch: int, *, now: float | None = None) -> bool:
        return self.read(resource).human_matches(scope, viewer, epoch, time.time() if now is None else now)

    def agent_permitted(self, resource: str, epoch: int) -> bool:
        lease = self.read(resource)
        return lease.holder in ('none', 'agent') and lease.epoch == epoch

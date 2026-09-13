"""Process-owned APNs worker; reusable by authenticated Team and Chats adapters."""

import atexit
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .mobile_push_payloads import Scope
from .mobile_push_provider import APNsProvider
from .mobile_push_store import PushStore

logger = logging.getLogger(__name__)


class PushService:
    def __init__(self, path, sender, *, clock=time.time, environments=("production",)):
        self.store = PushStore(path, clock)
        self.sender = sender
        self.environments = tuple(environments)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._drain_lock = threading.Lock()
        self._thread = None
        self._closed = False

    def register(self, principal, scope, **params):
        if params.get("environment") not in self.environments:
            raise ValueError("delivery environment unavailable")
        receipt = self.store.register(principal, scope, **params)
        self._wake.set()
        return receipt

    def unregister(self, principal, **params):
        return self.store.unregister(principal, **params)

    def refresh(self, principal, **params):
        if params.get("environment") not in self.environments:
            raise ValueError("delivery environment unavailable")
        receipt = self.store.refresh(principal, **params)
        self._wake.set()
        return receipt

    def start_run(self, scope, run_id=None, event_id=None):
        return self.store.start_run(scope, run_id, event_id)

    def current_run(self, scope):
        run = self.store.current_run(scope)
        return {key: run[key] for key in ("run_id", "status", "started_at", "updated_at")} if run else None

    def record(self, scope, run_id, event_id, status, *, preview=None):
        result = self.store.record(scope, run_id, event_id, status, preview=preview)
        self._wake.set()
        return result

    def drain_once(self):
        with self._drain_lock:
            self.store.maintain()
            count = 0
            for _ in range(20):
                if self._stop.is_set():
                    break
                job = self.store.claim()
                if job is None:
                    break
                result = self.sender.send(job)
                self.store.finish(job, result)
                count += 1
            return count

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="mobile-push-outbox", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception as error:
                # SQLite/HTTP failures must not kill canonical agent work or expose a token
                # through an exception URL. A leased job becomes available after 30 seconds.
                logger.warning("mobile push delivery unavailable (%s)", type(error).__name__)
            self._wake.wait(2)
            self._wake.clear()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=12)
            if self._thread.is_alive():
                return  # the process exits; do not close SQLite beneath an in-flight worker
        with self._drain_lock:
            if self._closed:
                return
            self.sender.close()
            self.store.close()
            self._closed = True


_services = {}
_services_lock = threading.Lock()


def service_for_home(home):
    """Configuration is loaded once per process; restart after changing its normal config."""
    from hermes_cli.config import read_user_config_raw

    home = Path(home).resolve()
    with _services_lock:
        if home in _services:
            return _services[home]
        service = None
        provider = None
        try:
            config = read_user_config_raw(home / "config.yaml").get("mobile_push", {})
            if not isinstance(config, dict):
                raise ValueError("invalid mobile push configuration")
            if config.get("enabled") is True:
                environments = config.get("environments", ["production"])
                if (not isinstance(environments, list) or not environments
                        or any(v not in {"production", "sandbox"} for v in environments)):
                    raise ValueError("invalid environments")
                provider = APNsProvider(config)
                service = PushService(home / "mobile-push" / "outbox.sqlite3", provider, environments=environments)
                service.start()
                atexit.register(service.close)
        except (OSError, ValueError, KeyError, ImportError, TypeError, sqlite3.Error) as error:
            if provider is not None:
                provider.close()
            logger.warning("mobile push configuration unavailable (%s)", type(error).__name__)
        _services[home] = service
        return service


def unregister_for_home(home, principal, **params):
    """Opt-out remains effective if delivery is disabled or its credential is missing."""
    service = service_for_home(home)
    if service:
        return service.unregister(principal, **params)
    path = Path(home) / "mobile-push" / "outbox.sqlite3"
    if not path.is_file():
        return 0
    store = PushStore(path, time.time)
    try:
        return store.unregister(principal, **params)
    finally:
        store.close()

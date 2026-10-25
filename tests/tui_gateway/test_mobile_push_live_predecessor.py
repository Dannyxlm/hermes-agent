"""The live predecessor must keep writing through the persistent device constraints."""

import importlib
import importlib.util
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

from tui_gateway.mobile_push_store import PushStore


_MODULES = ("mobile_push_store", "mobile_push_payloads", "mobile_push_activity", "mobile_push_widgets",
            "mobile_push_attention", "mobile_inbox_invalidation")


def test_live_predecessor_writes_all_registration_paths_after_migration(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    name = "_push_predecessor_" + uuid.uuid4().hex
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    for module in _MODULES:
        shown = subprocess.run(["git", "-C", str(repo), "show", f"1b73f32d:tui_gateway/{module}.py"],
                               check=True, capture_output=True, text=True)
        (package / f"{module}.py").write_text(shown.stdout)
    spec = importlib.util.spec_from_file_location(name, package / "__init__.py",
                                                submodule_search_locations=[str(package), str(repo / "tui_gateway")])
    sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sys.modules[name])
    path = tmp_path / "rollback.sqlite"
    current = PushStore(path, lambda: 1800000000.0)
    current.close()
    old = None
    try:
        predecessor = importlib.import_module(name + ".mobile_push_store")
        payloads = importlib.import_module(name + ".mobile_push_payloads")
        inbox = importlib.import_module(name + ".mobile_inbox_invalidation")
        old = predecessor.PushStore(path, lambda: 1800000000.0)
        ids = {"installation_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())}
        scope = payloads.Scope("native_session", "ops", "root")
        for n, principal in enumerate(("first-human", "second-human")):
            for _ in range(2):
                old.register(principal, scope, **ids, token="aa" * 32, environment="production")
            old.set_policy(principal, **ids, policy="all", token="aa" * 32, environment="production")
            old.register_start_token(principal, **ids, token="bb" * 32, environment="production")
            inbox.register(old, principal, **ids, profile="ops", device_token="aa" * 32, environment="production")
            old.register(principal, scope, **ids, kind="widget", token="cc" * 32,
                         read_token=f"{n + 1:064x}", environment="production")
        run = old.start_run(scope, listed=True)
        old.record(scope, run["run_id"], "ask", "waitingForApproval")
        old.refresh("second-human", **ids, token="aa" * 32, environment="production", accepts_scope=lambda _: True)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT kind,COUNT(*) FROM subscriptions GROUP BY kind ORDER BY kind").fetchall() == [
                ("alert", 1), ("background", 1), ("widget", 1)]
            assert db.execute("SELECT COUNT(*) FROM outbox o JOIN subscriptions s ON s.id=o.subscription_id WHERE s.kind='alert'").fetchone()[0] == 1
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert old.unregister("second-human", **ids) == 3
    finally:
        if old is not None:
            old.close()
        for module in [key for key in sys.modules if key == name or key.startswith(name + ".")]:
            del sys.modules[module]

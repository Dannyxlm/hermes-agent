"""Round 9 U30 (R25): the dispatcher does not report "stuck" when caps explain the hold.

During round 9 the default lane held ready cards behind
``max_in_progress_per_profile: 1`` and the gateway logged "kanban dispatcher
stuck" with an empty reason (51 lines since 10-06). A cap hold is busy, not
stuck; a ready card that no cap explains must still warn.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


@pytest.fixture()
def kanban(monkeypatch):
    home = tempfile.mkdtemp(prefix="kanban_suppression_caps_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(home, "profiles", prof), exist_ok=True)
        with open(os.path.join(home, "profiles", prof, "config.yaml"), "w") as fh:
            fh.write("{}\n")
    monkeypatch.setenv("HERMES_HOME", home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
    return kanban_db, kanban_db_connect, kanban_db_dispatch


def _spawn(*_args, **_kwargs):
    return os.getpid()  # a live pid: the next tick must not reclaim the "worker" as crashed


def _tick(kbc, kbd, **caps):
    with kbc.connect_closing() as conn:
        return kbd.dispatch_once(conn, spawn_fn=_spawn, default_assignee="", **caps)


def test_profile_cap_hold_is_explained(kanban):
    kb, kbc, kbd = kanban
    with kbc.connect_closing() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")
    first = _tick(kbc, kbd, max_in_progress_per_profile=1)
    assert len(first.spawned) == 1

    held = _tick(kbc, kbd, max_in_progress_per_profile=1)

    assert held.spawned == []
    assert len(held.skipped_per_profile_capped) == 2
    assert kbd.caps_explain_hold([held]) is True
    assert "profile_cap=2" in kbd.describe_suppression([held])


def test_global_cap_hold_is_explained(kanban):
    kb, kbc, kbd = kanban
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="a0", assignee="alpha")
        kb.create_task(conn, title="b0", assignee="beta")
    assert len(_tick(kbc, kbd, max_in_progress=1).spawned) == 1

    held = _tick(kbc, kbd, max_in_progress=1)

    assert held.spawned == []
    assert kbd.caps_explain_hold([held]) is True
    assert "max_in_progress" in kbd.describe_suppression([held])


def test_mixed_queue_with_genuinely_stuck_card_still_warns(kanban):
    kb, kbc, kbd = kanban
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="a0", assignee="alpha")
        kb.create_task(conn, title="a1", assignee="alpha")
    assert len(_tick(kbc, kbd, max_in_progress_per_profile=1).spawned) == 1
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="needs routing", assignee=None)

    held = _tick(kbc, kbd, max_in_progress_per_profile=1)

    assert held.spawned == []
    assert held.skipped_per_profile_capped and held.skipped_unassigned
    assert kbd.caps_explain_hold([held]) is False


def test_no_cap_involved_is_not_explained(kanban):
    _kb, _kbc, kbd = kanban
    assert kbd.caps_explain_hold([kbd.DispatchResult()]) is False
    assert kbd.caps_explain_hold([None]) is False
    locked = kbd.DispatchResult(skipped_locked=True)
    capped = kbd.DispatchResult(skipped_per_profile_capped=[("t", "alpha", 1)])
    assert kbd.caps_explain_hold([locked, capped]) is False

"""A delegated child closed before it ran records why its launch ended."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.delegate_tool_child_run import _close_child


class _LaunchDB:
    def __init__(self):
        self.finished = []

    def finish_launch(self, launch_id, cause):
        self.finished.append((launch_id, cause))


@pytest.mark.parametrize(("stop_cause", "expected"), [
    (None, "failed_start"), ("parent_stop", "parent_stop"), ("user_stop", "user_stop")])
def test_unstarted_child_launch_keeps_the_stop_that_ended_it(stop_cause, expected):
    db = _LaunchDB()
    closed = []
    child = SimpleNamespace(_launch_id="launch-1", _session_db=db, _launch_executed=False,
                            close=lambda: closed.append(True))
    if stop_cause is not None:
        child._launch_stop_cause = stop_cause

    _close_child(child, "close failed")

    assert db.finished == [("launch-1", expected)]
    assert closed == [True]


def test_started_child_close_leaves_its_launch_to_the_run():
    db = _LaunchDB()
    child = SimpleNamespace(_launch_id="launch-1", _session_db=db, _launch_executed=True, close=lambda: None)

    _close_child(child, "close failed")

    assert db.finished == []

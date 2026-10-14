"""setup_choose answers for app-drawn lists settle; server-drawn rows stay authoritative.

Desktop (apps/desktop/src/store/clarify.ts setupChooseAnswer/answerSetupChoose) draws the accent,
theme, layout, connectors and plugins lists itself and answers a free-text question card with the
typed text as ``picked``; setup_choose_tool sends ``options=None`` for those cards. Option
membership and labels are enforced only when the server supplied the rows (U8 F1).
"""
import sys
import threading

import pytest

from tui_gateway import server_requests as sr
from tui_gateway.human_input import validate_answer


@pytest.fixture
def requests():
    sr.reset_for_tests()
    sr.bind_sinks(lambda frame: None, lambda *args: None, lambda sid: True)
    yield sr
    sr.cancel()
    sr.reset_for_tests()


def _ask(kind, multi, seen, options=None):
    sr.send_async("setup_choose", "runtime", {"kind": kind, "question": "Pick", "options": options,
                                              "multi_select": multi, "preselected": []},
                  seen.append, timeout=600)
    return sr.open_requests("runtime")[0]["id"]


@pytest.mark.parametrize("kind,multi,answer", [
    ("accent", False, {"picked": "#8a2be2", "label": "Violet"}),
    ("theme", False, {"picked": "dark"}),
    ("layout", False, {"picked": "compact"}),
    ("connectors", True, {"picked": ["gmail", "slack"]}),
    ("plugins", True, {"picked": ["some-plugin"]}),
    ("question", False, {"picked": "Ada"}),
])
def test_desktop_setup_answers_for_app_owned_lists(requests, kind, multi, answer):
    seen = []
    rid = _ask(kind, multi, seen)
    assert sr.resolve_response({"id": rid, "result": answer}) is True
    assert seen == [answer]
    assert sr.open_requests("runtime") == []


def test_desktop_setup_answer_settles_through_server_dispatch(requests, monkeypatch):
    """The renderer's JSON-RPC response frame reaches the producer through the real server.dispatch."""
    from hermes_cli import banner

    # The real server binds callbacks on import; isolate only its process-wide side effects.
    monkeypatch.setattr(banner, "prefetch_update_check", lambda: None)
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    from tui_gateway import server

    class Transport:
        def write(self, frame):
            return True

    for kind, multi, answer in [("theme", False, {"picked": "dark"}),
                                ("connectors", True, {"picked": ["gmail", "slack"]}),
                                ("question", False, {"picked": "Ada"})]:
        seen = []
        rid = _ask(kind, multi, seen)
        assert server.dispatch({"jsonrpc": "2.0", "id": rid, "result": answer}, Transport()) is None
        assert seen == [answer]
        assert sr.open_requests("runtime") == []


def test_server_rows_refuse_invented_and_duplicate_picks(requests):
    seen = []
    rid = _ask("question", False, seen, options=[{"id": "a", "label": "A"}])
    for bad in ({"picked": "zzz"}, {"picked": ["a", "a"]}, {"picked": "a", "label": "Not A"}):
        with pytest.raises(ValueError):
            sr.resolve_response({"id": rid, "result": bad})
    assert seen == [] and len(sr.open_requests("runtime")) == 1
    assert sr.resolve_response({"id": rid, "result": {"picked": "a", "label": "A"}}) is True
    assert seen == [{"picked": "a", "label": "A"}]


def test_setup_choose_unknown_pick_refused_and_said_allowed():
    params = {"kind": "question", "question": "q", "options": [{"id": "a", "label": "A"}],
              "multi_select": False, "preselected": []}
    with pytest.raises(ValueError):
        validate_answer("setup_choose", params, {"picked": "zzz"})
    with pytest.raises(ValueError):
        validate_answer("setup_choose", params, {"picked": ["a", "a"]})
    assert validate_answer("setup_choose", params, {"said": "free text"}) == {"said": "free text"}
    no_opts = {"kind": "question", "question": "q", "multi_select": False, "preselected": []}
    assert validate_answer("setup_choose", no_opts, {"said": "typed"}) == {"said": "typed"}


@pytest.mark.parametrize("multi", [False, True])
def test_multi_pick_needs_multi_select_with_server_rows(multi):
    params = {"kind": "plugins", "question": "q", "multi_select": multi,
              "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}
    if multi:
        assert validate_answer("setup_choose", params, {"picked": ["a", "b"]}) == {"picked": ["a", "b"]}
    else:
        with pytest.raises(ValueError):
            validate_answer("setup_choose", params, {"picked": ["a", "b"]})


@pytest.mark.parametrize("params,answer", [
    ({"kind": "theme", "multi_select": False}, {"picked": ["dark"]}),               # list on a single-select card
    ({"kind": "theme", "multi_select": False}, {"picked": ["dark", "light"]}),
    ({"kind": "connectors", "multi_select": True}, {"picked": ["gmail", "gmail"]}),  # duplicate pick
    ({"kind": "accent", "multi_select": False}, {"picked": "#8a2be2", "label": ["Violet"]}),
    ({"kind": "connectors", "multi_select": True}, {"picked": ["gmail", "slack"], "label": ["Gmail"]}),
    ({"kind": "connectors", "multi_select": True}, {"picked": ["gmail"], "label": "Gmail"}),
    ({"kind": "theme", "multi_select": False}, {"picked": 7}),                      # wrong type
    ({"kind": "theme", "multi_select": False}, {"picked": "x" * 70000}),            # over the answer bound
])
def test_app_owned_lists_keep_shape_and_size_bounds(params, answer):
    with pytest.raises(ValueError):
        validate_answer("setup_choose", {"question": "q", "preselected": [], **params}, answer)


def test_app_owned_multi_labels_accepted_one_for_one():
    params = {"kind": "connectors", "question": "q", "multi_select": True, "preselected": []}
    answer = {"picked": ["gmail", "slack"], "label": ["Gmail", "Slack"]}
    assert validate_answer("setup_choose", params, answer) == answer

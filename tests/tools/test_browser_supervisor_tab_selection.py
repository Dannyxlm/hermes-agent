"""Tab selection in a busy shared browser (t_9d780493).

The shared real-profile Chrome holds frozen background tabs, tabs blocked in ``beforeunload``
and busy renderers. Session-scoped CDP calls into them never answer. The supervisor used to
attach to the FIRST page target and wait 10 s on ``Page.enable``, so ``start()`` failed on every
browser call and ``browser_vault_fill`` fell through two 30 s CLI timeouts to "Could not determine
the current page origin". These tests drive ``_pick_page`` / ``_attach_initial_page`` /
``focus_page`` against a fake CDP endpoint whose hung tabs never reply. No real Chrome.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from tools import browser_supervisor as bs

PROBE = 0.2  # per-call probe bound used in these tests (production: PAGE_PROBE_TIMEOUT_S)
PASSWORD_PROBE = "!!document.querySelector('input[type=password]')"


@dataclass
class Tab:
    target_id: str
    url: str
    state: str = "hidden"  # "visible" | "hidden" | "hung"
    form: bool = False


class FakeBrowser:
    """Answers CDP like Chrome, except every session call into a ``hung`` tab never returns."""

    def __init__(self, tabs: list[Tab]):
        self.tabs = {t.target_id: t for t in tabs}
        self.order = [t.target_id for t in tabs]
        self.calls: list[tuple[str, dict, str | None]] = []

    def detached(self) -> set[str]:
        return {p["sessionId"] for m, p, _ in self.calls if m == "Target.detachFromTarget"}

    async def cdp(self, method, params=None, *, session_id=None, timeout=10.0):
        params = params or {}
        self.calls.append((method, params, session_id))

        async def answer():
            if method == "Target.getTargets":
                return {"result": {"targetInfos": [{"targetId": i, "type": "page", "url": self.tabs[i].url}
                                                   for i in self.order]}}
            if method == "Target.attachToTarget":
                return {"result": {"sessionId": "S-" + params["targetId"]}}
            if method == "Target.detachFromTarget":
                return {"result": {}}
            tab = self.tabs[session_id[2:]]
            if tab.state == "hung":
                await asyncio.Event().wait()
            if method == "Runtime.evaluate":
                accepted = tab.form if "password" in params["expression"] else True
                return {"result": {"result": {"type": "object",
                                              "value": ["visible" if tab.state == "visible" else "hidden", accepted, tab.url]}}}
            return {"result": {}}

        return await asyncio.wait_for(answer(), timeout)


def _supervisor(fake: FakeBrowser, task_id: str = "tab-select") -> bs.CDPSupervisor:
    sup = bs.CDPSupervisor(task_id=task_id, cdp_url="ws://127.0.0.1:1/devtools/browser/x")
    sup._cdp = fake.cdp  # type: ignore[method-assign]
    return sup


def _pick(sup, fake, *, accept=None, prefer_url=None):
    async def run():
        t0 = time.monotonic()
        res = await sup._pick_page([(i, fake.tabs[i].url) for i in fake.order], accept=accept,
                                   prefer_url=prefer_url, probe_timeout=PROBE)
        await asyncio.sleep(0.05)  # let fire-and-forget detaches run
        return res, time.monotonic() - t0
    return asyncio.run(run())


def test_hung_first_tab_is_skipped_within_one_probe_bound():
    fake = FakeBrowser([Tab("hung1", "https://app.kit.com/reports", "hung"),
                        Tab("hung2", "https://mail.google.com/", "hung"),
                        Tab("ok", "https://www.bestbuy.ca/signin", "hidden", form=True)])
    sup = _supervisor(fake)
    (url, sid), elapsed = _pick(sup, fake, accept=PASSWORD_PROBE)
    assert (url, sid) == ("https://www.bestbuy.ca/signin", "S-ok")
    assert elapsed < PROBE * 2 + 0.3  # parallel probes: a hung tab costs the bound once, not per tab
    assert {"S-hung1", "S-hung2"} <= fake.detached() and "S-ok" not in fake.detached()


def test_prefer_url_beats_a_visible_tab_and_does_not_wait_for_hung_tabs():
    fake = FakeBrowser([Tab("hung", "https://www.bestbuy.ca/signin?tid=Z", "hung"),
                        Tab("vis", "https://www.bestbuy.ca/signin?tid=A", "visible", form=True),
                        Tab("mine", "https://www.bestbuy.ca/signin?tid=B", "hidden", form=True)])
    sup = _supervisor(fake)
    (url, sid), elapsed = _pick(sup, fake, accept=PASSWORD_PROBE, prefer_url="https://www.bestbuy.ca/signin?tid=B")
    assert sid == "S-mine" and url.endswith("tid=B")
    assert elapsed < PROBE  # returned as soon as the navigated tab answered
    assert "S-vis" in fake.detached()


def test_prefer_url_matches_same_page_with_a_changed_query():
    fake = FakeBrowser([Tab("other", "https://shop.example/login", "visible", form=True),
                        Tab("mine", "https://shop.example/signin?step=2#pw", "hidden", form=True)])
    sup = _supervisor(fake)
    # The page navigated to, after a redirect changed its query, still ranks above "merely visible".
    (_url, sid), _ = _pick(sup, fake, accept=PASSWORD_PROBE, prefer_url="https://shop.example/signin?step=1")
    assert sid == "S-mine"
    assert bs._page_rank("https://shop.example/signin?step=2#pw", "hidden", "https://shop.example/signin?step=1") == 1
    assert bs._page_rank("https://shop.example/signin?x=1", "visible", None) == 2
    assert bs._page_rank("https://shop.example/signin?x=1", "hidden", None) == 3


def test_visible_tab_wins_without_a_hint_and_formless_tabs_never_qualify():
    fake = FakeBrowser([Tab("bg", "https://shop.example/login", "hidden", form=True),
                        Tab("nofrm", "https://shop.example/cart", "visible", form=False),
                        Tab("fg", "https://shop.example/login2", "visible", form=True)])
    sup = _supervisor(fake)
    (_url, sid), _ = _pick(sup, fake, accept=PASSWORD_PROBE)
    assert sid == "S-fg"
    assert {"S-bg", "S-nofrm"} <= fake.detached()


def test_no_responsive_tab_returns_none_within_the_bound():
    fake = FakeBrowser([Tab(f"h{i}", f"https://h{i}.example/", "hung") for i in range(40)])
    sup = _supervisor(fake)
    res, elapsed = _pick(sup, fake)
    assert res is None
    assert elapsed < PROBE * 2 + 0.5  # 40 hung tabs, still one bound


def test_probe_expression_tolerates_a_throwing_accept_check():
    js = bs._page_probe_js("document.querySelector('[')")
    assert "try" in js and "catch" in js and "!!(document.querySelector('['))" in js
    assert "ok = true;" in bs._page_probe_js(None)


def test_initial_attach_skips_a_hung_first_page_target():
    """The incident: the first page target (a frozen Kit tab) never answered Page.enable."""
    fake = FakeBrowser([Tab("kit", "https://app.kit.com/reports", "hung"),
                        Tab("bb", "https://www.bestbuy.ca/signin", "visible", form=True)])
    sup = _supervisor(fake)

    async def run():
        t0 = time.monotonic()
        with patch.object(bs, "PAGE_PROBE_TIMEOUT_S", PROBE):
            await sup._attach_initial_page()
        return time.monotonic() - t0

    elapsed = asyncio.run(run())
    assert sup._page_session_id == "S-bb"
    assert elapsed < 2.0
    enabled = {sid for m, _p, sid in fake.calls if m == "Page.enable"}
    assert enabled == {"S-bb"}


def test_initial_attach_prefers_the_tab_this_task_navigated():
    fake = FakeBrowser([Tab("a", "https://a.example/", "visible"), Tab("b", "https://b.example/page", "hidden")])
    sup = _supervisor(fake, task_id="hinted-task")
    bs.SUPERVISOR_REGISTRY.note_page("hinted-task", "https://b.example/page")
    try:
        asyncio.run(sup._attach_initial_page())
    finally:
        bs.SUPERVISOR_REGISTRY.stop("hinted-task")
    assert sup._page_session_id == "S-b"


def test_initial_attach_still_succeeds_when_every_tab_is_hung():
    """Browser-level work (focus_page) must stay possible: attach the first tab, enable it in the background."""
    fake = FakeBrowser([Tab("h1", "https://h1.example/", "hung"), Tab("h2", "https://h2.example/", "hung")])
    sup = _supervisor(fake)

    async def run():
        t0 = time.monotonic()
        with patch.object(bs, "PAGE_PROBE_TIMEOUT_S", PROBE):
            await sup._attach_initial_page()
        elapsed = time.monotonic() - t0
        for task in list(sup._background_tasks):
            task.cancel()
        return elapsed

    elapsed = asyncio.run(run())
    assert sup._page_session_id == "S-h1"
    assert elapsed < PROBE * 3 + 0.5


@pytest.fixture
def looped_supervisor():
    """A supervisor whose loop runs on a thread, as after ``start()`` (without a real socket)."""
    made: list[tuple[bs.CDPSupervisor, asyncio.AbstractEventLoop, threading.Thread]] = []

    def make(fake: FakeBrowser) -> bs.CDPSupervisor:
        sup = _supervisor(fake)
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        sup._loop = loop
        sup._active = True

        async def _no_bridge(_sid):
            return None
        sup._install_dialog_bridge = _no_bridge  # type: ignore[method-assign]
        made.append((sup, loop, thread))
        return sup

    yield make
    for _sup, loop, thread in made:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_focus_page_picks_the_navigated_login_tab_among_hung_same_origin_tabs(looped_supervisor):
    fake = FakeBrowser([Tab("h1", "https://www.bestbuy.ca/account", "hung"),
                        Tab("other", "https://app.kajabi.com/admin", "visible", form=True),
                        Tab("vis", "https://www.bestbuy.ca/signin?tid=A", "visible", form=True),
                        Tab("mine", "https://www.bestbuy.ca/signin?tid=B", "hidden", form=True)])
    sup = looped_supervisor(fake)
    t0 = time.monotonic()
    res = sup.focus_page("https://www.bestbuy.ca", accept=PASSWORD_PROBE, probe_timeout=PROBE,
                         prefer_url="https://www.bestbuy.ca/signin?tid=B")
    assert res == {"ok": True, "url": "https://www.bestbuy.ca/signin?tid=B"}
    assert time.monotonic() - t0 < 1.0
    assert sup._page_session_id == "S-mine"
    attached = {p["targetId"] for m, p, _ in fake.calls if m == "Target.attachToTarget"}
    assert "other" not in attached  # other origins are never touched


def test_focus_page_failure_is_bounded_and_keeps_the_previous_session(looped_supervisor):
    fake = FakeBrowser([Tab(f"h{i}", f"https://www.bestbuy.ca/p{i}", "hung") for i in range(30)])
    sup = looped_supervisor(fake)
    sup._page_session_id = "S-previous"
    t0 = time.monotonic()
    res = sup.focus_page("https://www.bestbuy.ca", accept=PASSWORD_PROBE, probe_timeout=PROBE)
    assert res["ok"] is False and "no responsive page" in res["error"] and "30 candidate" in res["error"]
    assert time.monotonic() - t0 < PROBE * 2 + 1.0
    assert sup._page_session_id == "S-previous"


def test_schedule_cancels_the_coroutine_when_the_caller_times_out():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    reached_end = threading.Event()
    cancelled = threading.Event()

    async def slow():
        try:
            await asyncio.sleep(0.5)
            reached_end.set()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    try:
        with pytest.raises(TimeoutError):
            bs._schedule(slow(), loop, timeout=0.05)
        assert cancelled.wait(1.0)
        time.sleep(0.6)
        assert not reached_end.is_set()  # a late result can no longer retarget the page session
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


def test_page_hints_record_http_pages_only_and_clear_on_stop():
    reg = bs._SupervisorRegistry()
    reg.note_page("t1", "about:blank")
    assert reg.page_hint("t1") is None
    reg.note_page("t1", "https://www.bestbuy.ca/signin")
    assert reg.page_hint("t1") == "https://www.bestbuy.ca/signin"
    reg.stop("t1")
    assert reg.page_hint("t1") is None


def test_browser_navigate_hint_is_recorded_for_task_and_session_key():
    from tools import browser_tool

    try:
        browser_tool._note_supervisor_page("nav-task", "nav-task::rp", "https://www.bestbuy.ca/signin?tid=B")
        assert bs.SUPERVISOR_REGISTRY.page_hint("nav-task") == "https://www.bestbuy.ca/signin?tid=B"
        assert bs.SUPERVISOR_REGISTRY.page_hint("nav-task::rp") == "https://www.bestbuy.ca/signin?tid=B"
    finally:
        bs.SUPERVISOR_REGISTRY.stop("nav-task")
        bs.SUPERVISOR_REGISTRY.stop("nav-task::rp")


def test_vault_focus_passes_the_navigated_tab_as_prefer_url():
    from tools import browser_vault_tool as vt

    seen = {}

    class Sup:
        def focus_page(self, origin, *, accept=None, prefer_url=None, **_kw):
            seen.update(origin=origin, accept=accept, prefer_url=prefer_url)
            return {"ok": True, "url": prefer_url}

    bs.SUPERVISOR_REGISTRY.note_page("vault-task", "https://www.bestbuy.ca/signin?tid=B")
    try:
        with patch.object(vt, "_ensure_supervisor", return_value=Sup()):
            assert vt._focus_bound_origin("vault-task", "https://www.bestbuy.ca", "login") == "https://www.bestbuy.ca"
    finally:
        bs.SUPERVISOR_REGISTRY.stop("vault-task")
    assert seen == {"origin": "https://www.bestbuy.ca", "accept": vt._TAB_PROBES["login"],
                    "prefer_url": "https://www.bestbuy.ca/signin?tid=B"}


def test_vault_supervisor_uses_the_session_endpoint_without_a_daemon_round_trip():
    """``get cdp-url`` timed out at 30 s in the busy shared Chrome; a real-profile session already knows it."""
    from tools import browser_tool, browser_vault_tool as vt

    ws = "ws://127.0.0.1:9225/devtools/browser/abc"
    started = {}

    def get_or_start(**kw):
        started.update(kw)
        return "SUP"

    with patch.dict(browser_tool._active_sessions, {"ep-task": {"cdp_url": ws}}), \
            patch.object(browser_tool, "_last_session_key", lambda t: t), \
            patch("tools.browser_tool_session._run_browser_command",
                  side_effect=AssertionError("daemon must not be asked")), \
            patch.object(bs.SUPERVISOR_REGISTRY, "get_or_start", side_effect=get_or_start):
        assert vt._ensure_supervisor("ep-task") == "SUP"
    assert started["task_id"] == "ep-task" and started["cdp_url"] == ws


def test_vault_supervisor_still_asks_the_daemon_for_a_local_session():
    from tools import browser_tool, browser_vault_tool as vt

    with patch.dict(browser_tool._active_sessions, {"local-task": {"session_name": "h_1"}}), \
            patch.object(browser_tool, "_last_session_key", lambda t: t), \
            patch("tools.browser_tool_session._run_browser_command",
                  return_value={"success": False, "error": "no daemon"}) as run, \
            patch.object(bs.SUPERVISOR_REGISTRY, "get_or_start", side_effect=AssertionError("no endpoint")):
        assert vt._ensure_supervisor("local-task") is None
    run.assert_called_once_with("local-task", "get", ["cdp-url"])

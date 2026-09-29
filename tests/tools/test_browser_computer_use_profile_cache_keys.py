"""Browser-exec and computer_use backend caches are namespaced by the served profile.

Regression for #110032: both process-global caches were keyed by the caller's session/task id
alone, so under gateway.multiplex_profiles two profiles using the same id (``"default"``, a shared
named browser session, a matching DISPLAY) resolved to the FIRST profile's browser / cua-driver.
Outside a served-profile scope every key stays byte-identical to the legacy shape."""

from __future__ import annotations

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def two_homes(tmp_path):
    a = tmp_path / "profiles" / "a"
    b = tmp_path / "profiles" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    return a, b


def _under(home):
    return set_hermes_home_override(str(home))


def test_browser_exec_cache_key_differs_per_served_profile_and_is_legacy_when_unscoped(two_homes):
    import tools.browser_use_cli as bu

    a, b = two_homes
    assert bu._backend_cache_key("t1", "work") == "bu-named-work"
    assert bu._backend_cache_key(None) == "browser-exec-default"
    tok = _under(a)
    try:
        key_a = bu._backend_cache_key("t1", "work")
    finally:
        reset_hermes_home_override(tok)
    tok = _under(b)
    try:
        key_b = bu._backend_cache_key("t1", "work")
        key_b_again = bu._backend_cache_key("t1", "work")
    finally:
        reset_hermes_home_override(tok)
    assert key_a != key_b and key_b == key_b_again
    assert key_a.startswith("bu-named-work") and key_b.startswith("bu-named-work")


def test_browser_exec_harness_daemon_namespace_is_per_served_profile(two_homes):
    """The harness daemon (``bu-<BU_NAME>`` pid/socket) is reused BEFORE the resolved CDP is read, so a
    shared runtime dir handed a served profile the launch profile's live ``bu-default`` daemon — its
    browser, proxy and real-profile Chrome — no matter what backend the served profile resolved."""
    import tools.browser_use_cli as bu

    a, b = two_homes
    unscoped = {"TMPDIR": "/tmp"}
    bu._isolate_served_profile_daemons(unscoped)
    assert unscoped == {"TMPDIR": "/tmp"}  # launch profile keeps the legacy per-user runtime dir

    dirs = []
    for home in (a, b, a):
        env = {"TMPDIR": "/tmp"}
        tok = _under(home)
        try:
            bu._isolate_served_profile_daemons(env)
        finally:
            reset_hermes_home_override(tok)
        assert env["BH_RUNTIME_DIR"] == env["BH_TMP_DIR"] and env["BH_RUNTIME_DIR"].startswith("/tmp/bh-")
        assert env["BH_RUNTIME_DIR_SHARED"] == "1"  # named sessions still get distinct filenames
        assert len(env["BH_RUNTIME_DIR"]) + len("/bu-" + "x" * 64 + ".sock") < 108
        dirs.append(env["BH_RUNTIME_DIR"])
    assert dirs[0] != dirs[1] and dirs[0] == dirs[2]

    explicit = {"TMPDIR": "/tmp", "BH_RUNTIME_DIR": "/srv/bh"}
    tok = _under(a)
    try:
        bu._isolate_served_profile_daemons(explicit)
    finally:
        reset_hermes_home_override(tok)
    assert explicit["BH_RUNTIME_DIR"] == "/srv/bh" and "BH_TMP_DIR" not in explicit  # operator override wins


def test_computer_use_backend_not_shared_across_profiles_and_release_finds_it(two_homes, monkeypatch):
    import tools.computer_use.tool as cu

    a, b = two_homes
    created = []

    class _Backend:
        def __init__(self):
            self.stopped = False
            created.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(cu, "_new_backend", lambda mode: _Backend())
    monkeypatch.setattr(cu, "_cua_permission_mode", lambda sid: "standard")
    with cu._backend_lock:
        cu._backends.clear(), cu._backend_call_locks.clear(), cu._backend_permission_modes.clear()

    tok = _under(a)
    try:
        backend_a = cu._get_backend("shared")
        assert cu._get_backend("shared") is backend_a
    finally:
        reset_hermes_home_override(tok)
    tok = _under(b)
    try:
        backend_b = cu._get_backend("shared")
        assert backend_b is not backend_a
        assert cu.release_computer_use_session("shared") is True  # releases B's, not A's
        assert backend_b.stopped and not backend_a.stopped
    finally:
        reset_hermes_home_override(tok)
    tok = _under(a)
    try:
        assert cu._get_backend("shared") is backend_a  # A's entry survived B's release
    finally:
        reset_hermes_home_override(tok)
        with cu._backend_lock:
            cu._backends.clear(), cu._backend_call_locks.clear(), cu._backend_permission_modes.clear()

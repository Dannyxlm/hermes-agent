"""Cold roster polls coalesce without retaining unknown/fresh-store answers."""
from concurrent.futures import Future, ThreadPoolExecutor
import os
import threading

import pytest

from tui_gateway import profile_roster_cache as cache


@pytest.fixture(autouse=True)
def clean_cache():
    cache.invalidate()
    yield
    cache.invalidate()


def test_overlapping_roster_reads_share_a_flight(tmp_path, monkeypatch):
    (tmp_path / "state.db").write_bytes(b"store")
    monkeypatch.setattr(cache, "mtime_is_racy", lambda _: False)
    started, release, joined = threading.Event(), threading.Event(), threading.Event()

    class ObservedFuture(Future):
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout=5)

    monkeypatch.setattr(cache, "Future", ObservedFuture, raising=False)
    reads = []

    def compute():
        reads.append(1)
        started.set()
        assert release.wait(5)
        return {"canonical_session": {"id": "root", "working": None}}

    def read():
        return cache.cached_reader_session_fields(tmp_path, ("owner", False), compute)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(read)
        assert started.wait(5)
        second = pool.submit(read)
        try:
            assert joined.wait(5), "overlapping poll did not join the cold read"
        finally:
            release.set()
        before, after = first.result(5), second.result(5)
    assert reads == [1]
    assert before == after == {"canonical_session": {"id": "root", "working": None}}
    after["canonical_session"]["id"] = "caller mutation"
    assert before["canonical_session"]["id"] == "root"
    assert read()["canonical_session"]["id"] == "root"
    assert reads == [1]


def test_replaced_database_with_same_size_and_mtime_invalidates(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "mtime_is_racy", lambda _: False)
    target = tmp_path / "state.db"
    target.write_bytes(b"old")
    assert cache.cached_session_fields(tmp_path, lambda: {"n": 1}) == {"n": 1}
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"new")
    stat = target.stat()
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(target)
    assert cache.cached_session_fields(tmp_path, lambda: {"n": 2}) == {"n": 2}


def test_invalidation_during_compute_cannot_repopulate_cache(tmp_path, monkeypatch):
    (tmp_path / "state.db").write_bytes(b"store")
    monkeypatch.setattr(cache, "mtime_is_racy", lambda _: False)

    def compute():
        cache.invalidate(tmp_path)
        return {"n": 1}

    assert cache.cached_session_fields(tmp_path, compute) == {"n": 1}
    assert cache.cached_session_fields(tmp_path, lambda: {"n": 2}) == {"n": 2}


def test_returned_nested_values_do_not_mutate_a_cached_roster(tmp_path, monkeypatch):
    (tmp_path / "state.db").write_bytes(b"store")
    monkeypatch.setattr(cache, "mtime_is_racy", lambda _: False)
    first = cache.cached_session_fields(tmp_path, lambda: {"last_session": {"id": "root"}})
    first["last_session"]["id"] = "changed"
    second = cache.cached_session_fields(tmp_path, lambda: {"last_session": None})
    assert second == {"last_session": {"id": "root"}}


def test_a_fresh_store_write_does_not_join_a_prewrite_flight(tmp_path, monkeypatch):
    (tmp_path / "state.db").write_bytes(b"store")
    monkeypatch.setattr(cache, "mtime_is_racy", lambda _: True)
    started, release = threading.Event(), threading.Event()

    def old_read():
        started.set()
        assert release.wait(5)
        return {"n": 1}

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cache.cached_session_fields, tmp_path, old_read)
        assert started.wait(5)
        second = pool.submit(cache.cached_session_fields, tmp_path, lambda: {"n": 2})
        try:
            assert second.result(5) == {"n": 2}
        finally:
            release.set()
        assert first.result(5) == {"n": 1}

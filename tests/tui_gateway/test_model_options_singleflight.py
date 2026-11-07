"""Concurrent model picker opens share discovery, not session-local selection."""
from concurrent.futures import ThreadPoolExecutor
import threading

from tui_gateway import server

pytest_plugins = ["tests.tui_gateway.test_model_options_cache"]


def test_cold_picker_discovery_is_singleflight(options, monkeypatch):
    observed, release = threading.Event(), threading.Event()
    threads = set()
    lock = threading.Lock()

    class ObservedCache(dict):
        def get(self, key, default=None):
            with lock:
                threads.add(threading.get_ident())
                if len(threads) == 3:
                    observed.set()
            return super().get(key, default)

    monkeypatch.setattr(server, "_MODEL_OPTIONS_CACHE", ObservedCache())
    builds = []

    def build(ctx, **kwargs):
        builds.append(1)
        assert observed.wait(5)
        assert release.wait(5)
        return {"providers": [{"slug": "fixture"}]}

    monkeypatch.setattr("hermes_cli.inventory.build_model_options_payload", build)
    with ThreadPoolExecutor(3) as pool:
        futures = [pool.submit(options) for _ in range(3)]
        try:
            assert observed.wait(5)
        finally:
            release.set()
        assert [f.result(5) for f in futures] == ["fixture"] * 3
    assert builds == [1]

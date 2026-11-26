from __future__ import annotations

import os
from pathlib import Path

import pytest

import persistence_assembly
import utils


def test_foreign_provider_cache_reuses_and_invalidates_on_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    active_home = tmp_path / "active"
    foreign_home = tmp_path / "foreign"
    active_home.mkdir()
    foreign_home.mkdir()
    cfg = foreign_home / "config.yaml"
    cfg.write_text("persistence:\n  provider: sqlite\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(active_home))
    with persistence_assembly._FOREIGN_PROVIDER_LOCK:
        persistence_assembly._FOREIGN_PROVIDER_CACHE.clear()

    parse_calls = 0
    real_load = utils.fast_safe_load

    def counting_load(fh):
        nonlocal parse_calls
        parse_calls += 1
        return real_load(fh)

    monkeypatch.setattr(utils, "fast_safe_load", counting_load)

    assert persistence_assembly._provider_for_home(foreign_home) == "sqlite"
    assert persistence_assembly._provider_for_home(foreign_home) == "sqlite"
    assert parse_calls == 1

    # Rewrite file with a new size/mtime -> must invalidate and re-parse.
    cfg.write_text("# changed\npersistence:\n  provider: sqlite\n", encoding="utf-8")
    st = cfg.stat()
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert persistence_assembly._provider_for_home(foreign_home) == "sqlite"
    assert parse_calls == 2

    # Unknown provider must raise and must not be cached.
    cfg.write_text("persistence:\n  provider: bogus_db\n", encoding="utf-8")
    st = cfg.stat()
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))
    with pytest.raises(ValueError, match="unknown persistence provider"):
        persistence_assembly._provider_for_home(foreign_home)
    assert parse_calls == 3

    # Missing config.yaml returns default sqlite without parsing.
    cfg.unlink()
    assert persistence_assembly._provider_for_home(foreign_home) == "sqlite"
    assert parse_calls == 3

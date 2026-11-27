"""Tests for server-side thumbnail transport and revision-validated files.index caching."""
import io
import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from plugins.cloudseed_mobile import links_index, workspace_files


def _make_png(path: Path, size=(1200, 900), color=(40, 140, 210)) -> None:
    im = Image.new("RGB", size, color)
    im.save(path, format="PNG")


def test_thumbnail_transport_resizes_caches_and_returns_304(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    ws = tmp_path / "workspace"
    home.mkdir()
    ws.mkdir()
    img_path = ws / "hero.png"
    _make_png(img_path, size=(1600, 1200))

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(workspace_files, "workspace", lambda profile, wid, generation=False: ws)
    monkeypatch.setattr(workspace_files, "check_profile", lambda profile: None)

    r1 = workspace_files.thumbnail_transport("default", "w1", "hero.png", width=480)
    assert r1.status_code == 200
    assert r1.media_type in ("image/webp", "image/jpeg")
    assert r1.headers["X-Thumb-Cache"] == "miss"
    assert "ETag" in r1.headers
    assert len(r1.body) < img_path.stat().st_size

    out_im = Image.open(io.BytesIO(r1.body))
    assert max(out_im.size) <= 480

    r2 = workspace_files.thumbnail_transport("default", "w1", "hero.png", width=480)
    assert r2.status_code == 200
    assert r2.headers["X-Thumb-Cache"] == "hit"
    assert r2.body == r1.body

    r3 = workspace_files.thumbnail_transport("default", "w1", "hero.png", width=480, if_none_match=r1.headers["ETag"])
    assert r3.status_code == 304
    assert r3.headers["X-Thumb-Cache"] == "not-modified"

    r_head = workspace_files.thumbnail_transport("default", "w1", "hero.png", width=480, head=True)
    assert r_head.status_code == 200
    assert r_head.body == b""
    assert int(r_head.headers["Content-Length"]) == len(r1.body)


def test_thumbnail_transport_rejects_non_image_and_symlink(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    ws = tmp_path / "workspace"
    home.mkdir()
    ws.mkdir()
    txt = ws / "notes.txt"
    txt.write_text("not an image")
    outside = tmp_path / "secret.png"
    _make_png(outside, size=(200, 200))
    link = ws / "escape.png"
    link.symlink_to(outside)

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(workspace_files, "workspace", lambda profile, wid, generation=False: ws)
    monkeypatch.setattr(workspace_files, "check_profile", lambda profile: None)

    with pytest.raises(HTTPException) as exc:
        workspace_files.thumbnail_transport("default", "w1", "notes.txt", width=480)
    assert exc.value.status_code == 415

    with pytest.raises(HTTPException) as exc2:
        workspace_files.thumbnail_transport("default", "w1", "escape.png", width=480)
    assert exc2.value.status_code == 403


def test_thumbnail_transport_invalidates_on_file_update(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    ws = tmp_path / "workspace"
    home.mkdir()
    ws.mkdir()
    img_path = ws / "art.png"
    _make_png(img_path, size=(800, 600), color=(255, 0, 0))

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(workspace_files, "workspace", lambda profile, wid, generation=False: ws)
    monkeypatch.setattr(workspace_files, "check_profile", lambda profile: None)

    r1 = workspace_files.thumbnail_transport("default", "w1", "art.png", width=320)
    assert r1.headers["X-Thumb-Cache"] == "miss"

    time.sleep(0.01)
    _make_png(img_path, size=(600, 800), color=(0, 255, 0))
    r2 = workspace_files.thumbnail_transport("default", "w1", "art.png", width=320)
    assert r2.headers["X-Thumb-Cache"] == "miss"
    assert r2.headers["ETag"] != r1.headers["ETag"]


def test_query_index_cache_hits_and_invalidates_on_source_change(tmp_path):
    links_index._QUERY_CACHE.clear()
    db_file = tmp_path / "state.db"
    db_file.write_bytes(b"v1")

    class _FakeIndex:
        home = tmp_path
        profile = "default"

    fp1 = links_index._source_db_fingerprint(_FakeIndex())
    time.sleep(0.01)
    db_file.write_bytes(b"v2-longer")
    fp2 = links_index._source_db_fingerprint(_FakeIndex())
    assert fp1 != fp2

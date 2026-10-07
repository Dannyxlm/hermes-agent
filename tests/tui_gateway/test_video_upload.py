"""Video sharing (Hermex R8-09): upload slots, the chunk routes, ``video.attach`` / ``video.evidence``
and the evidence sidecar the agent reads. Real ffmpeg runs only when the managed binary is present."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from tui_gateway import media_uploads, video_evidence

MP4_HEAD = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


# ── upload registry ──────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def reg(tmp_path):
    clock = Clock()
    registry = media_uploads.UploadRegistry(tmp_path / "uploads", clock=clock, max_bytes=1024 * 1024,
                                            idle_ttl=60, max_open=2)
    registry.clock = clock
    return registry


def begin(reg, size=64, session_key="s1", filename="clip.mp4"):
    return reg.begin(session_key=session_key, profile_home="/p", filename=filename, size=size)


def test_begin_rejects_non_video_oversize_and_bad_size(reg):
    with pytest.raises(media_uploads.UploadRejected):
        begin(reg, filename="notes.txt")
    with pytest.raises(media_uploads.UploadTooLarge) as too_big:
        begin(reg, size=2 * 1024 * 1024)
    assert too_big.value.status == 413 and too_big.value.detail["max_bytes"] == 1024 * 1024
    for bad in (0, -1, "64", 1.5, None):
        with pytest.raises(media_uploads.UploadRejected):
            begin(reg, size=bad)
    with pytest.raises(media_uploads.UploadRejected):
        reg.begin(session_key="", profile_home="/p", filename="clip.mp4", size=10)


def test_chunks_resume_from_server_offset_and_claim_once(reg):
    payload = MP4_HEAD + bytes(range(40))
    slot = begin(reg, size=len(payload))
    assert len(slot.upload_id) >= 32 and media_uploads.valid_upload_id(slot.upload_id)
    assert reg.append(slot.upload_id, 0, [payload[:10]])["offset"] == 10
    # A retried chunk at a stale offset is refused with the truth, never double-written.
    with pytest.raises(media_uploads.UploadOffsetMismatch) as stale:
        reg.append(slot.upload_id, 0, [payload[:10]])
    assert stale.value.detail["offset"] == 10
    assert reg.status(slot.upload_id) == {"upload_id": slot.upload_id, "offset": 10,
                                          "size": len(payload), "complete": False}
    with pytest.raises(media_uploads.UploadConflict):
        reg.claim(slot.upload_id, session_key="s1")  # incomplete
    reg.append(slot.upload_id, 10, [payload[10:20], payload[20:]])
    # Another chat cannot claim it, and the answer does not reveal it exists.
    with pytest.raises(media_uploads.UploadNotFound):
        reg.claim(slot.upload_id, session_key="other")
    claimed = reg.claim(slot.upload_id, session_key="s1")
    assert claimed.part_path.read_bytes() == payload
    with pytest.raises(media_uploads.UploadNotFound):
        reg.claim(slot.upload_id, session_key="s1")
    with pytest.raises(media_uploads.UploadNotFound):
        reg.status(slot.upload_id)


def test_more_bytes_than_declared_is_refused_and_truncated(reg):
    slot = begin(reg, size=8)
    with pytest.raises(media_uploads.UploadTooLarge):
        reg.append(slot.upload_id, 0, [b"12345", b"6789"])
    # The accepted prefix stays; the overflow chunk is not written.
    assert reg.status(slot.upload_id)["offset"] == 5
    assert slot.part_path.stat().st_size == 5


def test_claim_rejects_bytes_that_are_not_a_video(reg):
    slot = begin(reg, size=16)
    reg.append(slot.upload_id, 0, [b"not a video at all"[:16]])
    with pytest.raises(media_uploads.UploadRejected):
        reg.claim(slot.upload_id, session_key="s1")
    assert not slot.part_path.exists()


def test_idle_slots_expire_and_open_slots_are_capped(reg):
    first = begin(reg)
    begin(reg)
    with pytest.raises(media_uploads.UploadConflict):
        begin(reg)
    begin(reg, session_key="s2")  # the cap is per chat
    reg.clock.now += 61
    with pytest.raises(media_uploads.UploadNotFound):
        reg.status(first.upload_id)
    assert not first.part_path.exists()
    begin(reg)  # expired slots no longer count


def test_cancel_removes_part_and_unknown_ids_are_not_found(reg):
    slot = begin(reg)
    assert reg.cancel(slot.upload_id) is True
    assert not slot.part_path.exists()
    assert reg.cancel(slot.upload_id) is False
    for bad in ("", "../etc/passwd", "x" * 10, "a" * 65):
        with pytest.raises(media_uploads.UploadNotFound):
            reg.get(bad)


def test_sniff_video_container():
    assert media_uploads.sniff_video_container(MP4_HEAD) == "mp4"
    assert media_uploads.sniff_video_container(b"\x00\x00\x00\x08wide\x00\x00") == "mp4"
    assert media_uploads.sniff_video_container(b"\x1a\x45\xdf\xa3rest") == "webm"
    assert media_uploads.sniff_video_container(b"\x89PNG\r\n\x1a\n0000") is None


# ── REST chunk route ─────────────────────────────────────────────────────────

class FakeRequest:
    def __init__(self, body: bytes, *, pieces=3, content_length=True):
        self._body = body
        self._pieces = pieces
        self.headers = {"content-length": str(len(body))} if content_length else {}

    async def stream(self):
        step = max(1, len(self._body) // self._pieces)
        for i in range(0, len(self._body), step):
            yield self._body[i:i + step]
        yield b""


@pytest.fixture
def route_reg(reg):
    media_uploads.set_registry_for_tests(reg)
    yield reg
    media_uploads.set_registry_for_tests(None)


def test_put_route_streams_chunks_and_reports_offsets(route_reg):
    from hermes_cli.web_routers import media_uploads as routes
    payload = MP4_HEAD + b"x" * 100
    slot = begin(route_reg, size=len(payload))
    first = asyncio.run(routes.put_upload_chunk(slot.upload_id, FakeRequest(payload[:50]), offset=0))
    assert first == {"upload_id": slot.upload_id, "offset": 50, "size": len(payload), "complete": False}
    stale = asyncio.run(routes.put_upload_chunk(slot.upload_id, FakeRequest(payload[:50]), offset=0))
    assert stale.status_code == 409 and json.loads(stale.body)["offset"] == 50
    done = asyncio.run(routes.put_upload_chunk(slot.upload_id, FakeRequest(payload[50:], content_length=False),
                                               offset=50))
    assert done["complete"] is True
    assert asyncio.run(routes.get_upload_status(slot.upload_id))["offset"] == len(payload)
    missing = asyncio.run(routes.get_upload_status("A" * 32))
    assert missing.status_code == 404


def test_put_route_rejects_oversized_chunk_before_reading(route_reg, monkeypatch):
    from hermes_cli.web_routers import media_uploads as routes
    monkeypatch.setattr(media_uploads, "MAX_CHUNK_BYTES", 10)
    slot = begin(route_reg, size=64)
    response = asyncio.run(routes.put_upload_chunk(slot.upload_id, FakeRequest(b"y" * 20), offset=0))
    assert response.status_code == 413
    assert route_reg.status(slot.upload_id)["offset"] == 0


def test_routes_are_registered_on_the_dashboard_app():
    from hermes_cli.web_server import app
    paths = {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", ()) or ()))) for r in app.routes}
    assert ("/api/media/uploads/{upload_id}", ("PUT",)) in paths
    assert ("/api/media/uploads/{upload_id}", ("GET",)) in paths


# ── RPCs ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def rpc(tmp_path, reg, monkeypatch):
    from tui_gateway import server
    media_uploads.set_registry_for_tests(reg)
    started: list[Path] = []
    video_evidence.set_jobs_for_tests(video_evidence.EvidenceJobs(runner=started.append))
    session = {
        "agent": None, "agent_ready": threading.Event(), "agent_error": None, "attached_images": [],
        "cwd": str(tmp_path / "workspace"), "history": [], "history_lock": threading.RLock(),
        "history_version": 0, "image_counter": 0, "profile_home": str(tmp_path / "profile"),
        "running": False, "session_key": "stored-1", "transport": None,
    }
    (tmp_path / "workspace").mkdir()
    other = {**session, "session_key": "stored-2", "history_lock": threading.RLock(), "attached_images": []}
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, s: None)
    monkeypatch.setitem(server._sessions, "live-1", session)
    monkeypatch.setitem(server._sessions, "live-2", other)
    facts = {"duration_s": 42.5, "width": 1170, "height": 2532, "codec": "hevc", "has_audio": True, "rotation": 0}
    monkeypatch.setattr(video_evidence, "resolve_ffmpeg", lambda: ("/bin/ffmpeg", "/bin/ffprobe"))
    monkeypatch.setattr(video_evidence, "probe", lambda video, ffprobe: dict(facts))

    def call(method, **params):
        return server._methods[method](1, params)

    yield call, session, started, facts
    media_uploads.set_registry_for_tests(None)
    video_evidence.set_jobs_for_tests(None)


def _upload(call, reg, payload, sid="live-1", filename="Screen Recording.mov"):
    begun = call("media.upload.begin", session_id=sid, filename=filename, size=len(payload), mime="video/quicktime")
    assert "result" in begun, begun
    upload_id = begun["result"]["upload_id"]
    assert begun["result"]["upload_path"] == f"api/media/uploads/{upload_id}"
    reg.append(upload_id, 0, [payload])
    return upload_id


def test_video_attach_stores_in_session_attachments_and_starts_evidence(rpc, reg, tmp_path):
    call, session, started, _ = rpc
    payload = MP4_HEAD + b"v" * 200
    upload_id = _upload(call, reg, payload)
    result = call("video.attach", session_id="live-1", upload_id=upload_id)["result"]
    stored = Path(result["path"])
    assert stored.parent == (tmp_path / "profile" / "attachments").resolve()
    assert stored.name == "Screen Recording.mov" and stored.read_bytes() == payload
    assert result["ref_text"].startswith("@file:") and "Screen Recording.mov" in result["ref_text"]
    assert result["duration_s"] == 42.5 and result["has_audio"] is True
    assert result["evidence"]["status"] == "pending"
    assert started == [stored]
    assert video_evidence.read_manifest(stored)["display_name"] == "Screen Recording.mov"
    # The slot is one-use.
    again = call("video.attach", session_id="live-1", upload_id=upload_id)
    assert again["error"]["code"] == 4016
    # A second upload with the same name gets a distinct file.
    second = call("video.attach", session_id="live-1", upload_id=_upload(call, reg, payload))["result"]
    assert Path(second["path"]).name == "Screen Recording-2.mov"


def test_video_attach_refuses_another_chats_upload_and_incomplete_bytes(rpc, reg):
    call, *_ = rpc
    upload_id = _upload(call, reg, MP4_HEAD + b"v" * 10, sid="live-2")
    assert call("video.attach", session_id="live-1", upload_id=upload_id)["error"]["code"] == 4016
    begun = call("media.upload.begin", session_id="live-1", filename="a.mp4", size=100)["result"]
    reg.append(begun["upload_id"], 0, [MP4_HEAD])
    assert call("video.attach", session_id="live-1", upload_id=begun["upload_id"])["error"]["code"] == 4020


def test_video_too_long_is_refused_with_an_honest_message(rpc, reg):
    call, session, started, facts = rpc
    facts["duration_s"] = media_uploads.VIDEO_MAX_SECONDS + 1
    error = call("video.attach", session_id="live-1", upload_id=_upload(call, reg, MP4_HEAD + b"v"))["error"]
    assert error["code"] == 4018 and "Trim it" in error["message"]
    assert started == []
    assert not any((Path(session["profile_home"]) / "attachments").glob("*.mov"))


def test_begin_answers_caps_before_bytes_move(rpc):
    call, *_ = rpc
    error = call("media.upload.begin", session_id="live-1", filename="huge.mp4", size=10 ** 12)["error"]
    assert error["code"] == 4018
    assert call("media.upload.begin", session_id="live-1", filename="doc.pdf", size=10)["error"]["code"] == 4017


def test_video_evidence_queues_contact_sheet_once_and_scopes_paths(rpc, reg, tmp_path):
    call, session, *_ = rpc
    result = call("video.attach", session_id="live-1", upload_id=_upload(call, reg, MP4_HEAD + b"v"))["result"]
    video = Path(result["path"])
    manifest = video_evidence.read_manifest(video)
    evidence = video_evidence.evidence_dir(video)
    (evidence / "contact-sheet.jpg").write_bytes(b"\xff\xd8\xff")
    manifest.update(status="ready", contact_sheet="contact-sheet.jpg", poster="contact-sheet.jpg",
                    frames=[{"file": "frames/frame-01.jpg", "t": 1.0}])
    (evidence / "manifest.json").write_text(json.dumps(manifest))
    first = call("video.evidence", session_id="live-1", path=str(video), queue_contact_sheet=True)["result"]
    assert first["status"] == "ready" and first["queued_image_paths"] == [str(evidence / "contact-sheet.jpg")]
    assert session["attached_images"] == [str(evidence / "contact-sheet.jpg")]
    second = call("video.evidence", session_id="live-1", path=str(video), queue_contact_sheet=True)["result"]
    assert second["queued_image_paths"] == []
    # Paths outside this session's attachments are unknown, including traversal.
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(MP4_HEAD)
    for raw in (str(outside), str(video.parent / ".." / ".." / "elsewhere.mp4")):
        assert call("video.evidence", session_id="live-1", path=raw)["error"]["code"] == 4016


def test_capabilities_advertise_video_upload():
    from tui_gateway import server
    result = server._methods["mobile.capabilities"](1, {})["result"]
    assert "video_upload" in result["features"] and result["feature_versions"]["video_upload"] == 1
    assert result["video_upload"]["max_bytes"] == media_uploads.VIDEO_MAX_BYTES
    assert ".mov" in result["video_upload"]["extensions"]
    assert {"video.attach", "video.evidence"} <= server._LONG_HANDLERS
    assert "media.upload.begin" not in server._LONG_HANDLERS


def test_contracts_declare_media_methods():
    from tui_gateway.contracts import METHODS
    from tui_gateway.contracts.registry import validate_params
    for name in ("media.upload.begin", "media.upload.cancel", "video.attach", "video.evidence"):
        assert name in METHODS
        _, error = validate_params(METHODS[name], {"session_id": "s", "nope": 1})
        assert error is not None


# ── evidence + agent note ────────────────────────────────────────────────────

def _fake_manifest(video: Path, **overrides) -> dict:
    video.write_bytes(MP4_HEAD)
    facts = {"duration_s": 12.0, "width": 390, "height": 844, "codec": "h264", "has_audio": True, "rotation": 0}
    manifest = video_evidence.write_initial_manifest(video, facts, display_name=video.name)
    manifest.update(overrides)
    (video_evidence.evidence_dir(video) / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_agent_note_mirrors_inbound_video_note_and_lists_evidence(tmp_path):
    video = tmp_path / "jitter.mp4"
    evidence = video_evidence.evidence_dir(video)
    manifest = _fake_manifest(video, status="ready", contact_sheet="contact-sheet.jpg",
                              contact_sheet_layout={"columns": 4, "rows": 1},
                              frames=[{"file": f"frames/frame-0{i}.jpg", "t": float(i)} for i in (1, 2, 3)],
                              transcript={"status": "done", "file": "transcript.txt", "chars": 5})
    (evidence / "transcript.txt").write_text("hello there\n")
    note = video_evidence.agent_note(video, video_evidence.read_manifest(video))
    assert note.startswith("[The user sent a video attachment: 'jitter.mp4'. It is saved at: ")
    assert "instead of asking the user to describe it" in note
    assert str(evidence / "contact-sheet.jpg") in note and "0:01.0, 0:02.0, 0:03.0" in note
    assert "Audio transcript" in note and "hello there" in note
    assert manifest["video"]["bytes"] == len(MP4_HEAD)


@pytest.mark.parametrize("status,expected", [
    ("no_audio", "no audio track"), ("silent", "no speech detected"),
    ("unavailable", "no speech-to-text provider"), ("failed", "transcription failed"),
    ("pending", "still in progress")])
def test_agent_note_is_honest_about_audio(tmp_path, status, expected):
    video = tmp_path / "clip.mp4"
    _fake_manifest(video, transcript={"status": status, "file": None, "chars": 0})
    assert expected in video_evidence.agent_note(video, video_evidence.read_manifest(video))


def test_file_reference_to_uploaded_video_expands_to_the_evidence_note(tmp_path):
    from agent.context_references import _expand_path_reference, parse_context_references
    video = tmp_path / "clip.mov"
    _fake_manifest(video, status="processing")
    ref = parse_context_references(f"look @file:{video.name}")[0]
    warning, block = _expand_path_reference(ref, tmp_path)
    assert warning is None
    assert "The user sent a video attachment" in block and "still being prepared" in block
    # A plain video without a sidecar keeps the ordinary binary handling.
    plain = tmp_path / "plain.mp4"
    plain.write_bytes(MP4_HEAD)
    _, plain_block = _expand_path_reference(parse_context_references("@file:plain.mp4")[0], tmp_path)
    assert "The user sent a video attachment" not in plain_block and "binary file" in plain_block


def test_frame_times_are_bucket_centres():
    assert video_evidence.frame_times(12.0, 4) == [1.5, 4.5, 7.5, 10.5]
    assert video_evidence.frame_times(0.0) == [0.0]
    assert video_evidence.format_timestamp(75.25) == "1:15.2"


def test_job_failure_marks_manifest_failed(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    _fake_manifest(video)

    def boom(*_args, **_kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(video_evidence, "build_evidence", boom)
    jobs = video_evidence.EvidenceJobs()
    assert jobs.start(video)
    jobs.wait(video, 5)
    manifest = video_evidence.read_manifest(video)
    assert manifest["status"] == "failed" and "evidence job crashed" in manifest["errors"]
    assert manifest["transcript"]["status"] == "failed"
    assert not jobs.is_running(video)


_FFMPEG, _FFPROBE = video_evidence.resolve_ffmpeg()


@pytest.mark.skipif(not (_FFMPEG and _FFPROBE), reason="ffmpeg not installed")
def test_real_ffmpeg_builds_frames_sheet_poster_and_detects_silence(tmp_path):
    video = tmp_path / "synth.mp4"
    subprocess.run([_FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=320x640:rate=15", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                    "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)],
                   check=True, timeout=60)
    facts = video_evidence.probe(video, _FFPROBE)
    assert facts["has_audio"] and facts["width"] == 320 and 2.5 < facts["duration_s"] < 3.5
    video_evidence.write_initial_manifest(video, facts, display_name="synth.mp4")
    called = []
    manifest = video_evidence.build_evidence(video, ffmpeg=_FFMPEG, frame_count=4,
                                             transcriber=lambda p: called.append(p) or {"success": True})
    evidence = video_evidence.evidence_dir(video)
    assert manifest["status"] == "ready" and len(manifest["frames"]) == 4
    assert (evidence / "contact-sheet.jpg").stat().st_size > 0 and (evidence / "poster.jpg").is_file()
    # A silent track never reaches the STT provider.
    assert manifest["transcript"]["status"] == "silent" and called == []
    assert not list(evidence.glob("audio_*")), "temporary audio must be cleaned up"
    shutil.rmtree(evidence)

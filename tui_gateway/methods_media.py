"""Large-media attach RPCs (Hermex video sharing): ``media.upload.begin``, ``media.upload.cancel``,
``video.attach`` and ``video.evidence``.

The bytes never travel over this socket: ``media.upload.begin`` mints a one-use slot for an admitted
session, the client streams to ``PUT /api/media/uploads/{upload_id}`` (``hermes_cli/web_routers/
media_uploads.py``), and ``video.attach`` claims the slot for the SAME session, stores the video in
that session's attachments (like ``file.attach``) and starts the evidence job
(``tui_gateway/video_evidence.py``). ``video.evidence`` reports progress and, once asked, queues the
contact sheet as an image for the next turn so the agent sees the frames.

Bodies are rebound onto server.py's globals (method_ctx.bind_module) and reference them bare.
"""

from __future__ import annotations

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

# Moving a few hundred MB, ffprobe and the evidence read must never stall the socket reader.
MEDIA_LONG_HANDLERS = frozenset({"video.attach", "video.evidence"})

_MEDIA_ERROR_CODES = {404: 4016, 409: 4020, 413: 4018, 422: 4017}


def _media_err(rid, exc):
    return _err(rid, _MEDIA_ERROR_CODES.get(getattr(exc, "status", 400), 4017), str(exc))


def _media_session_key(session) -> str:
    return str(session.get("session_key") or "")


def _store_uploaded_video(session, part_path, filename: str):
    """Move the claimed ``.part`` into ``<session attachments>/<name>`` (exclusive create, ``-2``
    suffixes on collision, same allocation rule as ``file.attach``)."""
    import os
    import shutil
    root = _session_home_dir(session, "attachments")
    root.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(filename).stem or "video", Path(filename).suffix
    target, counter = root / filename, 2
    while True:
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            target = root / f"{stem}-{counter}{suffix}"
            counter += 1
            continue
        os.close(fd)
        break
    try:
        try:
            os.replace(part_path, target)
        except OSError:
            shutil.copyfile(part_path, target)
            Path(part_path).unlink(missing_ok=True)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target.resolve()


def _session_video_path(session, raw: str):
    """``raw`` as a video inside this session's attachments root, else None (lexical containment on
    resolved paths: a symlink can't widen what this session may read)."""
    if not raw:
        return None
    from tui_gateway import media_uploads
    root = _session_home_dir(session, "attachments").resolve()
    try:
        candidate = Path(raw).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    if media_uploads.video_mime_for(candidate.name) is None or not candidate.is_file():
        return None
    return candidate


@method("media.upload.begin")
def _(rid, params: dict) -> dict:
    """Mint an upload slot for a video this session will attach. Caps are checked here, before a
    single byte moves."""
    session, err = _sess_building(params, rid)
    if err:
        return err
    from tui_gateway import media_uploads
    filename = _sanitize_attachment_name(str(params.get("filename") or ""))
    try:
        slot = media_uploads.registry().begin(
            session_key=_media_session_key(session), profile_home=str(session.get("profile_home") or ""),
            filename=filename, size=params.get("size"), mime=params.get("mime"))
    except media_uploads.UploadError as exc:
        return _media_err(rid, exc)
    return _ok(rid, {
        "upload_id": slot.upload_id, "upload_path": f"api/media/uploads/{slot.upload_id}",
        "offset": 0, "size": slot.expected_bytes, "chunk_bytes": media_uploads.CHUNK_BYTES,
        "max_bytes": media_uploads.VIDEO_MAX_BYTES, "max_seconds": media_uploads.VIDEO_MAX_SECONDS,
        "expires_in": int(media_uploads.SLOT_IDLE_TTL_SECONDS)})


@method("media.upload.cancel")
def _(rid, params: dict) -> dict:
    session, err = _sess_building(params, rid)
    if err:
        return err
    from tui_gateway import media_uploads
    upload_id = str(params.get("upload_id") or "")
    reg = media_uploads.registry()
    try:
        slot = reg.get(upload_id)
    except media_uploads.UploadError:
        return _ok(rid, {"cancelled": False})
    if slot.session_key != _media_session_key(session):
        return _ok(rid, {"cancelled": False})
    return _ok(rid, {"cancelled": reg.cancel(upload_id)})


@method("video.attach")
def _(rid, params: dict) -> dict:
    """Claim a completed upload for this session, store it, probe it, start the evidence job and hand
    back the ``@file:`` reference for the message text."""
    session, err = _sess_building(params, rid)
    if err:
        return err
    from tui_gateway import media_uploads, video_evidence
    try:
        slot = media_uploads.registry().claim(
            str(params.get("upload_id") or ""), session_key=_media_session_key(session))
    except media_uploads.UploadError as exc:
        return _media_err(rid, exc)
    filename = _sanitize_attachment_name(str(params.get("filename") or "") or slot.filename)
    if media_uploads.video_mime_for(filename) is None:
        filename = Path(filename).stem + Path(slot.filename).suffix
    try:
        target = _store_uploaded_video(session, slot.part_path, filename)
    except Exception as exc:
        Path(slot.part_path).unlink(missing_ok=True)
        return _err(rid, 5028, f"could not store the video: {exc}")
    _ffmpeg, ffprobe = video_evidence.resolve_ffmpeg()
    if ffprobe is None:
        facts = {"duration_s": 0.0, "width": 0, "height": 0, "codec": "", "has_audio": False, "rotation": 0}
    else:
        try:
            facts = video_evidence.probe(target, ffprobe)
        except (video_evidence.EvidenceError, OSError) as exc:
            target.unlink(missing_ok=True)
            return _err(rid, 4017, str(exc))
        except Exception as exc:  # timeout or a broken binary: keep nothing half-attached
            target.unlink(missing_ok=True)
            return _err(rid, 5028, f"could not read the video: {type(exc).__name__}")
    if facts["duration_s"] > media_uploads.VIDEO_MAX_SECONDS:
        target.unlink(missing_ok=True)
        return _err(rid, 4018, (
            f"video is {video_evidence.format_timestamp(facts['duration_s'])} long; the cap is "
            f"{media_uploads.VIDEO_MAX_SECONDS // 60} minutes. Trim it and try again."))
    manifest = video_evidence.write_initial_manifest(target, facts, display_name=filename)
    profile_home = session.get("profile_home")
    video_evidence.jobs().start(target, scope_factory=lambda: _session_profile_runtime_scope(
        {"profile_home": profile_home or None}))
    ref_path = _attachment_ref_path(session, target)
    return _ok(rid, {
        "attached": True, "name": target.name, "path": str(target), "ref_path": ref_path,
        "ref_text": f"@file:{_format_ref_value(ref_path)}", "bytes": target.stat().st_size,
        "duration_s": facts["duration_s"], "width": facts["width"], "height": facts["height"],
        "has_audio": facts["has_audio"], "evidence": video_evidence.evidence_summary(target, manifest)})


@method("video.evidence")
def _(rid, params: dict) -> dict:
    """Evidence progress for a video this session attached. ``queue_contact_sheet`` queues the sheet
    for the next turn exactly once per session (so the agent sees the frames inline)."""
    session, err = _sess_building(params, rid)
    if err:
        return err
    from tui_gateway import video_evidence
    video = _session_video_path(session, str(params.get("path") or "").strip())
    if video is None:
        return _err(rid, 4016, "unknown video for this chat")
    manifest = video_evidence.read_manifest(video)
    summary = video_evidence.evidence_summary(video, manifest)
    queued: list[str] = []
    sheet = summary.get("contact_sheet_path")
    if params.get("queue_contact_sheet") is True and sheet and Path(sheet).is_file():
        with session["history_lock"]:
            done = session.setdefault("_queued_video_evidence", set())
            if sheet not in done:
                done.add(sheet)
                session.setdefault("attached_images", []).append(sheet)
                queued.append(sheet)
    return _ok(rid, {**summary, "path": str(video), "running": video_evidence.jobs().is_running(video),
                     "queued_image_paths": queued})


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
    server._LONG_HANDLERS = server._LONG_HANDLERS | MEDIA_LONG_HANDLERS

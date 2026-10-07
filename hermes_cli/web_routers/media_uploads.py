"""Resumable video upload chunks: ``PUT/GET/DELETE /api/media/uploads/{upload_id}``.

A slot is minted only by the session-admitted WS RPC ``media.upload.begin``
(``tui_gateway/methods_media.py``) and claimed one time by ``video.attach`` on the same session; these
routes only move bytes into it. They sit behind the dashboard's normal ``/api/`` auth (native bearer or
session cookie); the slot id is a 192-bit secret the owner's live session received, and it never
reveals or reaches another chat's files. Bodies stream to disk in bounded chunks, never base64/JSON.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

router = APIRouter()


def _error(exc) -> JSONResponse:
    content = {"detail": str(exc)}
    content.update(getattr(exc, "detail", {}) or {})
    return JSONResponse(status_code=getattr(exc, "status", 400), content=content)


@router.put("/api/media/uploads/{upload_id}")
async def put_upload_chunk(upload_id: str, request: Request, offset: int = 0):
    """Append this request's body at ``offset`` (must equal the bytes already received; a 409 carries
    the server's ``offset`` so the client resumes from there)."""
    from tui_gateway import media_uploads
    reg = media_uploads.registry()
    try:
        slot = reg.get(upload_id)
    except media_uploads.UploadError as exc:
        return _error(exc)
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > media_uploads.MAX_CHUNK_BYTES:
                return _error(media_uploads.UploadTooLarge(
                    "chunk too large", max_chunk_bytes=media_uploads.MAX_CHUNK_BYTES))
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
    if offset != slot.received:
        return _error(media_uploads.UploadOffsetMismatch(
            f"offset {offset} does not match the {slot.received} bytes already received",
            offset=slot.received))
    pieces: list[bytes] = []
    total = 0
    async for piece in request.stream():
        total += len(piece)
        if total > media_uploads.MAX_CHUNK_BYTES:
            return _error(media_uploads.UploadTooLarge(
                "chunk too large", max_chunk_bytes=media_uploads.MAX_CHUNK_BYTES))
        if piece:
            pieces.append(piece)
    try:
        return await run_in_threadpool(reg.append, upload_id, offset, pieces)
    except media_uploads.UploadError as exc:
        return _error(exc)


@router.get("/api/media/uploads/{upload_id}")
async def get_upload_status(upload_id: str):
    from tui_gateway import media_uploads
    try:
        return media_uploads.registry().status(upload_id)
    except media_uploads.UploadError as exc:
        return _error(exc)


@router.delete("/api/media/uploads/{upload_id}")
async def delete_upload(upload_id: str):
    from tui_gateway import media_uploads
    return {"cancelled": media_uploads.registry().cancel(upload_id)}

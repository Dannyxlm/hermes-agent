"""Large-media (video) attach RPCs (``methods_media.py``). The bytes move over
``PUT /api/media/uploads/{upload_id}``, never this socket."""

from __future__ import annotations

from .base import Params, Result
from .common import SessionParams
from .registry import method


class VideoUploadLimits(Result):
    """Advertised in ``mobile.capabilities.video_upload`` (feature ``video_upload`` v1)."""

    max_bytes: int
    max_seconds: int
    chunk_bytes: int
    extensions: list[str]


class MediaUploadBeginParams(SessionParams):
    filename: str
    size: int
    mime: str | None = None


class MediaUploadBeginResult(Result):
    upload_id: str
    upload_path: str  # relative to the dashboard origin, e.g. ``api/media/uploads/<id>``
    offset: int
    size: int
    chunk_bytes: int
    max_bytes: int
    max_seconds: int
    expires_in: int


method("media.upload.begin", params=MediaUploadBeginParams, result=MediaUploadBeginResult,
       doc="Mint a one-use upload slot for a video this session will attach (caps checked here).")


class MediaUploadCancelParams(SessionParams):
    upload_id: str


class MediaUploadCancelResult(Result):
    cancelled: bool


method("media.upload.cancel", params=MediaUploadCancelParams, result=MediaUploadCancelResult,
       doc="Drop an unfinished upload slot owned by this session.")


class VideoEvidenceSummary(Result):
    status: str  # missing | pending | processing | ready | failed
    poster_path: str | None = None
    contact_sheet_path: str | None = None
    frame_count: int
    transcript_status: str  # pending | done | no_audio | silent | unavailable | failed
    errors: list[str]


class VideoAttachParams(SessionParams):
    upload_id: str
    filename: str | None = None


class VideoAttachResult(Result):
    attached: bool
    name: str
    path: str
    ref_path: str
    ref_text: str
    bytes: int
    duration_s: float
    width: int
    height: int
    has_audio: bool
    evidence: VideoEvidenceSummary


method("video.attach", params=VideoAttachParams, result=VideoAttachResult,
       doc="Claim a completed upload, store it in the session's attachments and start the evidence job.")


class VideoEvidenceParams(SessionParams):
    path: str
    queue_contact_sheet: bool = False


class VideoEvidenceResult(VideoEvidenceSummary):
    path: str
    running: bool
    queued_image_paths: list[str]


method("video.evidence", params=VideoEvidenceParams, result=VideoEvidenceResult,
       doc="Evidence progress for an attached video; optionally queue its contact sheet for the next turn.")

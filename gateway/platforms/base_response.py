"""Post-handler response extraction; guest replies remain inert text."""
from __future__ import annotations

import logging

from gateway.platforms.event import MessageEvent

logger = logging.getLogger("gateway.platforms.base")


async def extract_response_content(self, response: str, event: MessageEvent, session_key: str,
                                    *, is_ephemeral_response: bool):
    """Split a handler response into deliverable text + attachments. Order matters: MEDIA tags →
    image URLs → residual directives → bare local paths (skipped for ephemeral notices so config
    paths stay text; unknown-extension MEDIA tags survive for the bare-path detector). History
    dedup is bare-path only, off-loop, fail-open. An emptied non-empty response is recovered."""
    from gateway.platforms.base import _ExtractedResponse, _strip_media_directives
    from gateway.guest_channels import is_guest_source
    runner = getattr(self, "gateway_runner", None)
    if runner is not None and is_guest_source(runner, event.source):
        # Guest output is inert text: never open an emitted local path or fetch an image URL.
        return _ExtractedResponse(response, [], [], [], False, response)
    # Captured before extract_media strips it: images then go via send_document (no recompression).
    force_document = "[[as_document]]" in response
    pre_extract = response
    # The handler's routed profile scope is gone by now; Docker MEDIA translation and the
    # bare-path validator infer the sandbox from the ACTIVE profile (#109024).
    with self._media_delivery_scope(event.source):
        media_files, response = self.extract_media(response)
        media_files = self.filter_media_delivery_paths(media_files, session_key=session_key)
        images, text_content = self.extract_images(response)
        # Strip any remaining internal directives from message body (fixes #1561). _strip_media_directives
        # shares MEDIA_TAG_CLEANUP_RE, so a MEDIA: tag with an unknown extension is intentionally left in
        # the body for extract_local_files below to pick up rather than silently dropped (#34517).
        text_content = _strip_media_directives(text_content).strip()
        if images:
            logger.info("[%s] extract_images found %d image(s) in response (%d chars)", self.name, len(images), len(response))
        local_files = []
        if not is_ephemeral_response:
            local_files, text_content = self.extract_local_files(text_content)
            local_files = self.filter_local_delivery_paths(local_files, session_key=session_key)
    history = (await self._bounded_history_media_paths_for_session(session_key)
               if local_files else None)
    if history:
        suppressed = [p for p in local_files if p in history]
        if suppressed:
            logger.info("[%s] Suppressing %d bare local file path(s) already delivered in "
                        "this session: %s", self.name, len(suppressed), suppressed)
            local_files = [p for p in local_files if p not in history]
    if local_files:
        logger.info("[%s] extract_local_files found %d file(s) in response", self.name, len(local_files))
    # A2 (#29346): extraction can reduce a non-empty response to empty text with no attachment, and the
    # `if text_content` guard below then drops it silently. Recover on every platform (#33842 was
    # Discord-only); the guard avoids duplicating an attachment.
    if not (text_content or images or local_files or media_files):
        _recovered = _strip_media_directives(response).strip()
        if _recovered:
            logger.warning("[%s] response_delivery_recovered: extract pipeline "
                           "reduced a non-empty response (%d chars) to empty with "
                           "no attachment; delivering recovered original to %s", self.name,
                           len(pre_extract), event.source.chat_id)
            text_content = _recovered
    return _ExtractedResponse(
        text_content=text_content, images=images, media_files=media_files,
        local_files=local_files, force_document_attachments=force_document, pre_extract=pre_extract)

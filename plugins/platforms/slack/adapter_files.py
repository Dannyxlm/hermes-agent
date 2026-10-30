"""Slack file downloading with shared SSRF/token protections and optional streaming limits."""
import asyncio
import logging

from gateway.platforms.base_log_safety import safe_url_for_log
from gateway.platforms.base import ssrf_redirect_guard

logger = logging.getLogger("plugins.platforms.slack.adapter")


async def read_response(client, url, bot_token, max_bytes):
    headers = {"Authorization": f"Bearer {bot_token}"}
    if max_bytes is None:
        response = await client.get(url, headers=headers)
        return response, response.content
    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code != 200:
            return response, b""
        length = response.headers.get("content-length")
        if length and int(length) > max_bytes:
            raise ValueError("Guest image exceeds byte limit")
        if response.headers.get("content-type", "").split(";", 1)[0] not in {
            "image/png", "image/jpeg", "image/gif", "image/webp",
        }:
            raise ValueError("Guest upload is not a supported image")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > max_bytes:
                raise ValueError("Guest image exceeds byte limit")
            data.extend(chunk)
        return response, bytes(data)


async def _download_slack_file_bytes(
    self, url: str, team_id: str = "", *, html_label: str = "file bytes", max_bytes=None) -> bytes:
    """Download a Slack file with the bot token (3 attempts on 429/5xx/timeout). URL must pass
    ``is_safe_url`` AND the Slack-CDN allowlist (token exfiltration); redirects are
    re-validated; an HTML body (sign-in page) is rejected so bogus bytes are never cached."""
    import httpx
    from tools.url_safety import (
        create_ssrf_safe_async_client, is_safe_url, redirect_target_from_response,
    )
    if not is_safe_url(url):
        raise ValueError(
            f"Blocked unsafe Slack file URL (SSRF protection): {safe_url_for_log(url)}")
    if not self._is_slack_cdn_url(url):
        raise ValueError(
            "Blocked non-Slack-CDN file URL (token-exfiltration protection): "
            f"{safe_url_for_log(url)}")
    bot_token = self._resolve_download_token(url, team_id)
    async with create_ssrf_safe_async_client(
        timeout=30.0, follow_redirects=False, event_hooks={"response": [ssrf_redirect_guard]}
    ) as client:
        for attempt in range(3):
            try:
                download_url = url
                redirect_count = 0
                while True:
                    # httpx drops Authorization across origins. Enterprise Grid
                    # needs it on files-origin, but never send it off Slack's CDN.
                    response, content = await read_response(client, download_url, bot_token, max_bytes)
                    if response.status_code not in (301, 302, 303, 307, 308):
                        break
                    target = redirect_target_from_response(response)
                    if not target:
                        break  # raise_for_status reports the malformed redirect.
                    if not is_safe_url(target):
                        raise ValueError(
                            "Blocked unsafe Slack file redirect (SSRF protection): "
                            f"{safe_url_for_log(target)}")
                    if not self._is_slack_cdn_url(target):
                        raise ValueError(
                            "Blocked non-Slack-CDN file redirect (token-exfiltration protection): "
                            f"{safe_url_for_log(target)}")
                    if redirect_count == 3:
                        raise ValueError("Too many Slack file redirects (maximum 3)")
                    download_url = target
                    redirect_count += 1
                response.raise_for_status()
                ct = response.headers.get("content-type", "")
                if "text/html" in ct:
                    raise ValueError(
                        f"Slack returned HTML instead of {html_label} (content-type: {ct}); "
                        "check bot token scopes and file permissions, or a cross-origin "
                        "redirect dropped the token (Enterprise Grid files-origin)")
                return content
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 429:
                    raise
                if attempt < 2:
                    logger.debug(
                        "Slack file download retry %d/2 for %s: %s", attempt + 1, url[:80], exc)
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise

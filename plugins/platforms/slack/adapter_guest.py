"""Guest intake limits run before media parsing or model work; owner paths are unchanged."""
import math
import time

import httpx

from gateway.guest_channels import guest_channel_policy, is_guest_source


def policy_for_event(adapter, user_id, channel_id, team_id, is_dm=False):
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None:
        return None
    source = adapter.build_source(chat_id=channel_id, user_id=user_id, scope_id=team_id,
                                  chat_type="dm" if is_dm else "group")
    return guest_channel_policy(runner, source) if is_guest_source(runner, source) else None


def positive_limit(policy, key, default):
    value = policy.get(key, default) if isinstance(policy, dict) else default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return default
    return value


def admit_guest_event(adapter, user_id, channel_id, team_id, is_dm=False):
    """Per receiving bot/workspace/channel/user cooldown, shared across threads and native slash.

    Silent drops cannot create a reply storm. Full tables fail closed until an entry expires,
    rather than evicting a still-cooled user and letting them buy another turn.
    """
    policy = policy_for_event(adapter, user_id, channel_id, team_id, is_dm)
    if policy is None:
        return True
    now = time.monotonic()
    deadlines = getattr(adapter, "_guest_turn_deadlines", {})
    adapter._guest_turn_deadlines = deadlines
    key = (team_id, channel_id, user_id)
    if now < deadlines.get(key, 0):
        return False
    for expired in [k for k, deadline in deadlines.items() if deadline <= now]:
        del deadlines[expired]
    if len(deadlines) >= 10000:
        return False
    deadlines[key] = now + positive_limit(policy, "min_seconds_between_turns", 20)
    return True


async def cache_guest_image(adapter, file, team_id, policy):
    """Only known-size raster images; enforce actual streamed bytes as well as Slack metadata."""
    from gateway.platforms.base import cache_image_from_bytes_async
    mime = file.get("mimetype")
    extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
    size = file.get("size")
    limit = int(positive_limit(policy, "max_image_bytes", 5 * 1024 * 1024))
    url = file.get("url_private_download") or file.get("url_private")
    if mime not in extensions or type(size) is not int or not 0 < size <= limit or not url:
        return None
    data = await adapter._download_slack_file_bytes(url, team_id=team_id, max_bytes=limit)
    signatures = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if len(data) > limit or not signatures[mime]:
        return None
    return await cache_image_from_bytes_async(data, extensions[mime]), mime, len(data)


async def collect_guest_media(adapter, event, channel_id, team_id, text, policy):
    """No documents, audio/STT, videos or text inlining, including on a hydrated thread root."""
    files = list(event.get("files") or [])
    thread_ts = event.get("thread_ts")
    if thread_ts and thread_ts != event.get("ts") and not adapter._has_active_session_for_thread(
        channel_id=channel_id, thread_ts=thread_ts, user_id=event.get("user"), team_id=team_id,
        chat_type="dm" if event.get("channel_type") in {"im", "mpim"} else "group",
    ):
        cached = adapter._thread_context_cache.get(adapter._thread_cache_key(channel_id, thread_ts, team_id))
        root = adapter._thread_root_message(cached.messages, thread_ts) if cached else None
        files = list(root.get("files") or []) + files if root else files
    urls, types = [], []
    # Bound aggregate disk/vision work too: the channel cap is a per-turn byte budget.
    remaining = int(positive_limit(policy, "max_image_bytes", 5 * 1024 * 1024))
    note = ("[attachment not read: only PNG/JPEG/GIF/WebP screenshots "
            f"up to {remaining} bytes per turn are accepted here]")
    for file in files:
        image = None
        if isinstance(file, dict) and remaining > 0:
            try:
                image = await cache_guest_image(adapter, file, team_id, {"max_image_bytes": remaining})
            except (OSError, ValueError, httpx.HTTPError):
                image = None
        if image:
            urls.append(image[0])
            types.append(image[1])
            remaining -= image[2]
        else:
            text = f"{text}\n{note}".strip()
    return urls, types, [False] * len(urls), text

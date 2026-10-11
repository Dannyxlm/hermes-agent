"""Server-side email verification codes for ``browser_vault_enter_code``.

When a site emails the second-factor code, Hermes can read it from an inbox it already has access to
(``vault.email_codes``), pick the code out locally and type it into the page, so the agent can finish a
login without a human relaying the code and without the model ever seeing the code or the email.

Guard rails, each covered by tests:
- the inbox is chosen by the saved login's identifier, never searched broadly;
- only mail from the site's own domain, received within ``lookback_seconds``, counts; the NEWEST such
  email must hold exactly one code, otherwise the caller falls back to asking the user;
- reads are read-only by construction (Gmail ``messages.list/get``; IMAP ``EXAMINE`` + ``BODY.PEEK``);
- bodies and codes never reach logs or tool results; a code is used at most once per process.
"""

from __future__ import annotations

import base64
import email
import email.policy
import html as _html
import imaplib
import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# Words that mark a number as a sign-in code rather than an order, ticket or phone number.
_KEYWORD = re.compile(r"\b(?:code|codes|passcode|verification|verify|one[- ]time|otp|pin|security code|2fa|mfa)\b", re.IGNORECASE)
_NEAR = 80  # a candidate counts only when a keyword sits within this many characters of it
# 4-8 digits standing alone: not part of a date (10/11/2026), time (13:46), decimal, price, id or word.
_DIGITS = re.compile(r"(?<![\w/:.,$#%-])(\d{4,8})(?![\w/:%-]|[.,]\d)")
# The "482 913" / "482-913" presentation many senders use.
_GROUPED = re.compile(r"(?<![\w/:.,$#%-])(\d{3})[ -](\d{3})(?![\w/:%-]|[.,]\d)")
# Letters-and-digits codes only count when the email labels them as the code directly.
_LABELLED_ALNUM = re.compile(r"\b(?:code|passcode|otp|pin)\b\s*(?:is\b)?\s*[:\-]?\s*([A-Za-z0-9]{4,8})\b", re.IGNORECASE)
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_YEAR = re.compile(r"(?:19|20)\d\d")


def _plain_text(body: str) -> str:
    """Visible text of a plain or HTML body: no styles, scripts, tags, attribute values or URLs."""
    text = re.sub(r"(?is)<(style|script|head)\b.*?</\1\s*>", " ", body)
    text = re.sub(r"(?s)<[^>]*>", " ", text)
    text = _html.unescape(text)
    text = _URL.sub(" ", text)
    return re.sub(r"[ \t\r\f\v\u00a0]+", " ", text)


def _near_keyword(text: str, start: int, end: int) -> bool:
    return bool(_KEYWORD.search(text, max(0, start - _NEAR), min(len(text), end + _NEAR)))


def extract_code(subject: str, body: str) -> Optional[str]:
    """The one verification code in this email, or None when there is none or more than one."""
    text = f"{_plain_text(subject or '')}\n{_plain_text(body or '')}"
    found: set[str] = set()
    for m in _GROUPED.finditer(text):
        if _near_keyword(text, m.start(), m.end()):
            found.add(m.group(1) + m.group(2))
    for m in _DIGITS.finditer(text):
        if _near_keyword(text, m.start(), m.end()):
            found.add(m.group(1))
    for m in _LABELLED_ALNUM.finditer(text):
        token = m.group(1)
        if re.search(r"\d", token) and re.search(r"[A-Za-z]", token):
            found.add(token)
    # A copyright year in the footer sits near "code" too; it only counts when it is the sole candidate.
    if len(found) > 1:
        found = {c for c in found if not _YEAR.fullmatch(c)} or found
    return found.pop() if len(found) == 1 else None



@dataclass
class EmailMessage:
    """One received email, held only in this process. ``body`` may be plain text or HTML."""

    sender: str  # the From header, display name included
    subject: str
    received_at: float  # epoch seconds, as the mail server recorded it
    body: str
    key: str = ""  # lane-scoped message id, so a code already entered is never entered twice


# Second-level labels under a two-letter country TLD (acme.co.uk, acme.com.au): the registrable
# domain is then three labels. A heuristic, not the public-suffix list; a miss only means "ask the user".
_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "ac", "edu", "ne", "or"}


def _registrable_domain(host: str) -> str:
    labels = [x for x in host.lower().strip(".").split(".") if x]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _sender_address(sender: str) -> str:
    return (parseaddr(sender or "")[1] or "").lower()


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def sender_matches_site(sender: str, subject: str, site_host: str, site_label: str = "") -> bool:
    """Did the site send this? Decided by the sender's DOMAIN only: it is the site's registrable domain
    or a subdomain of it, or the site's name (first label of that domain, or the saved login's label)
    begins the sender's registrable domain (facebookmail.com, shopifyemail.com; never notacme.test). Display names and
    subjects are free to spoof, so they never count: an impostor's code typed into the real page could
    authorise the impostor's device. ``subject`` is accepted for callers' convenience and ignored."""
    site = _registrable_domain(site_host)
    if not site:
        return False
    domain = _sender_address(sender).rpartition("@")[2]
    if not domain:
        return False
    if domain == site or domain.endswith("." + site):
        return True
    sender_name = _squash(_registrable_domain(domain).rsplit(".", 1)[0])
    names = {_squash(site.split(".", 1)[0]), _squash(site_label)}
    return any(len(n) >= 4 and sender_name.startswith(n) for n in names)


def pick_code(messages: Iterable[EmailMessage], site_host: str, *, since: float, site_label: str = "") -> Optional[str]:
    """The code in the NEWEST message from the site received at or after ``since``. When that message
    holds no single code, returns None rather than reaching back to an older one."""
    fresh = [m for m in messages if m.received_at >= since and sender_matches_site(m.sender, m.subject, site_host, site_label)]
    if not fresh:
        return None
    newest = max(fresh, key=lambda m: m.received_at)
    return extract_code(newest.subject, newest.body)


# ---------------------------------------------------------------------------
# Inbox lanes. Read-only by construction: Gmail through messages.list/get only (get never changes
# labels, so nothing is marked read), IMAP through EXAMINE + BODY.PEEK only. Bodies stay in this
# process; nothing here logs message content.
# ---------------------------------------------------------------------------

Matcher = Callable[[str, str], bool]  # (sender, subject) -> is this from the site?
_MAX_SCAN = 15  # newest messages inspected per poll


def _preferred_text(parts: list[tuple[str, str]]) -> str:
    """Plain text when the email has it (cleanest for codes), else the HTML."""
    plain = "\n".join(t for mime, t in parts if mime == "text/plain" and t.strip())
    return plain or "\n".join(t for mime, t in parts if mime == "text/html")


def _run_cli(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        # stderr is never echoed: a failing mail CLI can print message fragments.
        raise RuntimeError(f"mail CLI exited {proc.returncode}")
    return proc.stdout or ""


class GmailLane:
    """A Gmail inbox through a gws-compatible CLI (``<command> gmail users messages list|get``)."""

    def __init__(self, command: list[str], run: Callable[[list[str]], str] = _run_cli):
        self.command = list(command)
        self._run = run
        self._meta: dict[str, tuple[str, str, float]] = {}

    def _call(self, method: str, params: dict) -> dict:
        out = self._run([*self.command, "gmail", "users", "messages", method,
                         "--params", json.dumps(params), "--format", "json"])
        return json.loads(out[out.index("{"):]) if "{" in out else {}

    def recent(self, since: float, matches: Matcher) -> list[EmailMessage]:
        listing = self._call("list", {"userId": "me", "q": f"after:{int(since)}", "maxResults": _MAX_SCAN})
        found = []
        for ref in listing.get("messages") or []:
            mid = str(ref.get("id") or "")
            if not mid:
                continue
            if mid not in self._meta:
                meta = self._call("get", {"userId": "me", "id": mid, "format": "metadata", "metadataHeaders": ["From", "Subject"]})
                headers = {h.get("name", "").lower(): h.get("value", "") for h in (meta.get("payload") or {}).get("headers") or []}
                self._meta[mid] = (headers.get("from", ""), headers.get("subject", ""), int(meta.get("internalDate") or 0) / 1000)
            sender, subject, received = self._meta[mid]
            if received < since or not matches(sender, subject):
                continue
            full = self._call("get", {"userId": "me", "id": mid, "format": "full"})
            found.append(EmailMessage(sender, subject, received, _preferred_text(_gmail_parts(full.get("payload") or {})), key=f"gmail:{mid}"))
        return found

    def close(self) -> None:
        return None


def _gmail_parts(payload: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    mime = str(payload.get("mimeType") or "")
    data = (payload.get("body") or {}).get("data")
    if data and mime.startswith("text/") and not payload.get("filename"):
        out.append((mime, base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")))
    for part in payload.get("parts") or []:
        out.extend(_gmail_parts(part))
    return out


class ImapLane:
    """An IMAP inbox, examined read-only. The password is resolved at connect time and dropped after login."""

    def __init__(self, host: str, port: int, username: str, password: Callable[[], str], folder: str = "INBOX",
                 connect: Optional[Callable[[str, int], object]] = None):
        self.host, self.port, self.username, self.folder = host, int(port or 993), username, folder or "INBOX"
        self._password = password
        self._connect = connect or (lambda h, p: imaplib.IMAP4_SSL(h, p, timeout=30))
        self._conn = None
        self._meta: dict[str, tuple[str, str, float]] = {}

    def _client(self):
        if self._conn is None:
            conn = self._connect(self.host, self.port)
            secret = self._password()
            try:
                conn.login(self.username, secret)
            finally:
                del secret
            self._conn = conn
        return self._conn

    def recent(self, since: float, matches: Matcher) -> list[EmailMessage]:
        conn = self._client()
        typ, _ = conn.select(self.folder, readonly=True)  # EXAMINE; re-issued each poll to see new mail
        if typ != "OK":
            raise RuntimeError("IMAP folder unavailable")
        day = time.strftime("%d-%b-%Y", time.gmtime(since - 86400))
        typ, data = conn.uid("SEARCH", None, "SINCE", day)
        uids = [u.decode() for u in (data[0] or b"").split()] if typ == "OK" and data else []
        found = []
        for uid in reversed(uids[-_MAX_SCAN:]):
            if uid not in self._meta:
                typ, data = conn.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                line, head = _fetch_payload(data)
                stamp = imaplib.Internaldate2tuple(line)
                parsed = email.message_from_bytes(head, policy=email.policy.default)
                self._meta[uid] = (str(parsed.get("From", "")), str(parsed.get("Subject", "")), time.mktime(stamp) if stamp else 0.0)
            sender, subject, received = self._meta[uid]
            if received < since or not matches(sender, subject):
                continue
            typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            msg = email.message_from_bytes(_fetch_payload(data)[1], policy=email.policy.default)
            parts = [(p.get_content_type(), p.get_content()) for p in msg.walk()
                     if p.get_content_maintype() == "text" and not p.is_attachment()]
            found.append(EmailMessage(sender, subject, received, _preferred_text(parts), key=f"imap:{self.username}:{uid}"))
        return found

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None


def _fetch_payload(data) -> tuple[bytes, bytes]:
    for item in data or []:
        if isinstance(item, tuple) and len(item) == 2:
            return item[0], item[1]
    raise RuntimeError("IMAP FETCH returned no message data")


# ---------------------------------------------------------------------------
# Config → inbox, and the bounded wait
# ---------------------------------------------------------------------------

def _normal_address(address: str) -> str:
    local, _, domain = (address or "").strip().lower().partition("@")
    return f"{local.split('+', 1)[0]}@{domain}" if local and domain else ""


def _resolve_op_reference(reference: str) -> str:
    from agent.vault_backends.base import _cfg
    from agent.vault_backends.onepassword import OnePasswordLoginBackend

    section = _cfg().get("onepassword") or {}
    return OnePasswordLoginBackend(section if isinstance(section, dict) else {}).read_reference(reference)


def lane_for_identifier(identifier: str, cfg: dict, resolve_ref: Callable[[str], str] = _resolve_op_reference):
    """The configured inbox that receives mail for ``identifier`` (an email address), or None."""
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None
    wanted = _normal_address(identifier)
    if not wanted:
        return None
    for inbox in cfg.get("inboxes") or []:
        if not isinstance(inbox, dict) or wanted not in {_normal_address(a) for a in inbox.get("addresses") or []}:
            continue
        command = inbox.get("gmail_command")
        if isinstance(command, list) and command and all(isinstance(x, str) for x in command):
            return GmailLane(command)
        imap = inbox.get("imap")
        if isinstance(imap, dict) and imap.get("host") and imap.get("username") and str(imap.get("password_ref") or "").startswith("op://"):
            ref = str(imap["password_ref"])
            return ImapLane(str(imap["host"]), int(imap.get("port") or 993), str(imap["username"]),
                            lambda: resolve_ref(ref), folder=str(imap.get("folder") or "INBOX"))
        return None
    return None


_USED_LOCK = threading.Lock()
_USED_KEYS: set[str] = set()  # messages whose code was already entered in this process


def _number(cfg: dict, key: str, default: float) -> float:
    try:
        value = float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def email_codes_config() -> dict:
    from agent.vault_backends.base import _cfg

    section = _cfg().get("email_codes") or {}
    return section if isinstance(section, dict) else {}


def wait_for_email_code(site_host: str, identifier: str, *, site_label: str = "", cfg: Optional[dict] = None,
                        clock: Callable[[], float] = time.time, sleep: Callable[[float], Any] = time.sleep,
                        lane_factory: Optional[Callable[[str, dict], Any]] = None) -> Optional[str]:
    """Wait briefly for the site's verification email in the inbox that receives ``identifier`` and
    return its code, or None (no inbox for this identifier, nothing arrived in time, the newest email
    holds no single code, or the inbox could not be read). The caller falls back to asking the user.

    The window opens ``lookback_seconds`` before the call (the site was asked to send the code just
    before) and the wait lasts at most ``wait_seconds``. Never logs content."""
    cfg = email_codes_config() if cfg is None else cfg
    lane = (lane_factory or lane_for_identifier)(identifier, cfg)
    if lane is None:
        return None
    start = clock()
    since = start - _number(cfg, "lookback_seconds", 600)
    deadline = start + min(_number(cfg, "wait_seconds", 75), 180)
    interval = max(_number(cfg, "poll_interval_seconds", 5), 1)

    def from_site(sender: str, subject: str) -> bool:
        return sender_matches_site(sender, subject, site_host, site_label)

    try:
        while True:
            try:
                with _USED_LOCK:
                    used = set(_USED_KEYS)
                fresh = [m for m in lane.recent(since, from_site) if not (m.key and m.key in used)]
            except Exception as exc:  # the message is never logged: it can carry mail content
                logger.info("email code: inbox read failed (%s); falling back to the user", type(exc).__name__)
                return None
            if fresh:
                newest = max(fresh, key=lambda m: m.received_at)
                code = pick_code([newest], site_host, since=since, site_label=site_label)
                if code and newest.key:
                    with _USED_LOCK:
                        _USED_KEYS.add(newest.key)
                logger.info("email code: site email found, code %s", "extracted" if code else "not unambiguous")
                return code
            if clock() + interval > deadline:
                logger.info("email code: no email from the site within the wait")
                return None
            sleep(interval)
    finally:
        try:
            lane.close()
        except Exception:
            pass

"""Fetch Greenhouse's emailed verification code via Gmail IMAP.

Greenhouse can gate a submission behind a one-time code emailed to the
candidate ("Security code for your application to {company}"). This reads
that code straight from the inbox so submit_application() can finish the
flow without a human checking email mid-run. Requires a Gmail App Password
(myaccount.google.com/apppasswords) — a real account password won't work
with IMAP if 2-Step Verification is on.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import os
import re
import time
from email.header import decode_header

log = logging.getLogger(__name__)


def _decode(s) -> str:
    if not s:
        return ""
    out = []
    for text, enc in decode_header(s):
        out.append(text.decode(enc or "utf-8", errors="ignore") if isinstance(text, bytes) else text)
    return "".join(out)


def _plain_text(msg) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True) or b""
                    text = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                    if ctype == "text/html":
                        text = re.sub(r"<[^>]+>", " ", text)
                    parts.append(text)
                except Exception:
                    continue
        return "\n".join(parts)
    try:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
    except Exception:
        return ""


_COMMON_WORDS = {
    "resubmit", "application", "greenhouse", "security", "verification",
    "code", "please", "regards", "sincerely", "thanks", "recruiting",
}


def _extract_code(body: str) -> str | None:
    """The code renders as its own isolated line/paragraph in the email — not
    inline with the instructional sentence — so line-scanning beats a regex
    that assumes the code sits right next to the word "code"."""
    candidates = []
    for i, line in enumerate(body.splitlines()):
        tok = line.strip()
        if re.fullmatch(r"[A-Za-z0-9]{6,10}", tok) and tok.lower() not in _COMMON_WORDS:
            candidates.append((i, tok))
    if not candidates:
        return None
    # Mixed-case alphanumeric (the actual code format) beats an all-lowercase
    # or all-uppercase word that happens to be the right length.
    mixed = [(i, t) for i, t in candidates if not t.islower() and not t.isupper() and not t.isdigit()]
    pool = mixed or candidates
    return pool[0][1]


def _try_fetch_once(company: str, since_ts: float, address: str, app_password: str) -> str | None:
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(address, app_password)
        imap.select("INBOX")
        status, data = imap.search(None, 'FROM', '"greenhouse-mail.io"')
        if status != "OK" or not data or not data[0]:
            return None
        for msg_id in reversed(data[0].split()[-10:]):
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode(msg.get("Subject", "")).lower()
            if not any(k in subject for k in ("security code", "verification code")):
                continue
            if company.lower() not in subject:
                continue
            date_tuple = email.utils.parsedate_tz(msg.get("Date"))
            if date_tuple and email.utils.mktime_tz(date_tuple) < since_ts - 30:
                continue  # a stale code from an earlier attempt, not this one
            code = _extract_code(_plain_text(msg))
            if code:
                return code
        return None
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def fetch_verification_code(
    company: str,
    since_ts: float,
    *,
    address: str | None = None,
    app_password: str | None = None,
    timeout_seconds: int = 90,
    poll_interval: int = 8,
) -> str | None:
    """Poll Gmail for a Greenhouse security-code email for `company` sent at
    or after `since_ts`. Returns the code, or None if it never arrives."""
    address = address or os.environ.get("GMAIL_ADDRESS")
    app_password = app_password or os.environ.get("GMAIL_APP_PASSWORD")
    if not address or not app_password:
        log.warning("email_code: GMAIL_ADDRESS/GMAIL_APP_PASSWORD not set")
        return None

    deadline = time.time() + timeout_seconds
    while True:
        try:
            code = _try_fetch_once(company, since_ts, address, app_password)
            if code:
                return code
        except Exception as e:
            log.warning("email_code: IMAP check failed: %s", e)
        if time.time() >= deadline:
            return None
        time.sleep(poll_interval)

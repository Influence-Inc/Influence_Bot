"""
Slack alert when the Claude API (Anthropic Console) is out of credits or
rejects the key.

Every Claude call here (AI drafts, reading inbound replies) passes its error
to `report()`. When the error means calls will keep failing until someone
acts (no credit balance, a usage limit reached, the key rejected) it posts
to the alerts channel's incoming webhook, `ALERTS_SLACK_WEBHOOK_URL`, once
and then again every 3 hours while it lasts. Passing errors (rate limits,
overload, a dropped connection) say nothing. Without the variable nothing is
sent, and sending never raises: a failed alert must not break a request.

The same variable and channel are used by the creator-list worker and the
outreach backend, so all Claude, OpenRouter, Groq and Monid alerts land in
one place.
"""

import hashlib
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

WEBHOOK_ENV = "ALERTS_SLACK_WEBHOOK_URL"
EVERY_S = 3 * 3600
# A marker file per alert, so every gunicorn worker shares the throttle.
MARKERS = Path(tempfile.gettempdir()) / "influence-alerts"
SERVICE = "Claude API (Anthropic Console) for Influence Bot"

# How Anthropic says the account can't pay for calls: "Your credit balance is
# too low to access the Anthropic API", "You have reached your specified API
# usage limits".
CREDITS = re.compile(
    r"credit balance|purchase credits|usage limit|spend limit|billing|quota|insufficient",
    re.I,
)


def problem(status: Optional[int], message: str) -> Optional[str]:
    """'out of credits' or 'key refused' for errors that persist; None otherwise."""
    if status == 402 or CREDITS.search(message or ""):
        return "out of credits"
    if status in (401, 403):
        return "key refused"
    return None


def notify(key: str, text: str, every_s: float = EVERY_S) -> bool:
    """Post `text` unless `key` was sent in the last `every_s`. True if sent."""
    url = (os.environ.get(WEBHOOK_ENV) or "").strip()
    if not url:
        return False
    marker = MARKERS / hashlib.sha1(key.encode()).hexdigest()
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < every_s:
            return False
        MARKERS.mkdir(parents=True, exist_ok=True)
        marker.touch()
        requests.post(url, json={"text": text[:3000]}, timeout=10).raise_for_status()
        return True
    except Exception as exc:  # never breaks the caller
        logger.warning("Slack alert not sent: %s", exc)
        return False


def report(exc: BaseException) -> bool:
    """Alert when a Claude call's error means it's out of credits or the key
    is refused. True if it was that."""
    status = getattr(exc, "status_code", None)
    message = " ".join(str(exc).split())[:300]
    found = problem(status, message)
    if found is None:
        return False
    detail = f"HTTP {status} ({message})" if status else message
    notify(
        f"claude:{found}",
        f":rotating_light: *{SERVICE}: {found}*: {detail}. AI drafts and reading "
        "inbound replies fail until it is topped up (console.anthropic.com → "
        "Plans & Billing) or the key is fixed.",
    )
    return True

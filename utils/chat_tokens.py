"""
Token helpers for chat-space magic links and session cookies.

- Magic links: short-lived URLs emailed to creators / embedded in Slack
  buttons for brands. Encoded with itsdangerous (signed + timestamped),
  single-use (consumed on exchange for a session row).
- Session cookies: signed cookie carrying a `session_id` that maps to a row
  in `chat_sessions`. Server-side validation enforces revocation + expiry.

itsdangerous ships with Flask, so no new dependency.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import Config
from models.models import ChatSession, SessionLocal

logger = logging.getLogger(__name__)

_INVITE_SALT = "influence-chat-invite-v1"
_SESSION_SALT = "influence-chat-session-v1"


def _serializer(salt: str) -> URLSafeTimedSerializer:
    secret = Config.CHAT_SECRET_KEY
    if not secret:
        raise RuntimeError(
            "CHAT_SECRET_KEY (or SLACK_SIGNING_SECRET) must be set "
            "for chat tokens to be issued."
        )
    return URLSafeTimedSerializer(secret_key=secret, salt=salt)


# ---------------------------------------------------------------------------
# Magic-link tokens (used in email links and brand Slack buttons)
# ---------------------------------------------------------------------------

def make_invite_token(
    *, chat_space_id: int, party: str, identifier: Optional[str] = None
) -> str:
    """
    Sign a magic-link payload. `identifier` is optional — for brand
    workspace-wide buttons it's None, for creator emails it's the email.
    """
    payload = {"sid": chat_space_id, "p": party}
    if identifier:
        payload["i"] = identifier
    return _serializer(_INVITE_SALT).dumps(payload)


def read_invite_token(token: str) -> Optional[dict]:
    """
    Verify + decode. Tries the long-lived brand TTL first (since brand
    tokens are baked into Slack messages that may sit around for weeks),
    then falls back to the shorter creator TTL.
    """
    ser = _serializer(_INVITE_SALT)
    try:
        return ser.loads(token, max_age=Config.CHAT_BRAND_LINK_TTL)
    except SignatureExpired:
        logger.info("Chat invite token expired (>%ds)", Config.CHAT_BRAND_LINK_TTL)
    except BadSignature:
        logger.info("Chat invite token signature invalid")
    except Exception as exc:
        logger.warning("Chat invite token decode failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Session cookies
# ---------------------------------------------------------------------------

SESSION_COOKIE = "influence_chat_session"


def create_session(
    *,
    chat_space_id: int,
    party: str,
    identifier: str,
    display_name: Optional[str] = None,
) -> tuple[ChatSession, str]:
    """
    Persist a chat session row and return (row, signed_cookie_value).
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=Config.CHAT_SESSION_TTL)
    db = SessionLocal()
    try:
        row = ChatSession(
            chat_space_id=chat_space_id,
            party=party,
            identifier=identifier,
            display_name=display_name,
            expires_at=expires_at,
            last_used_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        cookie = _serializer(_SESSION_SALT).dumps({"sess": row.id})
        return row, cookie
    finally:
        db.close()


def load_session(cookie_value: Optional[str]) -> Optional[ChatSession]:
    """Validate cookie + load active session, or return None."""
    if not cookie_value:
        return None
    try:
        payload = _serializer(_SESSION_SALT).loads(
            cookie_value, max_age=Config.CHAT_SESSION_TTL
        )
    except (BadSignature, SignatureExpired):
        return None
    except Exception as exc:
        logger.warning("Chat session cookie decode failed: %s", exc)
        return None

    sess_id = payload.get("sess")
    if not sess_id:
        return None

    db = SessionLocal()
    try:
        row = db.query(ChatSession).get(sess_id)
        if row is None or row.revoked_at is not None:
            return None
        if row.expires_at and row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            return None
        row.last_used_at = datetime.now(timezone.utc)
        db.commit()
        # detach for the caller; refresh first
        db.refresh(row)
        db.expunge(row)
        return row
    finally:
        db.close()


def revoke_sessions_for_space(chat_space_id: int) -> int:
    """Revoke every active session attached to a chat space."""
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        rows = (
            db.query(ChatSession)
            .filter(
                ChatSession.chat_space_id == chat_space_id,
                ChatSession.revoked_at.is_(None),
            )
            .all()
        )
        for row in rows:
            row.revoked_at = now
        db.commit()
        return len(rows)
    finally:
        db.close()

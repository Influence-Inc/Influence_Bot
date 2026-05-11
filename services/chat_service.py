"""
Chat space business logic.

Responsibilities:
- Create or reuse a ChatSpace when a brand requests changes on a review.
- Compute the reuse key (same creator + campaign + brand → same chat).
- Post messages, store attachments, react to messages.
- Track unread counts per member.
- Archive a chat space (and revoke its sessions) when a campaign ends.

Slack/email notifications themselves live in bot/actions.py and the
notification helpers — this module only persists state.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.exc import IntegrityError

from config import Config
from models.models import (
    ChatAttachment,
    ChatMember,
    ChatMessage,
    ChatReaction,
    ChatSpace,
    ReviewSubmission,
    SessionLocal,
)
from services.brand_routing import find_install_for_brand_name
from utils.chat_tokens import revoke_sessions_for_space

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reuse key
# ---------------------------------------------------------------------------

def _slug(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def compute_reuse_key(
    *,
    creator_email: Optional[str],
    creator_username: Optional[str],
    campaign_slug: Optional[str],
    campaign_name: Optional[str],
    brand_name: Optional[str],
) -> str:
    """
    Deterministic key for (creator, campaign, brand). Prefers stable
    identifiers (email, slug) when present, falls back to slugified names.
    """
    creator = (creator_email or "").strip().lower() or _slug(creator_username)
    campaign = _slug(campaign_slug) or _slug(campaign_name)
    brand = _slug(brand_name)
    raw = f"{creator}|{campaign}|{brand}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Create / reuse
# ---------------------------------------------------------------------------

def get_or_create_for_review(
    review_id: int,
    *,
    workspace_team_id: Optional[str] = None,
) -> Optional[ChatSpace]:
    """
    Called when a brand requests changes on a review. Reuses an active chat
    space if one already exists for (creator, campaign, brand); otherwise
    creates a new one.

    Returns a detached ChatSpace (or None if the review can't be found).
    """
    db = SessionLocal()
    try:
        review = db.query(ReviewSubmission).get(review_id)
        if review is None:
            logger.warning("get_or_create_for_review: review_id=%s not found", review_id)
            return None

        reuse_key = compute_reuse_key(
            creator_email=review.creator_email,
            creator_username=review.creator_username,
            campaign_slug=review.campaign_slug,
            campaign_name=review.campaign_name,
            brand_name=review.brand_name,
        )

        existing = (
            db.query(ChatSpace)
            .filter(
                ChatSpace.reuse_key == reuse_key,
                ChatSpace.status == "active",
            )
            .order_by(ChatSpace.created_at.desc())
            .first()
        )

        brand_install = find_install_for_brand_name(review.brand_name)
        brand_install_id = brand_install.id if brand_install else None
        resolved_team_id = workspace_team_id or (brand_install.team_id if brand_install else None)

        if existing is not None:
            existing.latest_review_id = review.id
            if resolved_team_id and not existing.workspace_team_id:
                existing.workspace_team_id = resolved_team_id
            if brand_install_id and not existing.brand_install_id:
                existing.brand_install_id = brand_install_id
            db.commit()
            db.refresh(existing)
            db.expunge(existing)
            return existing

        space = ChatSpace(
            reuse_key=reuse_key,
            creator_username=review.creator_username,
            creator_email=review.creator_email,
            campaign_slug=review.campaign_slug,
            campaign_name=review.campaign_name,
            brand_name=review.brand_name,
            workspace_team_id=resolved_team_id,
            brand_install_id=brand_install_id,
            latest_review_id=review.id,
            status="active",
        )
        db.add(space)
        try:
            db.commit()
        except IntegrityError:
            # Concurrent create — fall back to the existing row.
            db.rollback()
            space = (
                db.query(ChatSpace)
                .filter(ChatSpace.reuse_key == reuse_key, ChatSpace.status == "active")
                .order_by(ChatSpace.created_at.desc())
                .first()
            )
            if space is None:
                raise
        db.refresh(space)

        # Pre-create stable member rows. Identifier conventions:
        #   creator: lowercased email if present, else "@username"
        #   brand:   slack team_id if known, else slugified brand_name
        creator_ident = (
            (review.creator_email or "").strip().lower()
            or f"@{review.creator_username}"
        )
        brand_ident = resolved_team_id or _slug(review.brand_name) or "brand"
        for party, ident, name in (
            ("creator", creator_ident, review.creator_username),
            ("brand", brand_ident, review.brand_name or "Brand"),
        ):
            existing_member = (
                db.query(ChatMember)
                .filter_by(chat_space_id=space.id, party=party, identifier=ident)
                .first()
            )
            if existing_member is None:
                db.add(ChatMember(
                    chat_space_id=space.id,
                    party=party,
                    identifier=ident,
                    display_name=name,
                ))
        db.commit()
        db.refresh(space)
        db.expunge(space)
        return space
    finally:
        db.close()


def find_by_id(chat_space_id: int) -> Optional[ChatSpace]:
    db = SessionLocal()
    try:
        row = db.query(ChatSpace).get(chat_space_id)
        if row is None:
            return None
        db.expunge(row)
        return row
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Members + unread
# ---------------------------------------------------------------------------

def upsert_member(
    *,
    chat_space_id: int,
    party: str,
    identifier: str,
    display_name: Optional[str] = None,
) -> ChatMember:
    db = SessionLocal()
    try:
        row = (
            db.query(ChatMember)
            .filter_by(chat_space_id=chat_space_id, party=party, identifier=identifier)
            .first()
        )
        if row is None:
            row = ChatMember(
                chat_space_id=chat_space_id,
                party=party,
                identifier=identifier,
                display_name=display_name,
            )
            db.add(row)
        elif display_name and not row.display_name:
            row.display_name = display_name
        db.commit()
        db.refresh(row)
        db.expunge(row)
        return row
    finally:
        db.close()


def mark_read(*, chat_space_id: int, party: str, identifier: str, up_to_message_id: int) -> None:
    db = SessionLocal()
    try:
        row = (
            db.query(ChatMember)
            .filter_by(chat_space_id=chat_space_id, party=party, identifier=identifier)
            .first()
        )
        if row is None:
            return
        if row.last_read_message_id is None or up_to_message_id > row.last_read_message_id:
            row.last_read_message_id = up_to_message_id
        row.last_seen_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def unread_count(*, chat_space_id: int, party: str, identifier: str) -> int:
    """Number of messages newer than the member's last_read_message_id, excluding their own."""
    db = SessionLocal()
    try:
        member = (
            db.query(ChatMember)
            .filter_by(chat_space_id=chat_space_id, party=party, identifier=identifier)
            .first()
        )
        last_read = member.last_read_message_id if member else None
        q = db.query(ChatMessage).filter(ChatMessage.chat_space_id == chat_space_id)
        if last_read:
            q = q.filter(ChatMessage.id > last_read)
        q = q.filter(ChatMessage.sender_party != party)
        return q.count()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

_MAX_BODY = 4000


def post_message(
    *,
    chat_space_id: int,
    sender_party: str,
    sender_identifier: Optional[str],
    sender_display_name: Optional[str],
    body: str,
) -> Optional[ChatMessage]:
    body = (body or "").strip()
    if len(body) > _MAX_BODY:
        body = body[:_MAX_BODY]
    if not body:
        return None

    db = SessionLocal()
    try:
        space = db.query(ChatSpace).get(chat_space_id)
        if space is None or space.status != "active":
            return None
        msg = ChatMessage(
            chat_space_id=chat_space_id,
            sender_party=sender_party,
            sender_identifier=sender_identifier,
            sender_display_name=sender_display_name,
            body=body,
        )
        db.add(msg)
        space.last_message_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(msg)
        db.expunge(msg)
        return msg
    finally:
        db.close()


def list_messages(
    *,
    chat_space_id: int,
    since_id: int = 0,
    limit: int = 200,
) -> list[dict]:
    """Return serializable message dicts (oldest first)."""
    db = SessionLocal()
    try:
        q = (
            db.query(ChatMessage)
            .filter(ChatMessage.chat_space_id == chat_space_id)
        )
        if since_id:
            q = q.filter(ChatMessage.id > since_id)
        rows = q.order_by(ChatMessage.id.asc()).limit(limit).all()

        # Pre-fetch reactions + attachments for these messages in one go.
        ids = [r.id for r in rows]
        reactions_by_msg: dict[int, list[ChatReaction]] = {}
        attachments_by_msg: dict[int, list[ChatAttachment]] = {}
        if ids:
            for r in db.query(ChatReaction).filter(ChatReaction.message_id.in_(ids)).all():
                reactions_by_msg.setdefault(r.message_id, []).append(r)
            for a in db.query(ChatAttachment).filter(ChatAttachment.message_id.in_(ids)).all():
                attachments_by_msg.setdefault(a.message_id, []).append(a)

        out: list[dict] = []
        for r in rows:
            reactions: dict[str, int] = {}
            for reaction in reactions_by_msg.get(r.id, []):
                reactions[reaction.emoji] = reactions.get(reaction.emoji, 0) + 1
            out.append({
                "id": r.id,
                "party": r.sender_party,
                "sender": r.sender_display_name or r.sender_identifier or r.sender_party,
                "body": r.body,
                "created_at": r.created_at.replace(tzinfo=timezone.utc).isoformat() if r.created_at else None,
                "reactions": reactions,
                "attachments": [
                    {
                        "id": a.id,
                        "filename": a.filename,
                        "content_type": a.content_type,
                        "size": a.size_bytes,
                    }
                    for a in attachments_by_msg.get(r.id, [])
                ],
            })
        return out
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

ALLOWED_ATTACHMENT_MIMES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}


def store_attachment(
    *,
    message_id: int,
    filename: str,
    content_type: str,
    data: bytes,
) -> Optional[ChatAttachment]:
    if content_type not in ALLOWED_ATTACHMENT_MIMES:
        logger.info("Rejecting attachment with mime=%s", content_type)
        return None
    if len(data) > Config.CHAT_MAX_ATTACHMENT_BYTES:
        logger.info("Rejecting attachment over size limit: %d bytes", len(data))
        return None

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[-100:] or "upload.bin"
    storage_dir = Config.CHAT_UPLOADS_DIR
    os.makedirs(storage_dir, exist_ok=True)
    storage_filename = f"{message_id}-{secrets.token_hex(8)}-{safe_name}"
    storage_path = os.path.join(storage_dir, storage_filename)
    with open(storage_path, "wb") as fh:
        fh.write(data)

    db = SessionLocal()
    try:
        att = ChatAttachment(
            message_id=message_id,
            filename=safe_name,
            content_type=content_type,
            size_bytes=len(data),
            storage_path=storage_path,
        )
        db.add(att)
        db.commit()
        db.refresh(att)
        db.expunge(att)
        return att
    finally:
        db.close()


def find_attachment(attachment_id: int) -> Optional[ChatAttachment]:
    db = SessionLocal()
    try:
        row = db.query(ChatAttachment).get(attachment_id)
        if row is None:
            return None
        db.expunge(row)
        return row
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------

def toggle_reaction(
    *,
    message_id: int,
    party: str,
    identifier: str,
    emoji: str,
) -> bool:
    """Adds the reaction if absent, removes if present. Returns True if now present."""
    emoji = (emoji or "").strip()
    if not emoji or len(emoji) > 32:
        return False
    db = SessionLocal()
    try:
        row = (
            db.query(ChatReaction)
            .filter_by(
                message_id=message_id, party=party, identifier=identifier, emoji=emoji
            )
            .first()
        )
        if row is not None:
            db.delete(row)
            db.commit()
            return False
        db.add(ChatReaction(
            message_id=message_id, party=party, identifier=identifier, emoji=emoji,
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        return True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def archive_space(chat_space_id: int) -> bool:
    db = SessionLocal()
    try:
        space = db.query(ChatSpace).get(chat_space_id)
        if space is None or space.status == "archived":
            return False
        space.status = "archived"
        space.archived_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    revoke_sessions_for_space(chat_space_id)
    return True


def archive_for_campaign(
    *,
    campaign_slug: Optional[str] = None,
    campaign_name: Optional[str] = None,
    brand_name: Optional[str] = None,
) -> int:
    """Archive every active chat space matching the given campaign + brand."""
    if not (campaign_slug or campaign_name):
        return 0
    db = SessionLocal()
    try:
        q = db.query(ChatSpace).filter(ChatSpace.status == "active")
        if campaign_slug:
            q = q.filter(ChatSpace.campaign_slug == campaign_slug)
        elif campaign_name:
            q = q.filter(ChatSpace.campaign_name == campaign_name)
        if brand_name:
            q = q.filter(ChatSpace.brand_name == brand_name)
        ids = [s.id for s in q.all()]
    finally:
        db.close()

    archived = 0
    for sid in ids:
        if archive_space(sid):
            archived += 1
    return archived


# ---------------------------------------------------------------------------
# Admin listing
# ---------------------------------------------------------------------------

def list_spaces_for_admin(
    *,
    status: Optional[str] = None,
    search: Optional[str] = None,
    brand: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    db = SessionLocal()
    try:
        q = db.query(ChatSpace)
        if status:
            q = q.filter(ChatSpace.status == status)
        if brand:
            q = q.filter(ChatSpace.brand_name == brand)
        if search:
            like = f"%{search}%"
            q = q.filter(
                (ChatSpace.creator_username.ilike(like))
                | (ChatSpace.creator_email.ilike(like))
                | (ChatSpace.campaign_name.ilike(like))
                | (ChatSpace.brand_name.ilike(like))
            )
        q = q.order_by(ChatSpace.last_message_at.desc().nullslast(), ChatSpace.created_at.desc())
        rows = q.limit(limit).all()
        out = []
        for r in rows:
            out.append({
                "id": r.id,
                "creator_username": r.creator_username,
                "creator_email": r.creator_email,
                "campaign_name": r.campaign_name,
                "brand_name": r.brand_name,
                "status": r.status,
                "created_at": r.created_at,
                "last_message_at": r.last_message_at,
            })
        return out
    finally:
        db.close()


def admin_stats() -> dict:
    db = SessionLocal()
    try:
        active = db.query(ChatSpace).filter(ChatSpace.status == "active").count()
        archived = db.query(ChatSpace).filter(ChatSpace.status == "archived").count()
        creators = (
            db.query(ChatSpace.creator_username)
            .filter(ChatSpace.status == "active")
            .distinct()
            .count()
        )
        return {"active": active, "archived": archived, "active_creators": creators}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Helpers callers reuse
# ---------------------------------------------------------------------------

def creator_identifier_for(space: ChatSpace) -> str:
    return (
        (space.creator_email or "").strip().lower()
        or f"@{space.creator_username}"
    )


def brand_identifier_for(space: ChatSpace) -> str:
    return space.workspace_team_id or _slug(space.brand_name) or "brand"


def members_iter(chat_space_id: int) -> Iterable[ChatMember]:
    db = SessionLocal()
    try:
        rows = db.query(ChatMember).filter_by(chat_space_id=chat_space_id).all()
        for r in rows:
            db.expunge(r)
        return rows
    finally:
        db.close()

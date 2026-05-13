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
from services.chat_pubsub import publish as _pubsub_publish
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
            public_slug=_generate_public_slug(db),
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


def find_by_slug(public_slug: str) -> Optional[ChatSpace]:
    if not public_slug:
        return None
    db = SessionLocal()
    try:
        row = db.query(ChatSpace).filter_by(public_slug=public_slug).first()
        if row is None:
            return None
        db.expunge(row)
        return row
    finally:
        db.close()


def _generate_public_slug(db, *, max_attempts: int = 8) -> str:
    """
    URL-safe random slug (12 chars from secrets.token_urlsafe). 72 bits of
    entropy is plenty — but we still loop on the unique constraint just
    in case of collisions.
    """
    for _ in range(max_attempts):
        candidate = secrets.token_urlsafe(9)
        clash = db.query(ChatSpace).filter_by(public_slug=candidate).first()
        if clash is None:
            return candidate
    # Astronomically unlikely; fall back to a longer slug.
    return secrets.token_urlsafe(18)


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
    changed = False
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
            changed = True
        row.last_seen_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    if changed:
        _pubsub_publish(
            chat_space_id,
            "read",
            {
                "party": party,
                "identifier": identifier,
                "last_read_message_id": up_to_message_id,
            },
        )


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
    publish: bool = True,
    allow_empty: bool = False,
) -> Optional[ChatMessage]:
    """
    Persist a new message. Set `publish=False` if the caller is about to
    attach a file and wants to broadcast the complete message (body +
    attachments) via `publish_message(msg.id)` once the attachment row is
    written; that way SSE subscribers see one event with everything.

    `allow_empty=True` lets the caller post an image-only message (no
    text body). Without this guard, image uploads with no caption would
    be rejected with `None`.
    """
    body = (body or "").strip()
    if len(body) > _MAX_BODY:
        body = body[:_MAX_BODY]
    if not body and not allow_empty:
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
    finally:
        db.close()

    if publish:
        publish_message(msg.id)
    return msg


def publish_message(message_id: int) -> None:
    """Emit the full serialized form of an existing message to SSE subscribers."""
    db = SessionLocal()
    try:
        msg = db.query(ChatMessage).get(message_id)
        if msg is None:
            return
        chat_space_id = msg.chat_space_id
        reactions: dict[str, int] = {}
        for r in db.query(ChatReaction).filter(ChatReaction.message_id == message_id).all():
            reactions[r.emoji] = reactions.get(r.emoji, 0) + 1
        attachments = [
            {
                "id": a.id,
                "filename": a.filename,
                "content_type": a.content_type,
                "size": a.size_bytes,
            }
            for a in db.query(ChatAttachment).filter(ChatAttachment.message_id == message_id).all()
        ]
        payload = {
            "id": msg.id,
            "party": msg.sender_party,
            "sender": msg.sender_display_name or msg.sender_identifier or msg.sender_party,
            "body": msg.body,
            "created_at": msg.created_at.replace(tzinfo=timezone.utc).isoformat()
            if msg.created_at else None,
            "reactions": reactions,
            "attachments": attachments,
        }
    finally:
        db.close()
    _pubsub_publish(chat_space_id, "message", payload)


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
    # Some mobile browsers report the (non-standard but widespread) "image/jpg".
    "image/jpg",
    "image/png",
    "image/gif",
    "image/webp",
    # iOS shares photos as HEIC/HEIF when the user hasn't enabled "Most
    # Compatible" in Camera settings.
    "image/heic",
    "image/heif",
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
        msg = db.query(ChatMessage).get(message_id)
        if msg is None:
            return False
        chat_space_id = msg.chat_space_id

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
            now_present = False
        else:
            db.add(ChatReaction(
                message_id=message_id, party=party, identifier=identifier, emoji=emoji,
            ))
            try:
                db.commit()
                now_present = True
            except IntegrityError:
                db.rollback()
                now_present = True

        counts: dict[str, int] = {}
        for r in db.query(ChatReaction).filter(ChatReaction.message_id == message_id).all():
            counts[r.emoji] = counts.get(r.emoji, 0) + 1
    finally:
        db.close()

    _pubsub_publish(
        chat_space_id,
        "reaction",
        {"message_id": message_id, "counts": counts},
    )
    return now_present


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


def reopen_space(chat_space_id: int) -> bool:
    """Re-activate an archived chat space. Sessions stay revoked — both
    parties need fresh magic links."""
    db = SessionLocal()
    try:
        space = db.query(ChatSpace).get(chat_space_id)
        if space is None or space.status == "active":
            return False
        space.status = "active"
        space.archived_at = None
        db.commit()
        return True
    finally:
        db.close()


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
    """Headline counts + top-revisions-per-campaign for the dashboard."""
    from datetime import timedelta
    from sqlalchemy import func

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

        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        recently_active = (
            db.query(ChatSpace)
            .filter(ChatSpace.last_message_at >= week_ago)
            .count()
        )

        # Top campaigns by number of changes_requested decisions.
        revisions_rows = (
            db.query(
                ReviewSubmission.campaign_name,
                ReviewSubmission.brand_name,
                func.count(ReviewSubmission.id).label("n"),
            )
            .filter(ReviewSubmission.decision == "changes_requested")
            .group_by(ReviewSubmission.campaign_name, ReviewSubmission.brand_name)
            .order_by(func.count(ReviewSubmission.id).desc())
            .limit(5)
            .all()
        )
        top_revisions = [
            {"campaign_name": r[0], "brand_name": r[1], "revisions": r[2]}
            for r in revisions_rows
        ]

        return {
            "active": active,
            "archived": archived,
            "active_creators": creators,
            "recently_active": recently_active,
            "top_revisions": top_revisions,
        }
    finally:
        db.close()


def export_transcript(chat_space_id: int) -> Optional[dict]:
    """
    Returns a dict snapshot of a chat space: meta + members + messages
    (with attachment metadata and reaction counts). Used by both the JSON
    and Markdown export routes — keep this serialization shape stable.
    """
    space = find_by_id(chat_space_id)
    if space is None:
        return None
    db = SessionLocal()
    try:
        members = [
            {
                "party": m.party,
                "identifier": m.identifier,
                "display_name": m.display_name,
                "last_read_message_id": m.last_read_message_id,
                "last_seen_at": (m.last_seen_at.replace(tzinfo=timezone.utc).isoformat()
                                 if m.last_seen_at else None),
            }
            for m in db.query(ChatMember).filter_by(chat_space_id=chat_space_id).all()
        ]
    finally:
        db.close()
    messages = list_messages(chat_space_id=chat_space_id, limit=10000)
    return {
        "chat_space": {
            "id": space.id,
            "creator_username": space.creator_username,
            "creator_email": space.creator_email,
            "campaign_slug": space.campaign_slug,
            "campaign_name": space.campaign_name,
            "brand_name": space.brand_name,
            "status": space.status,
            "created_at": space.created_at.replace(tzinfo=timezone.utc).isoformat()
                          if space.created_at else None,
            "last_message_at": space.last_message_at.replace(tzinfo=timezone.utc).isoformat()
                               if space.last_message_at else None,
            "archived_at": space.archived_at.replace(tzinfo=timezone.utc).isoformat()
                           if space.archived_at else None,
        },
        "members": members,
        "messages": messages,
    }


def transcript_to_markdown(transcript: dict) -> str:
    """Render an export_transcript() result as a human-readable Markdown doc."""
    s = transcript["chat_space"]
    lines: list[str] = []
    lines.append(f"# Chat transcript — {s.get('campaign_name') or 'Untitled campaign'}")
    lines.append("")
    lines.append(f"- **Creator:** @{s.get('creator_username') or '?'}"
                 + (f" ({s['creator_email']})" if s.get("creator_email") else ""))
    lines.append(f"- **Brand:** {s.get('brand_name') or '—'}")
    lines.append(f"- **Status:** {s.get('status')}")
    lines.append(f"- **Created:** {s.get('created_at') or '—'}")
    if s.get("archived_at"):
        lines.append(f"- **Archived:** {s['archived_at']}")
    lines.append("")
    lines.append("---")
    lines.append("")
    if not transcript["messages"]:
        lines.append("_No messages._")
        return "\n".join(lines) + "\n"
    for m in transcript["messages"]:
        sender = m.get("sender") or m.get("party")
        when = m.get("created_at") or ""
        lines.append(f"**{sender}** · _{m.get('party')}_ · {when}")
        body = (m.get("body") or "").strip()
        if body:
            for ln in body.splitlines():
                lines.append(f"> {ln}")
        for a in m.get("attachments") or []:
            lines.append(f"> 📎 _{a.get('filename')}_ ({a.get('content_type')}, {a.get('size')} bytes)")
        reactions = m.get("reactions") or {}
        if reactions:
            lines.append("> " + " ".join(f"{k}×{v}" for k, v in reactions.items()))
        lines.append("")
    return "\n".join(lines) + "\n"


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


def read_state_for_space(chat_space_id: int) -> dict[str, int]:
    """
    Map of party -> highest `last_read_message_id` among that party's
    members. Used by the chat UI to render per-message read receipts.
    Returns 0 for parties with no recorded read.
    """
    db = SessionLocal()
    try:
        rows = db.query(ChatMember).filter_by(chat_space_id=chat_space_id).all()
        out: dict[str, int] = {}
        for r in rows:
            last = r.last_read_message_id or 0
            if last > out.get(r.party, 0):
                out[r.party] = last
        return out
    finally:
        db.close()


def members_iter(chat_space_id: int) -> Iterable[ChatMember]:
    db = SessionLocal()
    try:
        rows = db.query(ChatMember).filter_by(chat_space_id=chat_space_id).all()
        for r in rows:
            db.expunge(r)
        return rows
    finally:
        db.close()

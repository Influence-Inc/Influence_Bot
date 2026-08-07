"""
Chat space business logic.

Responsibilities:
- Create or reuse the campaign-long ChatSpace for a review submission.
- Compute the reuse key (same creator + campaign + brand → same chat).
- Post messages and review events, store attachments, react to messages.
- Track unread counts per member.
- Archive a chat space (and revoke its sessions) when a campaign ends.

A chat space spans the whole campaign: every review the creator submits for
the same (creator, campaign, brand) lands in the *same* space, so neither
side ever loses the earlier feedback. New submissions and approvals are
recorded in-line as event messages (see KIND_* below) rather than by opening
a fresh space.

Slack/email notifications themselves live in bot/actions.py and the
notification helpers — this module only persists state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import func
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

# ChatMessage.kind values. "text" is an ordinary message someone typed; the
# rest are event rows the chat UI renders as widgets instead of bubbles.
KIND_TEXT = "text"
KIND_REVIEW_SUBMISSION = "review_submission"
KIND_REVIEW_DECISION = "review_decision"


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


def reuse_keys_for_review(review: ReviewSubmission) -> list[str]:
    """
    Every key a review's chat space could have been filed under, newest
    convention first.

    `creator.email` is nullable in the review webhook and often gets filled
    in partway through a campaign. Since the key prefers email when it's
    there, the same creator can hash to a different key between drafts —
    which would silently strand the conversation in a space nobody links to
    again. Matching on either key keeps one space for the campaign.
    """
    keys = [
        compute_reuse_key(
            creator_email=review.creator_email,
            creator_username=review.creator_username,
            campaign_slug=review.campaign_slug,
            campaign_name=review.campaign_name,
            brand_name=review.brand_name,
        ),
        compute_reuse_key(
            creator_email=None,
            creator_username=review.creator_username,
            campaign_slug=review.campaign_slug,
            campaign_name=review.campaign_name,
            brand_name=review.brand_name,
        ),
    ]
    # dict.fromkeys de-dupes while keeping order (the two match when the
    # review has no email).
    return list(dict.fromkeys(keys))


# ---------------------------------------------------------------------------
# Create / reuse
# ---------------------------------------------------------------------------

def get_or_create_for_review(
    review_id: int,
    *,
    workspace_team_id: Optional[str] = None,
) -> Optional[ChatSpace]:
    """
    Resolve the chat space a review belongs to, creating it on first use.

    One chat space per (creator, campaign, brand), reused for the whole
    campaign: the creator's second, third and tenth draft all land in the
    space opened for their first one, so the brand's earlier notes stay
    visible instead of being stranded in a space nobody can reach any more.
    The "Request Changes" button baked into review N's Slack message
    therefore routes into the same running conversation.

    Only a campaign-end archive breaks the reuse — an archived space is
    never resurrected. A space closed by an approval (the legacy
    "approved" status) is reopened, since the campaign is still running.

    Returns a detached ChatSpace (or None if the review can't be found).
    """
    db = SessionLocal()
    try:
        review = db.query(ReviewSubmission).get(review_id)
        if review is None:
            logger.warning("get_or_create_for_review: review_id=%s not found", review_id)
            return None

        candidate_keys = reuse_keys_for_review(review)
        reuse_key = candidate_keys[0]

        # The campaign-long space for this (creator, campaign, brand).
        # Prefer a still-open one; among equals take the newest. Deploys
        # that ran the old one-space-per-review code can leave several
        # rows sharing a key — they were always written with the same
        # reuse_key, so the newest open one continues the conversation.
        existing = (
            db.query(ChatSpace)
            .filter(
                ChatSpace.reuse_key.in_(candidate_keys),
                ChatSpace.status != "archived",
            )
            .order_by(
                (ChatSpace.status != "active"),  # active before approved
                ChatSpace.created_at.desc(),
            )
            .first()
        )

        brand_install = find_install_for_brand_name(review.brand_name)
        brand_install_id = brand_install.id if brand_install else None
        resolved_team_id = workspace_team_id or (brand_install.team_id if brand_install else None)

        if existing is not None:
            if resolved_team_id and not existing.workspace_team_id:
                existing.workspace_team_id = resolved_team_id
            if brand_install_id and not existing.brand_install_id:
                existing.brand_install_id = brand_install_id
            # Point the space at the newest review, so Slack threading and
            # the review-linked lookups follow the live submission.
            existing.latest_review_id = review.id
            # Backfill identity fields a legacy row may be missing.
            if not existing.creator_email and review.creator_email:
                existing.creator_email = review.creator_email
            if not existing.campaign_slug and review.campaign_slug:
                existing.campaign_slug = review.campaign_slug
            if not existing.campaign_name and review.campaign_name:
                existing.campaign_name = review.campaign_name
            if not existing.brand_name and review.brand_name:
                existing.brand_name = review.brand_name
            # A previous approval closed the space; the campaign is still
            # running and a new draft just arrived, so reopen it. Sessions
            # stay revoked — both parties re-enter via a fresh magic link.
            if existing.status == "approved":
                existing.status = "active"
                existing.archived_at = None
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
        db.commit()
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


def find_by_review_id(review_id: int) -> Optional[ChatSpace]:
    """Return the chat space currently pointing at this review, or None.

    Only matches when the review is the space's *latest* — use
    :func:`find_space_for_review` to resolve older reviews too.
    """
    if not review_id:
        return None
    db = SessionLocal()
    try:
        row = (
            db.query(ChatSpace)
            .filter(ChatSpace.latest_review_id == review_id)
            .order_by(ChatSpace.created_at.desc())
            .first()
        )
        if row is None:
            return None
        db.expunge(row)
        return row
    finally:
        db.close()


def find_space_for_review(review_id: int) -> Optional[ChatSpace]:
    """
    Resolve the chat space a review lives in — including reviews that are
    no longer the newest one.

    A space now spans the whole campaign, so `latest_review_id` tracks only
    the most recent submission. Falls back to the (creator, campaign,
    brand) reuse key, which every submission in the campaign shares.
    Does not create anything.
    """
    if not review_id:
        return None
    direct = find_by_review_id(review_id)
    if direct is not None:
        return direct

    db = SessionLocal()
    try:
        review = db.query(ReviewSubmission).get(review_id)
        if review is None:
            return None
        row = (
            db.query(ChatSpace)
            .filter(ChatSpace.reuse_key.in_(reuse_keys_for_review(review)))
            .order_by(
                (ChatSpace.status == "archived"),  # open spaces first
                ChatSpace.created_at.desc(),
            )
            .first()
        )
        if row is None:
            return None
        db.expunge(row)
        return row
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


def find_for_campaign_creator(
    *, campaign_slug: Optional[str], creator_username: Optional[str]
) -> Optional[ChatSpace]:
    """Resolve a creator's chat space from the campaign slug + Instagram
    username — the identifiers the campaign dashboard already has. Prefers a
    still-open (non-archived) space and, among those, the most recently active.
    Matches the username case-insensitively; an exact (non-LIKE) comparison so
    underscores in handles aren't treated as wildcards."""
    campaign_slug = (campaign_slug or "").strip()
    creator_username = (creator_username or "").strip().lstrip("@")
    if not campaign_slug or not creator_username:
        return None
    db = SessionLocal()
    try:
        row = (
            db.query(ChatSpace)
            .filter(
                ChatSpace.campaign_slug == campaign_slug,
                func.lower(ChatSpace.creator_username) == creator_username.lower(),
            )
            .order_by(
                (ChatSpace.status == "archived"),  # active/approved sort before archived
                ChatSpace.last_message_at.desc().nullslast(),
                ChatSpace.created_at.desc(),
            )
            .first()
        )
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


def _decode_event(raw: Optional[str]) -> Optional[dict]:
    """Parse a stored event payload; never raise on a malformed blob."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def post_message(
    *,
    chat_space_id: int,
    sender_party: str,
    sender_identifier: Optional[str],
    sender_display_name: Optional[str],
    body: str,
    publish: bool = True,
    allow_empty: bool = False,
    kind: str = KIND_TEXT,
    event: Optional[dict] = None,
    review_id: Optional[int] = None,
) -> Optional[ChatMessage]:
    """
    Persist a new message. Set `publish=False` if the caller is about to
    attach a file and wants to broadcast the complete message (body +
    attachments) via `publish_message(msg.id)` once the attachment row is
    written; that way SSE subscribers see one event with everything.

    `allow_empty=True` lets the caller post an image-only message (no
    text body). Without this guard, image uploads with no caption would
    be rejected with `None`.

    `kind`/`event`/`review_id` write an event row (a review submission, an
    approval) that the chat UI renders as a widget. Event rows always allow
    an empty body — the widget carries the content, and `body` is only the
    plain-text fallback used by transcripts and previews.
    """
    body = (body or "").strip()
    if len(body) > _MAX_BODY:
        body = body[:_MAX_BODY]
    if kind != KIND_TEXT:
        allow_empty = True
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
            kind=kind,
            event_json=json.dumps(event) if event else None,
            review_id=review_id,
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
            "kind": msg.kind or KIND_TEXT,
            "event": _decode_event(msg.event_json),
            "created_at": msg.created_at.replace(tzinfo=timezone.utc).isoformat()
            if msg.created_at else None,
            "reactions": reactions,
            "attachments": attachments,
        }
    finally:
        db.close()
    _pubsub_publish(chat_space_id, "message", payload)


def edit_message(*, message_id: int, chat_space_id: int, new_body: str) -> bool:
    """
    Silently replace a message's body (an admin correction).

    Deliberately quiet: no out-of-band notification, no "edited" marker, and
    no `last_message_at` bump — it just persists the new text and broadcasts an
    `edit` event so any open client swaps the text in place. Returns False if
    the message doesn't exist, doesn't belong to the space, or the new body is
    empty.
    """
    new_body = (new_body or "").strip()
    if not new_body:
        return False
    if len(new_body) > _MAX_BODY:
        new_body = new_body[:_MAX_BODY]

    db = SessionLocal()
    try:
        msg = db.query(ChatMessage).get(message_id)
        if msg is None or msg.chat_space_id != chat_space_id:
            return False
        if msg.body == new_body:
            return True  # no-op, but treat as success
        msg.body = new_body
        db.commit()
    finally:
        db.close()

    _pubsub_publish(chat_space_id, "edit", {"message_id": message_id, "body": new_body})
    return True


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
                "kind": r.kind or KIND_TEXT,
                "event": _decode_event(r.event_json),
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
# Review events
#
# The chat space is campaign-long, so a new submission no longer announces
# itself by opening a new space. It posts an event row instead, which the
# chat UI renders as a card: the creator sees their draft land in the
# conversation, and the brand sees it arrive under the notes they left on
# the previous one.
# ---------------------------------------------------------------------------

def _submission_number(db, review: ReviewSubmission) -> int:
    """
    Which draft this is for the creator on this campaign (1-based).

    Counts the creator's own submissions on the same campaign + brand up to
    and including this one, so the card can say "Draft 2" without the
    webhook payload carrying a counter.
    """
    q = db.query(func.count(ReviewSubmission.id)).filter(
        func.lower(ReviewSubmission.creator_username)
        == (review.creator_username or "").lower(),
        ReviewSubmission.id <= review.id,
    )
    if review.campaign_slug:
        q = q.filter(ReviewSubmission.campaign_slug == review.campaign_slug)
    elif review.campaign_name:
        q = q.filter(ReviewSubmission.campaign_name == review.campaign_name)
    if review.brand_name:
        q = q.filter(ReviewSubmission.brand_name == review.brand_name)
    return int(q.scalar() or 1)


def _existing_event(db, *, chat_space_id: int, kind: str, review_id: int):
    """The most recent event of this kind already posted for this review."""
    return (
        db.query(ChatMessage)
        .filter(
            ChatMessage.chat_space_id == chat_space_id,
            ChatMessage.kind == kind,
            ChatMessage.review_id == review_id,
        )
        .order_by(ChatMessage.id.desc())
        .first()
    )


def post_review_submission_event(
    review_id: int, *, chat_space_id: Optional[int] = None
) -> Optional[ChatMessage]:
    """
    Drop the "new draft submitted" card into the campaign chat.

    Posted as the creator (it is their draft), so it renders on the
    creator's side of the conversation. Idempotent per review — webhook
    retries and repeated Request-Changes clicks won't stack duplicate
    cards. Returns the message, or None if there's nothing to post into.
    """
    space = (
        find_by_id(chat_space_id)
        if chat_space_id
        else get_or_create_for_review(review_id)
    )
    if space is None:
        return None

    db = SessionLocal()
    try:
        review = db.query(ReviewSubmission).get(review_id)
        if review is None:
            return None
        if _existing_event(
            db,
            chat_space_id=space.id,
            kind=KIND_REVIEW_SUBMISSION,
            review_id=review_id,
        ) is not None:
            return None
        number = _submission_number(db, review)
        event = {
            "review_id": review.id,
            "submission_number": number,
            "video_link": review.video_link or "",
            "notes": review.notes or "",
            "campaign_name": review.campaign_name or "",
            "brand_name": review.brand_name or "",
        }
        # Plain-text stand-in for anything that can't render the card
        # (transcripts, email previews). The creator's own notes stay in
        # `body` so they read as a normal message above the card.
        notes = (review.notes or "").strip()
    finally:
        db.close()

    return post_message(
        chat_space_id=space.id,
        sender_party="creator",
        sender_identifier=creator_identifier_for(space),
        sender_display_name=space.creator_username,
        body=notes,
        kind=KIND_REVIEW_SUBMISSION,
        event=event,
        review_id=review_id,
    )


def post_review_decision_event(
    *,
    review_id: int,
    decision: str,
    actor_name: Optional[str] = None,
    chat_space_id: Optional[int] = None,
) -> Optional[ChatMessage]:
    """
    Record an approve / request-changes decision in the chat as a system
    notice, so the conversation shows what happened to each draft without
    the space having to close. Idempotent per (review, decision).
    """
    space = (
        find_by_id(chat_space_id)
        if chat_space_id
        else find_space_for_review(review_id)
    )
    if space is None:
        return None

    db = SessionLocal()
    try:
        review = db.query(ReviewSubmission).get(review_id)
        if review is None:
            return None
        existing = _existing_event(
            db,
            chat_space_id=space.id,
            kind=KIND_REVIEW_DECISION,
            review_id=review_id,
        )
        # A brand can request changes more than once on the same draft; only
        # the first notice is worth posting. An approval that follows a
        # changes-requested notice is new information, so it still posts.
        if existing is not None:
            prior = _decode_event(existing.event_json) or {}
            if prior.get("decision") == decision:
                return None
        number = _submission_number(db, review)
    finally:
        db.close()

    label = "approved" if decision == "approved" else "changes requested"
    event = {
        "review_id": review_id,
        "submission_number": number,
        "decision": decision,
        "actor_name": actor_name or "",
    }
    return post_message(
        chat_space_id=space.id,
        sender_party="system",
        sender_identifier="system",
        sender_display_name="INFLUENCE",
        body=f"Draft {number} {label}",
        kind=KIND_REVIEW_DECISION,
        event=event,
        review_id=review_id,
    )


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


def draft_link_for_message(*, chat_space_id: int, message_id: int) -> Optional[str]:
    """The video link carried by a draft card, or None.

    Scoped to the space on purpose: the link-preview route resolves the URL
    from here rather than trusting one supplied by the caller, so the server
    only ever fetches links a creator actually submitted to a chat the
    requester can already read.
    """
    db = SessionLocal()
    try:
        msg = (
            db.query(ChatMessage)
            .filter(
                ChatMessage.id == message_id,
                ChatMessage.chat_space_id == chat_space_id,
                ChatMessage.kind == KIND_REVIEW_SUBMISSION,
            )
            .first()
        )
        if msg is None:
            return None
        event = _decode_event(msg.event_json) or {}
    finally:
        db.close()
    link = (event.get("video_link") or "").strip()
    return link or None


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


def close_for_approval(chat_space_id: int) -> bool:
    """
    Mark a chat space as 'approved' — closes it for brand + creator
    (sessions revoked, magic-link re-entry blocked, no new messages)
    while leaving it fully readable from the admin dashboard. The space
    is only fully archived once the parent campaign ends.

    No longer part of the approval flow: a chat space now runs for the
    whole campaign, and approving a draft posts a decision event into it
    instead of closing it (see `post_review_decision_event`). Kept as the
    manual "close this one early" lever, and because rows closed by the
    old flow still carry this status — `get_or_create_for_review` reopens
    them when the next draft arrives.
    """
    db = SessionLocal()
    try:
        space = db.query(ChatSpace).get(chat_space_id)
        if space is None or space.status != "active":
            return False
        space.status = "approved"
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
        # Archive both still-open chat spaces ("active") and ones already
        # closed by an approval ("approved") so the admin record gets
        # marked read-only once the campaign formally ends.
        q = db.query(ChatSpace).filter(ChatSpace.status != "archived")
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
        kind = m.get("kind") or KIND_TEXT
        event = m.get("event") or {}
        if kind == KIND_REVIEW_SUBMISSION:
            number = event.get("submission_number") or "?"
            lines.append(f"**Draft {number} submitted** · {when}")
            if event.get("video_link"):
                lines.append(f"> 🎬 {event['video_link']}")
            body = (m.get("body") or "").strip()
            if body:
                for ln in body.splitlines():
                    lines.append(f"> {ln}")
            lines.append("")
            continue
        if kind == KIND_REVIEW_DECISION:
            actor = event.get("actor_name") or ""
            suffix = f" by {actor}" if actor else ""
            lines.append(f"**{(m.get('body') or 'Decision').strip()}**{suffix} · {when}")
            lines.append("")
            continue
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

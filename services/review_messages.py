"""
Rebuilding the Slack messages a review lives in.

Each review is posted twice: the admin copy in the INFLUENCE workspace's
#content-reviews channel (coordinates stored on the ReviewSubmission row),
and the brand's copy in their own workspace (coordinates stored on the chat
space, since that's what tracks the brand install). Both are rendered by
`build_review_submitted_blocks`, so both can be rebuilt from the stored row
whenever the review's state changes — or when a button has to be added to
messages that were posted before that button existed.

Rebuilding replaces the message wholesale, so it's only safe while the
review is still live (no decision footer appended yet). Callers that need a
footer pass it in and get it re-appended.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from slack_sdk import WebClient

from config import Config
from models.models import ReviewSubmission, SessionLocal, SlackInstallation
from services import chat_service
from templates.slack_blocks import build_review_submitted_blocks

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewCard:
    """The stored fields a review's Slack message is rendered from."""

    review_id: int
    creator_username: str
    campaign_name: str
    brand_name: str
    video_link: str
    notes: str
    slack_channel: str | None
    slack_ts: str | None


def load_review_card(review_id: int) -> ReviewCard | None:
    """Snapshot a review row, detached from the session."""
    db = SessionLocal()
    try:
        row = db.query(ReviewSubmission).get(review_id)
        if row is None:
            return None
        return ReviewCard(
            review_id=row.id,
            creator_username=row.creator_username or "",
            campaign_name=row.campaign_name or "",
            brand_name=row.brand_name or "",
            video_link=row.video_link or "",
            notes=row.notes or "",
            slack_channel=row.slack_channel,
            slack_ts=row.slack_ts,
        )
    finally:
        db.close()


def admin_chat_url(review_id: int) -> str | None:
    """`/admin/chats/<id>` for this review's chat space, when resolvable."""
    if not Config.PUBLIC_BASE_URL:
        return None
    space = chat_service.find_space_for_review(review_id)
    if space is None:
        return None
    return f"{Config.PUBLIC_BASE_URL.rstrip('/')}/admin/chats/{space.id}"


def brand_chat_url(review_id: int) -> str | None:
    """Fresh brand magic link into this review's chat space, when resolvable."""
    if not Config.PUBLIC_BASE_URL:
        return None
    space = chat_service.find_space_for_review(review_id)
    if space is None:
        return None
    from utils.chat_tokens import make_invite_token

    token = make_invite_token(chat_space_id=space.id, party="brand")
    return f"{Config.PUBLIC_BASE_URL.rstrip('/')}/chat/invite/{token}"


def build_admin_blocks(card: ReviewCard) -> list[dict]:
    """The #content-reviews rendering of a review, Ignore button included."""
    return build_review_submitted_blocks(
        creator_username=card.creator_username,
        campaign_name=card.campaign_name,
        brand_name=card.brand_name,
        video_link=card.video_link,
        notes=card.notes,
        review_id=card.review_id,
        show_meta=True,
        admin_chat_url=admin_chat_url(card.review_id),
        include_ignore=True,
    )


def build_brand_blocks(card: ReviewCard, *, with_buttons: bool = True) -> list[dict]:
    """The brand-workspace rendering of a review (no Ignore button, ever)."""
    return build_review_submitted_blocks(
        creator_username=card.creator_username,
        campaign_name=card.campaign_name,
        brand_name=card.brand_name,
        video_link=card.video_link,
        notes=card.notes,
        review_id=card.review_id if with_buttons else None,
        show_meta=False,
        chat_url=brand_chat_url(card.review_id) if with_buttons else None,
    )


def rewrite_admin_review_message(
    card: ReviewCard,
    *,
    blocks: list[dict],
    text: str,
) -> bool:
    """
    Replace the #content-reviews copy of a review, addressed by the Slack
    coordinates stored on the row. Uses the home-workspace token — the admin
    channel always lives there. Best-effort; never raises.
    """
    if not Config.SLACK_BOT_TOKEN or not card.slack_channel or not card.slack_ts:
        return False
    try:
        WebClient(token=Config.SLACK_BOT_TOKEN).chat_update(
            channel=card.slack_channel,
            ts=card.slack_ts,
            text=text,
            blocks=blocks,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Could not update admin review message (review_id=%s): %s",
            card.review_id, exc,
        )
        return False


def rewrite_brand_review_message(
    review_id: int,
    *,
    with_buttons: bool,
    footer: dict | None = None,
    text: str,
) -> bool:
    """
    Replace the brand's own copy of a review — used when something decided on
    our side has to be reflected in their workspace (an approval by the
    INFLUENCE team, an ignore, an undo).

    `with_buttons=False` rebuilds the card without Approve / Request Changes
    so the brand can't act on a review that's already settled. Best-effort;
    never raises. Returns False when there's nothing to update.

    `find_by_review_id` (not `find_space_for_review`) on purpose:
    `brand_slack_ts` tracks the newest review's brand-workspace post, so only
    the newest review may rewrite it — otherwise an older draft's outcome
    would land on a later draft's message.
    """
    space = chat_service.find_by_review_id(review_id)
    if space is None or not space.brand_slack_ts or not space.brand_slack_channel:
        return False
    if not space.brand_install_id:
        return False

    card = load_review_card(review_id)
    if card is None:
        return False

    db = SessionLocal()
    try:
        install = db.query(SlackInstallation).get(space.brand_install_id)
        bot_token = install.bot_token if install is not None else None
    finally:
        db.close()
    if not bot_token:
        return False

    blocks = build_brand_blocks(card, with_buttons=with_buttons)
    if footer:
        blocks.append(footer)

    try:
        WebClient(token=bot_token).chat_update(
            channel=space.brand_slack_channel,
            ts=space.brand_slack_ts,
            text=text,
            blocks=blocks,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Could not update brand review message (review_id=%s): %s",
            review_id, exc,
        )
        return False

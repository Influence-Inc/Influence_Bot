"""
Shared review-approval workflow.

Used by both:
- the brand-clicked "Approve" Slack button (bot/actions.handle_review_approve)
- the 24h auto-approval sweep (run_auto_approval_sweep)

`approve_review_core` is intentionally idempotent: it short-circuits when
the review is already approved so concurrent triggers (a button click
landing the same minute the sweep runs) don't double-email or
double-post the admin notification.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import Config
from models.models import (
    ChatMessage,
    ChatSpace,
    ReviewSubmission,
    SessionLocal,
)
from services import chat_service
from services.email_service import EmailService
from services.reelstats_api import ReelStatsAPI
from templates.email_templates import video_approved
from templates.slack_blocks import build_review_approved_blocks

logger = logging.getLogger(__name__)

_email_service = EmailService()


def _fetch_submit_posts_url(
    campaign_slug: str, creator_username: str
) -> str | None:
    """
    Look up ``campaigns[].creators[].submissionLinks.submitPostsUrl`` from
    the live ReelStats API for this (creator, campaign).

    The URL is unique per creator-per-campaign and only lives in the
    ReelStats API response — the ``review_submitted`` webhook payload
    doesn't carry ``submissionLinks``, so the cached column on
    ReviewSubmission is almost always NULL. The fix is to ask the API
    at approval time.

    Returns None on any failure (missing slug/username, network error,
    no campaign match, no creator match, missing field). The caller
    falls back to whatever (if anything) is cached on the row.
    """
    if not campaign_slug or not creator_username:
        return None
    try:
        campaigns = ReelStatsAPI().get_campaigns()
    except Exception as exc:
        logger.warning("submitPostsUrl lookup: ReelStats API failed: %s", exc)
        return None
    target_user = creator_username.lower().lstrip("@")
    for campaign in campaigns:
        if campaign.get("slug") != campaign_slug:
            continue
        for creator in campaign.get("creators", []):
            uname = (creator.get("username") or "").lower().lstrip("@")
            if uname != target_user:
                continue
            links = creator.get("submissionLinks") or {}
            return links.get("submitPostsUrl") or None
    return None

AUTO_APPROVE_AFTER = timedelta(hours=24)
AUTO_APPROVE_ACTOR_ID = "system"
AUTO_APPROVE_ACTOR_NO_ACTION = "Auto-approval (no action in 24h)"
AUTO_APPROVE_ACTOR_NO_CHAT = "Auto-approval (no chat activity in 24h)"


def approve_review_core(
    *,
    review_id: int,
    actor_id: str,
    actor_name: str,
) -> bool:
    """
    Mark a review approved + run all approval side effects:
      - flip decision/decided_at/decided_by on the ReviewSubmission row
      - email the creator the video_approved template
      - close the per-review chat space (revoke brand+creator sessions)
      - post a fresh approval notification to admin #content-reviews

    Returns True if this call was the one that approved the review;
    False if it was already approved (no-op) or the review couldn't be
    found. The Slack `chat_update` on the original review-submitted
    message is handled by the action handler (it needs the click's
    channel/ts), not here.
    """
    db = SessionLocal()
    try:
        row = db.query(ReviewSubmission).get(review_id)
        if row is None:
            logger.warning("approve_review_core: review_id=%s not found", review_id)
            return False
        if row.decision == "approved":
            return False
        row.decision = "approved"
        row.decided_by_id = actor_id
        row.decided_by_name = actor_name
        row.decided_at = datetime.now(timezone.utc)
        db.commit()

        creator_email = row.creator_email
        creator_username = row.creator_username
        brand_name = row.brand_name or ""
        campaign_name = row.campaign_name or ""
        campaign_slug = row.campaign_slug or ""
        video_link = row.video_link or ""
        submit_posts_url = row.submit_posts_url or None
    finally:
        db.close()

    # The webhook payload doesn't carry submissionLinks, so the cached
    # column is almost always NULL. Fetch the live URL from the
    # ReelStats API so the approval email actually includes it.
    if not submit_posts_url:
        submit_posts_url = _fetch_submit_posts_url(
            campaign_slug, creator_username
        )

    if creator_email:
        template = video_approved(
            creator_name=creator_username,
            brand_name=brand_name,
            submit_posts_url=submit_posts_url,
        )
        try:
            _email_service.send_email(
                creator_email,
                template["subject"],
                template["body"],
            )
        except Exception as exc:
            logger.warning("approval email send failed: %s", exc)

    try:
        space = chat_service.find_by_review_id(review_id)
        if space is not None and space.status == "active":
            chat_service.close_for_approval(space.id)
    except Exception as exc:
        logger.warning(
            "Could not close chat space for review %s on approval: %s",
            review_id, exc,
        )

    _post_admin_approval_notification(
        creator_username=creator_username,
        campaign_name=campaign_name,
        brand_name=brand_name,
        video_link=video_link,
        actor_name=actor_name,
    )

    logger.info(
        "Review approved: review_id=%s creator=@%s actor=%s",
        review_id, creator_username, actor_name,
    )
    return True


def _post_admin_approval_notification(
    *,
    creator_username: str,
    campaign_name: str,
    brand_name: str,
    video_link: str,
    actor_name: str,
) -> None:
    """Post the approval recap to admin #content-reviews via the home token."""
    if not Config.SLACK_BOT_TOKEN or not Config.SLACK_CHANNEL_REVIEWS:
        return
    try:
        blocks = build_review_approved_blocks(
            creator_username=creator_username,
            campaign_name=campaign_name,
            brand_name=brand_name,
            video_link=video_link,
            actor_name=actor_name,
        )
        WebClient(token=Config.SLACK_BOT_TOKEN).chat_postMessage(
            channel=Config.SLACK_CHANNEL_REVIEWS,
            text=f"Review approved for @{creator_username}",
            blocks=blocks,
        )
    except SlackApiError as exc:
        err = exc.response.get("error") if exc.response else str(exc)
        logger.warning("Admin approval notification failed: %s", err)
    except Exception as exc:
        logger.warning("Admin approval notification failed: %s", exc)


# ---------------------------------------------------------------------------
# Auto-approval sweep
# ---------------------------------------------------------------------------

def run_auto_approval_sweep() -> int:
    """
    Auto-approve reviews where the brand has gone silent for 24h.

    Two cases:
    1. No decision at all 24h after the review was submitted (brand
       ignored the Slack message entirely).
    2. Decision is `changes_requested` and 24h have passed since that
       click, but the chat space still has zero messages (brand opened
       the chat and then went silent).

    Returns the number of reviews auto-approved on this sweep.
    """
    cutoff = datetime.now(timezone.utc) - AUTO_APPROVE_AFTER
    stale_ids: list[tuple[int, str]] = []  # (review_id, actor_name)

    db = SessionLocal()
    try:
        no_action_rows = (
            db.query(ReviewSubmission.id)
            .filter(
                ReviewSubmission.decision.is_(None),
                ReviewSubmission.submitted_at.isnot(None),
                ReviewSubmission.submitted_at <= cutoff,
            )
            .all()
        )
        for (rid,) in no_action_rows:
            stale_ids.append((rid, AUTO_APPROVE_ACTOR_NO_ACTION))

        # Changes-requested + no chat activity 24h after the click. Join
        # via ChatSpace.latest_review_id (per-review chat space mapping).
        no_chat_rows = (
            db.query(ReviewSubmission.id, ChatSpace.id)
            .join(ChatSpace, ChatSpace.latest_review_id == ReviewSubmission.id)
            .filter(
                ReviewSubmission.decision == "changes_requested",
                ReviewSubmission.decided_at.isnot(None),
                ReviewSubmission.decided_at <= cutoff,
                ChatSpace.status == "active",
            )
            .all()
        )
        for review_id, chat_space_id in no_chat_rows:
            has_messages = (
                db.query(ChatMessage.id)
                .filter(ChatMessage.chat_space_id == chat_space_id)
                .first()
                is not None
            )
            if not has_messages:
                stale_ids.append((review_id, AUTO_APPROVE_ACTOR_NO_CHAT))
    finally:
        db.close()

    if not stale_ids:
        return 0

    approved = 0
    for review_id, actor_name in stale_ids:
        try:
            if approve_review_core(
                review_id=review_id,
                actor_id=AUTO_APPROVE_ACTOR_ID,
                actor_name=actor_name,
            ):
                approved += 1
        except Exception as exc:
            logger.warning(
                "Auto-approval failed for review_id=%s: %s", review_id, exc,
            )

    logger.info(
        "Auto-approval sweep: %d candidate(s), %d newly approved",
        len(stale_ids), approved,
    )
    return approved

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
      - post an approval notice into the campaign chat space
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
        review_slack_channel = row.slack_channel
        review_slack_ts = row.slack_ts
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

    # The chat space runs for the whole campaign, so an approval doesn't
    # close it — it posts a notice into the conversation. The creator's next
    # draft lands in the same thread, under the notes that produced it.
    try:
        chat_service.post_review_decision_event(
            review_id=review_id,
            decision="approved",
            actor_name=actor_name,
        )
    except Exception as exc:
        logger.warning(
            "Could not post approval notice into chat for review %s: %s",
            review_id, exc,
        )

    _post_admin_approval_notification(
        creator_username=creator_username,
        campaign_name=campaign_name,
        brand_name=brand_name,
        video_link=video_link,
        actor_name=actor_name,
        review_slack_channel=review_slack_channel,
        review_slack_ts=review_slack_ts,
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
    review_slack_channel: str | None = None,
    review_slack_ts: str | None = None,
) -> None:
    """Post the approval recap to admin #content-reviews via the home token.

    Threads under the original review-submitted message when its Slack
    coordinates are known, so approvals show up as a reply inside the
    "content to be reviewed" thread rather than as a fresh top-level post.
    """
    if not Config.SLACK_BOT_TOKEN:
        return
    channel = review_slack_channel or Config.SLACK_CHANNEL_REVIEWS
    if not channel:
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
            channel=channel,
            text=f"Review approved for @{creator_username}",
            blocks=blocks,
            thread_ts=review_slack_ts or None,
        )
    except SlackApiError as exc:
        err = exc.response.get("error") if exc.response else str(exc)
        logger.warning("Admin approval notification failed: %s", err)
    except Exception as exc:
        logger.warning("Admin approval notification failed: %s", exc)


# ---------------------------------------------------------------------------
# Auto-approval sweep
# ---------------------------------------------------------------------------

def _as_naive_utc(value: datetime | None) -> datetime | None:
    """Drop the tzinfo from an aware datetime so it compares against the
    naive UTC values SQLAlchemy reads back out of the DateTime columns."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _last_conversation_at(db, chat_space_id: int) -> datetime | None:
    """
    When someone last *said* something in a chat space.

    Only counts messages a person typed. The submission card and decision
    notices are event rows the bot writes itself — counting those would
    stop the silence clock the moment a draft arrives and nothing would
    ever auto-approve again.
    """
    row = (
        db.query(ChatMessage.created_at)
        .filter(
            ChatMessage.chat_space_id == chat_space_id,
            ChatMessage.kind == chat_service.KIND_TEXT,
        )
        .order_by(ChatMessage.created_at.desc())
        .first()
    )
    return row[0] if row else None


def _is_silent_since(db, chat_space_id: int | None, since: datetime | None) -> bool:
    """True when nobody has spoken in the space since `since`."""
    if chat_space_id is None:
        return True
    last = _as_naive_utc(_last_conversation_at(db, chat_space_id))
    if last is None:
        return True
    since = _as_naive_utc(since)
    if since is None:
        return False
    return last < since


def run_auto_approval_sweep() -> int:
    """
    Auto-approve reviews where the brand has gone silent for 24h.

    Two cases:
    1. No decision at all 24h after the review was submitted (brand
       ignored the Slack message entirely) AND nobody has spoken in the
       chat since that draft was submitted. The chat space is opened at
       review-post time, so anyone (creator/brand/admin) can post in it
       before the brand clicks a button; any such activity means the
       review is being worked, so it shouldn't be auto-approved on the
       silence clock.
    2. Decision is `changes_requested` and 24h have passed since that
       click, but nobody has spoken since (brand opened the chat and then
       went silent).

    Both windows are measured from *this* review's own timestamp, not
    from the start of the chat space: the space now spans the whole
    campaign, so conversation about an earlier draft must not keep a
    later one alive forever.

    Returns the number of reviews auto-approved on this sweep.
    """
    cutoff = datetime.now(timezone.utc) - AUTO_APPROVE_AFTER
    stale_ids: list[tuple[int, str]] = []  # (review_id, actor_name)

    db = SessionLocal()
    try:
        no_action_rows = (
            db.query(ReviewSubmission.id, ReviewSubmission.submitted_at)
            .filter(
                ReviewSubmission.decision.is_(None),
                ReviewSubmission.submitted_at.isnot(None),
                ReviewSubmission.submitted_at <= cutoff,
            )
            .all()
        )
        changes_rows = (
            db.query(ReviewSubmission.id, ReviewSubmission.decided_at)
            .filter(
                ReviewSubmission.decision == "changes_requested",
                ReviewSubmission.decided_at.isnot(None),
                ReviewSubmission.decided_at <= cutoff,
            )
            .all()
        )
    finally:
        db.close()

    candidates = [
        (rid, since, AUTO_APPROVE_ACTOR_NO_ACTION) for rid, since in no_action_rows
    ] + [
        (rid, since, AUTO_APPROVE_ACTOR_NO_CHAT) for rid, since in changes_rows
    ]
    if not candidates:
        return 0

    # Resolve each candidate's chat space first (each lookup opens its own
    # session), then run the silence checks together on one session.
    resolved: list[tuple[int, datetime | None, str, int | None]] = []
    for review_id, since, actor_name in candidates:
        space = chat_service.find_space_for_review(review_id)
        # A changes-requested review with no reachable chat space has
        # nowhere for the brand to have replied; leave it alone rather
        # than approving on the strength of a missing record.
        if space is None and actor_name == AUTO_APPROVE_ACTOR_NO_CHAT:
            continue
        if space is not None and space.status == "archived":
            continue
        resolved.append((review_id, since, actor_name, space.id if space else None))

    db = SessionLocal()
    try:
        for review_id, since, actor_name, space_id in resolved:
            if _is_silent_since(db, space_id, since):
                stale_ids.append((review_id, actor_name))
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

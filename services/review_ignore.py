"""
"Ignore" — take a review out of play without deciding on it.

Creators sometimes send a link that was never meant for review: the wrong
URL, a duplicate of a draft already submitted, a test. Left alone, that
submission behaves like any other:

* the 24h sweep auto-approves it and emails the creator "your video is
  approved" for something nobody looked at (services/review_approval.py), and
* it counts as a video in review, which suppresses the creator's deadline
  reminder emails (services/review_coverage.py).

The Ignore button — INFLUENCE workspace only, since it's our call to make —
sets `ReviewSubmission.ignored`, which switches both of those off. It is not
a decision: `decision` keeps whatever it held (usually NULL, sometimes
`changes_requested`), so "Undo ignore" restores the review exactly as it was.

Restoring does not restart the 24h clock. That's deliberate: the sweep's rule
is "the brand has been silent since this draft landed", and an ignore we've
taken back doesn't change how long they've been silent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from models.models import ReviewSubmission, SessionLocal
from services import review_messages

logger = logging.getLogger(__name__)

# Outcomes of an ignore/restore attempt, so callers can phrase their own
# Slack replies without re-reading the row.
IGNORED = "ignored"
ALREADY_IGNORED = "already_ignored"
ALREADY_APPROVED = "already_approved"
RESTORED = "restored"
NOT_IGNORED = "not_ignored"
NOT_FOUND = "not_found"

# How far back the startup backfill reaches when adding the Ignore button to
# review messages posted before the button existed. Anything older than this
# is settled one way or another in practice, and rewriting it would only
# resurface stale posts.
BACKFILL_MAX_AGE_DAYS = 30


def ignore_review(*, review_id: int, actor_id: str, actor_name: str) -> str:
    """
    Flag a review as ignored. Returns one of IGNORED / ALREADY_IGNORED /
    ALREADY_APPROVED / NOT_FOUND.

    An approved review can't be ignored: its approval email has already gone
    out, so there's nothing left to hold back.
    """
    db = SessionLocal()
    try:
        row = db.query(ReviewSubmission).get(review_id)
        if row is None:
            logger.warning("ignore_review: review_id=%s not found", review_id)
            return NOT_FOUND
        if row.ignored:
            return ALREADY_IGNORED
        if row.decision == "approved":
            return ALREADY_APPROVED
        row.ignored = True
        row.ignored_at = datetime.now(timezone.utc)
        row.ignored_by_id = actor_id or None
        row.ignored_by_name = actor_name or None
        db.commit()
        creator_username = row.creator_username
    finally:
        db.close()

    logger.info(
        "Review ignored: review_id=%s creator=@%s actor=%s",
        review_id, creator_username, actor_name,
    )
    return IGNORED


def restore_review(*, review_id: int) -> str:
    """
    Clear the ignore flag, putting the review back in play with whatever
    decision it already carried. Returns RESTORED / NOT_IGNORED / NOT_FOUND.
    """
    db = SessionLocal()
    try:
        row = db.query(ReviewSubmission).get(review_id)
        if row is None:
            logger.warning("restore_review: review_id=%s not found", review_id)
            return NOT_FOUND
        if not row.ignored:
            return NOT_IGNORED
        row.ignored = False
        row.ignored_at = None
        row.ignored_by_id = None
        row.ignored_by_name = None
        db.commit()
        creator_username = row.creator_username
    finally:
        db.close()

    logger.info(
        "Review restored from ignored: review_id=%s creator=@%s",
        review_id, creator_username,
    )
    return RESTORED


# ---------------------------------------------------------------------------
# Backfill: put the Ignore button on review messages posted before it existed
# ---------------------------------------------------------------------------

def pending_review_ids(*, max_age_days: int = BACKFILL_MAX_AGE_DAYS) -> list[int]:
    """
    Reviews whose #content-reviews message still carries live buttons: not
    approved, not already ignored, and posted to Slack. `changes_requested`
    reviews are included — their admin copy is untouched by that click (it
    lands on the brand's copy), and they're still on the sweep's clock, so
    ignoring them is just as meaningful.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    db = SessionLocal()
    try:
        rows = (
            db.query(ReviewSubmission.id)
            .filter(
                ReviewSubmission.ignored.isnot(True),
                ReviewSubmission.slack_ts.isnot(None),
                ReviewSubmission.slack_channel.isnot(None),
                ReviewSubmission.submitted_at.isnot(None),
                ReviewSubmission.submitted_at >= cutoff,
                (ReviewSubmission.decision.is_(None))
                | (ReviewSubmission.decision == "changes_requested"),
            )
            .order_by(ReviewSubmission.id)
            .all()
        )
        return [row_id for (row_id,) in rows]
    finally:
        db.close()


def backfill_ignore_buttons(*, max_age_days: int = BACKFILL_MAX_AGE_DAYS) -> int:
    """
    Re-render every still-pending review message in #content-reviews so the
    Ignore button appears on submissions posted before this feature shipped.

    Rebuilding is safe here precisely because these messages are still
    pending: they are exactly what `build_review_submitted_blocks` produced,
    with no decision footer to preserve. Idempotent — a message that already
    has the button is rewritten to the same blocks — so re-running it on every
    boot also picks up anything a previous run couldn't reach.

    Returns the number of messages successfully updated.
    """
    review_ids = pending_review_ids(max_age_days=max_age_days)
    if not review_ids:
        return 0

    updated = 0
    for review_id in review_ids:
        card = review_messages.load_review_card(review_id)
        if card is None:
            continue
        try:
            ok = review_messages.rewrite_admin_review_message(
                card,
                blocks=review_messages.build_admin_blocks(card),
                text=(
                    f"New review submitted by @{card.creator_username} "
                    f"for {card.campaign_name}"
                ),
            )
        except Exception as exc:
            logger.warning(
                "Ignore-button backfill failed for review_id=%s: %s",
                review_id, exc,
            )
            continue
        if ok:
            updated += 1

    logger.info(
        "Ignore-button backfill: %d pending review message(s), %d updated",
        len(review_ids), updated,
    )
    return updated

"""
Do the videos a creator has already shared for review cover what they
still owe?

Used to suppress deadline reminder *emails*. Nagging a creator who has
already handed everything over is wrong — the ball is in the brand's
court, not theirs. The internal Slack alert still fires (with a note) so
the team can chase the review instead of the creator.

Two cases make this more than a boolean:

1. **View-based deliverables.** Views only accrue after a post goes live,
   so a video sitting in review can never *prove* a view target will be
   met. What it does tell us is that the shortfall isn't the creator's to
   act on right now — they can't post until the brand approves. So an
   unmet view target needs at least one video still in the pipeline for
   the email to be suppressed; once everything shared has been posted and
   the views are still short, the reminder goes out again.

2. **Several videos owed, several videos shared.** Coverage is counted,
   not boolean: a creator who still owes 2 videos and has shared 1 gets
   the email. Reviews that already turned into posted videos are netted
   out (`shared - posted`), so an approved-and-posted draft isn't counted
   a second time against the videos that are still outstanding.

A draft the brand sent back with "Request Changes" is *not* in play — the
ball is back with the creator, so a reminder is legitimate. That decision
state only exists in our own `review_submissions` table, while the
ReelStats `creator.reviews` array is the durable list of what was shared.
Both sources are unioned by video link: the API list survives a DB wipe
(SQLite on Railway is ephemeral), and the stored rows cover payloads that
don't carry `reviews` — the `deadline_check` webhook sends a trimmed
creator object.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func

from models.models import ReviewSubmission, SessionLocal

logger = logging.getLogger(__name__)

CHANGES_REQUESTED = "changes_requested"


@dataclass(frozen=True)
class ReviewCoverage:
    """Verdict for one creator on one campaign."""

    # True when the videos in review cover everything still outstanding —
    # i.e. don't email this creator a deadline reminder.
    covered: bool
    # Distinct videos shared for review that aren't awaiting creator changes.
    shared_for_review: int
    # Of those, the ones not yet posted (`shared_for_review - posted`).
    pending: int
    # Videos still owed against `deliverables.minVideos`.
    videos_needed: int
    # A `deliverables.minViews` target that hasn't been reached yet.
    views_gap: bool

    @property
    def summary(self) -> str:
        """One-line explanation, for the Slack note and the log line."""
        shared = f"{self.pending} video(s) in review"
        if self.videos_needed and self.views_gap:
            return (
                f"{shared} cover the {self.videos_needed} still owed; "
                f"view target pending those posts"
            )
        if self.videos_needed:
            return f"{shared} cover the {self.videos_needed} still owed"
        if self.views_gap:
            return f"{shared} not posted yet; view target pending those posts"
        return f"{shared}; nothing outstanding"


def review_coverage(creator: dict) -> ReviewCoverage:
    """Decide whether `creator`'s in-review videos cover their remaining work."""
    deliverables = creator.get("deliverables") or {}

    posted = _posted_video_count(creator, deliverables)
    min_videos = _as_int(deliverables.get("minVideos"))
    min_views = _as_int(deliverables.get("minViews"))

    videos_needed = max(min_videos - posted, 0) if min_videos else 0
    views_gap = _has_views_gap(creator, deliverables)

    shared = _in_play_review_count(creator)
    # Reviews that already became posted videos have done their job; only
    # the ones still in the pipeline can cover what's left.
    pending = max(shared - posted, 0)

    required = videos_needed
    if views_gap or (not min_videos and min_views is None):
        # An unmet view target can't be *completed* by a video in review,
        # but the creator can't act on it either while that video waits for
        # approval — so one video in the pipeline is enough to hold the
        # email. Same for a creator with no deliverable targets recorded at
        # all: "doing their part" can only mean something is in review.
        required = max(required, 1)

    return ReviewCoverage(
        covered=pending >= required,
        shared_for_review=shared,
        pending=pending,
        videos_needed=videos_needed,
        views_gap=views_gap,
    )


# ---------------------------------------------------------------------------
# Deliverable arithmetic
# ---------------------------------------------------------------------------

def _as_int(value) -> int | None:
    """Coerce an API number to int; None for null/garbage (and for bools)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _posted_video_count(creator: dict, deliverables: dict) -> int:
    """How many videos are live, preferring the API's own computed count."""
    for value in (deliverables.get("actualVideos"), creator.get("totalVideosPosted")):
        count = _as_int(value)
        if count is not None:
            return max(count, 0)
    return sum(
        1 for video in (creator.get("videos") or [])
        if video.get("hasLinks") or (video.get("links") or {})
    )


def _has_views_gap(creator: dict, deliverables: dict) -> bool:
    """True when a minViews target exists and hasn't been reached."""
    min_views = _as_int(deliverables.get("minViews"))
    if not min_views:
        return False
    complete = deliverables.get("viewsComplete")
    if isinstance(complete, bool):
        return not complete
    actual = _as_int(deliverables.get("actualViews"))
    if actual is None:
        actual = _as_int(creator.get("totalViews")) or 0
    return actual < min_views


# ---------------------------------------------------------------------------
# Shared-for-review videos
# ---------------------------------------------------------------------------

def _normalize_link(link) -> str:
    return (link or "").strip().rstrip("/").lower()


def _in_play_review_count(creator: dict) -> int:
    """
    Distinct videos this creator has shared for review that the brand
    hasn't sent back for changes.

    Keyed by video link so the same draft reported by both the ReelStats
    payload and our `review_submissions` table counts once. A submission
    with no link falls back to its own id, which can't be cross-matched —
    those are rare (the webhook logs a warning when `videoLink` is empty).
    """
    stored: dict[str, str | None] = {}
    for key, decision in _stored_reviews(creator):
        # Later rows win: a resubmission of the same link supersedes the
        # decision recorded on the earlier one.
        stored[key] = decision

    reviews: dict[str, str | None] = {}
    for index, review in enumerate(creator.get("reviews") or []):
        key = _normalize_link(review.get("videoLink"))
        if not key:
            key = f"api:{review.get('id') or index}"
        # Unknown to our table (DB wiped, or submitted before this bot
        # deploy) means undecided, which counts as in play.
        reviews[key] = stored.get(key)
    for key, decision in stored.items():
        reviews.setdefault(key, decision)

    return sum(1 for decision in reviews.values() if decision != CHANGES_REQUESTED)


def _stored_reviews(creator: dict) -> list[tuple[str, str | None]]:
    """`(link key, decision)` per stored review row; empty on any DB trouble."""
    username = (creator.get("username") or "").strip().lstrip("@")
    if not username:
        return []

    campaign_slug = (creator.get("campaign_slug") or "").strip()
    campaign_name = (creator.get("campaign_name") or "").strip()

    db = SessionLocal()
    try:
        query = db.query(ReviewSubmission).filter(
            func.lower(ReviewSubmission.creator_username) == username.lower()
        )
        # Scope to this campaign so a creator's work for another brand
        # doesn't silence this campaign's reminder.
        if campaign_slug:
            query = query.filter(ReviewSubmission.campaign_slug == campaign_slug)
        elif campaign_name:
            query = query.filter(ReviewSubmission.campaign_name == campaign_name)
        rows = query.order_by(ReviewSubmission.id).all()
        return [
            (_normalize_link(row.video_link) or f"db:{row.id}", row.decision)
            for row in rows
        ]
    except Exception as exc:
        # Never let a DB hiccup silence a reminder: no rows means no
        # suppression, which is the safe direction to fail in.
        logger.warning(
            "review coverage: could not read review_submissions for @%s: %s",
            username, exc,
        )
        return []
    finally:
        db.close()

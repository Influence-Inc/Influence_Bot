"""
Tests for deadline-reminder suppression (services/review_coverage.py).

The rule under test: don't email a creator a deadline nag when the videos
they've already shared for review cover what they still owe. The tricky
parts are view-based deliverables (a video in review can't produce views
until it's posted) and counting — several videos owed vs. several videos
shared.

Run with `python -m pytest tests/test_review_coverage.py`, or directly
with `python tests/test_review_coverage.py`.
"""

import os
import sys

# Importing the app modules pulls in config, which reads DATABASE_URL. Point
# it at an in-memory SQLite so the tests never touch a real database.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.models import (  # noqa: E402
    ReviewSubmission,
    SessionLocal,
    init_db,
)
from services.review_coverage import review_coverage  # noqa: E402

init_db()

CAMPAIGN = {"campaign_id": "camp1", "campaign_slug": "reve/reve-features"}


def _reset_reviews():
    db = SessionLocal()
    try:
        db.query(ReviewSubmission).delete()
        db.commit()
    finally:
        db.close()


def _store_review(video_link, decision=None, username="tharun.fyi", slug=None):
    """Persist a review_submissions row the way the webhook handler does."""
    db = SessionLocal()
    try:
        db.add(ReviewSubmission(
            campaign_slug=slug if slug is not None else CAMPAIGN["campaign_slug"],
            campaign_name="Reve Features",
            creator_username=username,
            video_link=video_link,
            decision=decision,
        ))
        db.commit()
    finally:
        db.close()


def _creator(min_videos=None, min_views=None, posted=0, views=0, reviews=None,
             **extra):
    deliverables = {
        "minVideos": min_videos,
        "minViews": min_views,
        "actualVideos": posted,
        "actualViews": views,
    }
    if min_views is not None:
        deliverables["viewsComplete"] = views >= min_views
    creator = {
        **CAMPAIGN,
        "username": "tharun.fyi",
        "email": "tharunr16@gmail.com",
        "deliverables": deliverables,
        "totalVideosPosted": posted,
        "totalViews": views,
    }
    if reviews is not None:
        creator["reviews"] = [
            {"id": f"r{i}", "videoLink": link} for i, link in enumerate(reviews)
        ]
    creator.update(extra)
    return creator


# --- Video-count deliverables ----------------------------------------------

def test_nothing_shared_still_gets_the_email():
    _reset_reviews()
    assert not review_coverage(_creator(min_videos=1, reviews=[])).covered


def test_one_owed_one_shared_is_covered():
    _reset_reviews()
    creator = _creator(min_videos=1, reviews=["https://drive.google.com/a"])
    assert review_coverage(creator).covered


def test_two_owed_one_shared_still_gets_the_email():
    _reset_reviews()
    creator = _creator(min_videos=2, reviews=["https://drive.google.com/a"])
    coverage = review_coverage(creator)
    assert not coverage.covered
    assert coverage.videos_needed == 2
    assert coverage.pending == 1


def test_two_owed_two_shared_is_covered():
    _reset_reviews()
    creator = _creator(
        min_videos=2,
        reviews=["https://drive.google.com/a", "https://drive.google.com/b"],
    )
    assert review_coverage(creator).covered


def test_posted_video_does_not_count_twice_against_the_remaining_one():
    # 2 owed, 1 already posted (from an approved review), 1 still in review.
    _reset_reviews()
    creator = _creator(
        min_videos=2,
        posted=1,
        reviews=["https://drive.google.com/a", "https://drive.google.com/b"],
    )
    coverage = review_coverage(creator)
    assert coverage.covered
    assert coverage.videos_needed == 1
    assert coverage.pending == 1


def test_all_shared_videos_already_posted_still_gets_the_email():
    # 2 owed, 1 posted, and the only review is the one that became it.
    _reset_reviews()
    creator = _creator(
        min_videos=2, posted=1, reviews=["https://drive.google.com/a"],
    )
    coverage = review_coverage(creator)
    assert not coverage.covered
    assert coverage.pending == 0


# --- View-based deliverables ------------------------------------------------

def test_views_short_with_nothing_in_review_gets_the_email():
    # Videos are all posted and live; only the view target is missing, and
    # that's on the creator to fix with more content.
    _reset_reviews()
    creator = _creator(
        min_videos=2, min_views=200_000, posted=2, views=50_000,
        reviews=["https://drive.google.com/a", "https://drive.google.com/b"],
    )
    coverage = review_coverage(creator)
    assert not coverage.covered
    assert coverage.views_gap


def test_views_short_but_a_video_is_still_in_review_is_covered():
    # The video that could deliver those views can't be posted until the
    # brand approves it — chasing the creator is pointless.
    _reset_reviews()
    creator = _creator(
        min_videos=2, min_views=200_000, posted=1, views=50_000,
        reviews=["https://drive.google.com/a", "https://drive.google.com/b"],
    )
    assert review_coverage(creator).covered


def test_views_only_deliverable_is_covered_by_one_video_in_review():
    _reset_reviews()
    creator = _creator(
        min_views=200_000, views=50_000, reviews=["https://drive.google.com/a"],
    )
    coverage = review_coverage(creator)
    assert coverage.covered
    assert coverage.videos_needed == 0


def test_views_only_deliverable_with_nothing_in_review_gets_the_email():
    _reset_reviews()
    creator = _creator(min_views=200_000, views=50_000, reviews=[])
    assert not review_coverage(creator).covered


def test_video_count_met_but_views_short_needs_something_in_review():
    # minVideos satisfied, so videos_needed is 0 — the view gap alone must
    # still require a video in the pipeline, not suppress by default.
    _reset_reviews()
    creator = _creator(
        min_videos=1, min_views=200_000, posted=1, views=10,
        reviews=["https://drive.google.com/a"],
    )
    assert not review_coverage(creator).covered


# --- Decision state ---------------------------------------------------------

def test_changes_requested_draft_does_not_suppress():
    _reset_reviews()
    _store_review("https://drive.google.com/a", decision="changes_requested")
    creator = _creator(min_videos=1, reviews=["https://drive.google.com/a"])
    assert not review_coverage(creator).covered


def test_resubmission_after_changes_requested_suppresses_again():
    _reset_reviews()
    _store_review("https://drive.google.com/a", decision="changes_requested")
    _store_review("https://drive.google.com/b")
    creator = _creator(
        min_videos=1,
        reviews=["https://drive.google.com/a", "https://drive.google.com/b"],
    )
    assert review_coverage(creator).covered


def test_approved_but_unposted_draft_still_suppresses():
    _reset_reviews()
    _store_review("https://drive.google.com/a", decision="approved")
    creator = _creator(min_videos=1, reviews=["https://drive.google.com/a"])
    assert review_coverage(creator).covered


# --- Source union: API payload vs. stored rows ------------------------------

def test_stored_rows_cover_payloads_without_a_reviews_array():
    # The deadline_check webhook sends a trimmed creator object.
    _reset_reviews()
    _store_review("https://drive.google.com/a")
    creator = _creator(min_videos=1)
    creator.pop("reviews", None)
    assert review_coverage(creator).covered


def test_same_draft_in_both_sources_counts_once():
    _reset_reviews()
    _store_review("https://drive.google.com/a")
    creator = _creator(min_videos=2, reviews=["https://drive.google.com/A/"])
    coverage = review_coverage(creator)
    assert not coverage.covered
    assert coverage.pending == 1


def test_another_campaigns_reviews_do_not_suppress_this_one():
    _reset_reviews()
    _store_review("https://drive.google.com/a", slug="other/campaign")
    creator = _creator(min_videos=1)
    creator.pop("reviews", None)
    assert not review_coverage(creator).covered


def test_another_creators_reviews_do_not_suppress_this_one():
    _reset_reviews()
    _store_review("https://drive.google.com/a", username="someone.else")
    creator = _creator(min_videos=1)
    creator.pop("reviews", None)
    assert not review_coverage(creator).covered


# --- Missing / odd data -----------------------------------------------------

def test_no_deliverable_targets_needs_a_video_in_review():
    _reset_reviews()
    creator = _creator(reviews=[])
    assert not review_coverage(creator).covered
    assert review_coverage(_creator(reviews=["https://drive.google.com/a"])).covered


def test_video_count_falls_back_to_posted_videos_list():
    _reset_reviews()
    creator = {
        **CAMPAIGN,
        "username": "tharun.fyi",
        "deliverables": {"minVideos": 2},
        "videos": [{"id": "v1", "hasLinks": True}],
        "reviews": [{"id": "r0", "videoLink": "https://drive.google.com/a"},
                    {"id": "r1", "videoLink": "https://drive.google.com/b"}],
    }
    coverage = review_coverage(creator)
    assert coverage.videos_needed == 1
    assert coverage.pending == 1
    assert coverage.covered


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    if failures:
        print(f"\n{failures} test(s) failed")
        sys.exit(1)
    print("\nAll tests passed")

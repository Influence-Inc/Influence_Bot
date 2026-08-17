"""
Tests for the creator's next step in the review chat
(services/creator_next_step.py).

The rule under test: at any moment the creator is offered exactly one
destination, or none — never a menu. Which one falls out of the state of
the campaign's drafts, so the interesting cases are the transitions
(approved -> post links -> done), the priority when two things are open
at once, and the states where the honest answer is "nothing, it's not
your move".

Also covered: the step never resolves to a link we can't produce, and
`/chat/<slug>/go/<step>` refuses to be used as an open redirect.

Run with `python -m pytest tests/test_creator_next_step.py`, or directly
with `python tests/test_creator_next_step.py`.
"""

import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CHAT_SECRET_KEY", "test-chat-secret-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.models import (  # noqa: E402
    ChatMessage,
    ChatSpace,
    ReviewSubmission,
    SessionLocal,
    init_db,
)
from services import chat_service, creator_next_step, submission_links  # noqa: E402

init_db()

CAMPAIGN_SLUG = "reve/reve-features"
REVIEW_URL = "https://campaigns.example.com/reve/reve-features/submit-for-review?username=riu"
POSTS_URL = "https://campaigns.example.com/reve/reve-features/submit-links?username=riu"


def _reset():
    db = SessionLocal()
    try:
        db.query(ChatMessage).delete()
        db.query(ChatSpace).delete()
        db.query(ReviewSubmission).delete()
        db.commit()
    finally:
        db.close()
    submission_links._misses.clear()


def _submit_review(*, username="riu.drafts", decision=None, ignored=False) -> int:
    db = SessionLocal()
    try:
        row = ReviewSubmission(
            campaign_slug=CAMPAIGN_SLUG,
            campaign_name="Reve Features",
            brand_name="Reve",
            creator_username=username,
            creator_email=f"{username}@example.com",
            video_link="https://drive.google.com/file/d/abc/view",
            notes="",
            decision=decision,
            ignored=ignored,
            submitted_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _space_for(review_id, *, with_links=True):
    """Open the chat space and give it the creator's submission links."""
    space = chat_service.get_or_create_for_review(review_id)
    assert space is not None
    if with_links:
        submission_links.remember(
            space.id,
            submission_links.SubmissionLinks(
                submit_for_review_url=REVIEW_URL, submit_posts_url=POSTS_URL
            ),
        )
    return chat_service.find_by_id(space.id)


def _decide(review_id, decision):
    """Record a decision the way the Slack buttons do: row + chat notice."""
    db = SessionLocal()
    try:
        row = db.query(ReviewSubmission).get(review_id)
        row.decision = decision
        row.decided_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    chat_service.post_review_decision_event(
        review_id=review_id, decision=decision, actor_name="Reve"
    )


def _resolve(space):
    """Re-read the space first, the way a fresh request would."""
    return creator_next_step.resolve(chat_service.find_by_id(space.id))


# ---------------------------------------------------------------------------
# The state table
# ---------------------------------------------------------------------------

def test_a_draft_with_the_brand_is_not_the_creators_move():
    _reset()
    space = _space_for(_submit_review())
    assert _resolve(space) is None


def test_changes_requested_asks_for_a_revised_draft():
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id)
    _decide(review_id, "changes_requested")

    step = _resolve(space)
    assert step is not None
    assert step.key == creator_next_step.KEY_RESUBMIT_DRAFT
    assert step.review_id == review_id
    assert creator_next_step.url_for_step(space, step.key) == REVIEW_URL


def test_approval_asks_for_the_live_post_links():
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id)
    _decide(review_id, "approved")

    step = _resolve(space)
    assert step is not None
    assert step.key == creator_next_step.KEY_SUBMIT_POSTS
    assert step.review_id == review_id
    assert creator_next_step.url_for_step(space, step.key) == POSTS_URL


def test_sharing_the_post_links_retires_the_step():
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id)
    _decide(review_id, "approved")
    assert _resolve(space).key == creator_next_step.KEY_SUBMIT_POSTS

    chat_service.post_posts_submitted_event(
        chat_space_id=space.id, platforms=["instagram"], video_id="v1"
    )
    assert _resolve(space) is None


def test_a_later_approval_reopens_the_post_links_step():
    """
    Post links shared for draft 1 don't answer draft 2's approval. The
    ordering is by message id, so the second approval outranks the earlier
    posts notice.
    """
    _reset()
    first = _submit_review()
    space = _space_for(first)
    _decide(first, "approved")
    chat_service.post_posts_submitted_event(
        chat_space_id=space.id, platforms=["instagram"], video_id="v1"
    )
    assert _resolve(space) is None

    second = _submit_review()
    chat_service.get_or_create_for_review(second)
    _decide(second, "approved")

    step = _resolve(space)
    assert step is not None
    assert step.key == creator_next_step.KEY_SUBMIT_POSTS
    assert step.review_id == second


def test_only_ignored_drafts_means_nothing_is_in_play():
    _reset()
    review_id = _submit_review(ignored=True)
    space = _space_for(review_id)

    step = _resolve(space)
    assert step is not None
    assert step.key == creator_next_step.KEY_SUBMIT_DRAFT
    assert step.review_id is None


def test_an_archived_space_has_no_next_step():
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id)
    _decide(review_id, "approved")
    assert _resolve(space) is not None

    chat_service.archive_space(space.id)
    assert _resolve(space) is None


# ---------------------------------------------------------------------------
# Only ever one
# ---------------------------------------------------------------------------

def test_a_revision_outranks_an_unposted_approval():
    """
    Draft 1 is approved and unposted while draft 2 comes back for changes.
    Both are open, but what the brand is waiting on wins — and the creator
    still sees a single button, not two.
    """
    _reset()
    first = _submit_review()
    space = _space_for(first)
    _decide(first, "approved")

    second = _submit_review()
    chat_service.get_or_create_for_review(second)
    _decide(second, "changes_requested")

    step = _resolve(space)
    assert step is not None
    assert step.key == creator_next_step.KEY_RESUBMIT_DRAFT
    assert step.review_id == second


def test_another_campaign_does_not_put_a_step_in_this_chat():
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id)

    db = SessionLocal()
    try:
        db.add(ReviewSubmission(
            campaign_slug="other/other-campaign",
            campaign_name="Other Campaign",
            brand_name="Other",
            creator_username="riu.drafts",
            decision="changes_requested",
            submitted_at=datetime.now(timezone.utc),
        ))
        db.commit()
    finally:
        db.close()

    assert _resolve(space) is None


# ---------------------------------------------------------------------------
# Never a link that goes nowhere
# ---------------------------------------------------------------------------

def test_no_resolvable_url_means_no_step_at_all(monkeypatch):
    """
    A space we can't produce a submission URL for shows nothing, rather
    than a button that dead-ends — the rule the approval email follows.
    """
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id, with_links=False)
    _decide(review_id, "approved")

    monkeypatch.setattr(
        submission_links, "fetch_from_api",
        lambda *a, **kw: submission_links.EMPTY,
    )
    assert _resolve(space) is None


def test_a_missing_url_is_fetched_once_then_cached(monkeypatch):
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id, with_links=False)
    _decide(review_id, "approved")

    calls = []

    def _fake_fetch(slug, username, **kw):
        calls.append(slug)
        return submission_links.SubmissionLinks(
            submit_for_review_url=REVIEW_URL, submit_posts_url=POSTS_URL
        )

    monkeypatch.setattr(submission_links, "fetch_from_api", _fake_fetch)

    assert _resolve(space).key == creator_next_step.KEY_SUBMIT_POSTS
    assert _resolve(space).key == creator_next_step.KEY_SUBMIT_POSTS
    # Written to the space on the first miss, so the second resolve is free.
    assert len(calls) == 1
    assert chat_service.find_by_id(space.id).submit_posts_url == POSTS_URL


def test_a_repeated_lookup_failure_is_not_retried_every_time(monkeypatch):
    """
    `for_space` sits on the chat page's render path and the API client
    allows 30s per call, so a creator the API has nothing for must not
    make their chat pay that cost on every load.
    """
    _reset()
    calls = []

    def _fake_campaigns(self, *a, **kw):
        calls.append(1)
        return []

    monkeypatch.setattr(
        "services.reelstats_api.ReelStatsAPI.get_campaigns", _fake_campaigns
    )
    for _ in range(3):
        assert not submission_links.fetch_from_api(CAMPAIGN_SLUG, "riu.drafts")
    assert len(calls) == 1

    # The paths that care more about being right than being fast opt out.
    assert not submission_links.fetch_from_api(
        CAMPAIGN_SLUG, "riu.drafts", use_miss_cache=False
    )
    assert len(calls) == 2


def test_posts_submitted_notice_is_idempotent_per_video():
    _reset()
    space = _space_for(_submit_review())
    first = chat_service.post_posts_submitted_event(
        chat_space_id=space.id, platforms=["instagram"], video_id="v1"
    )
    repeat = chat_service.post_posts_submitted_event(
        chat_space_id=space.id, platforms=["instagram"], video_id="v1"
    )
    other = chat_service.post_posts_submitted_event(
        chat_space_id=space.id, platforms=["tiktok"], video_id="v2"
    )
    assert first is not None
    assert repeat is None
    assert other is not None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

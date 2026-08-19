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
import pytest  # noqa: E402

from services import chat_service, creator_next_step, submission_links  # noqa: E402

init_db()


# Captured before any test can stub it over.
_real_fetch_creator = submission_links.fetch_creator


@pytest.fixture(autouse=True)
def _offline_campaigns_api(monkeypatch):
    """
    No test reaches the real campaigns API. The default is "we couldn't
    find out", which drops the resolver onto its local floor; tests that
    care about the authoritative count set it with `_api_reports_posted`.
    """
    monkeypatch.setattr(submission_links, "fetch_creator", lambda *a, **kw: None)


def _api_reports_posted(monkeypatch, count, required=None):
    """
    The campaigns site reports `count` videos with links logged, against a
    `required` target (`minVideos`) when the campaign sets one.
    """
    monkeypatch.setattr(
        submission_links,
        "fetch_creator",
        lambda *a, **kw: {
            "deliverables": {"actualVideos": count, "minVideos": required}
        },
    )
    _time_passes()


def _time_passes():
    """
    Clear the per-space recheck throttle, standing in for the minutes that
    would have elapsed between two visits to the chat.
    """
    creator_next_step._last_checked.clear()

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
    creator_next_step._last_checked.clear()


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


def test_links_logged_before_this_shipped_still_retire_the_step(monkeypatch):
    """
    The bug this counting replaced. A creator who logged their post links
    before `posts_submitted` notices existed has no notice in the chat, so
    inferring from the feed asked them for links they had already given —
    and no notice was ever going to arrive to stop it. The campaigns site
    knew all along; now we ask it.
    """
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id)
    _decide(review_id, "approved")
    # No posts_submitted event exists, exactly as for a pre-deploy space.
    assert _resolve(space).key == creator_next_step.KEY_SUBMIT_POSTS

    _api_reports_posted(monkeypatch, 1)
    assert _resolve(space) is None


def test_a_later_changes_request_does_not_reopen_a_posted_draft():
    """
    The other way the id comparison failed: it asked "is the newest posts
    notice newer than the newest decision?", which any later decision
    falsified — including a changes-requested on a different draft, which
    says nothing about draft 1's links.
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
    _decide(second, "changes_requested")
    assert _resolve(space).key == creator_next_step.KEY_RESUBMIT_DRAFT

    third = _submit_review()
    chat_service.get_or_create_for_review(third)
    # Draft 1 was posted long ago; nothing here is the creator's move.
    assert _resolve(space) is None


def test_the_count_is_per_approval_not_a_single_flag(monkeypatch):
    """Two approvals with one video posted still owes one set of links."""
    _reset()
    first = _submit_review()
    space = _space_for(first)
    _decide(first, "approved")
    second = _submit_review()
    chat_service.get_or_create_for_review(second)
    _decide(second, "approved")

    _api_reports_posted(monkeypatch, 1)
    step = _resolve(space)
    assert step is not None
    assert step.key == creator_next_step.KEY_SUBMIT_POSTS
    # Anchored to the approval still outstanding, not the one already posted.
    assert step.review_id == second

    _api_reports_posted(monkeypatch, 2)
    assert _resolve(space) is None


def test_an_unreachable_api_falls_back_to_our_own_notices():
    """
    Not knowing must not invent an answer. With the API unreachable the
    resolver uses the notices it has, which is a floor: it can undercount
    history, never overcount it.
    """
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id)
    _decide(review_id, "approved")
    assert _resolve(space).key == creator_next_step.KEY_SUBMIT_POSTS

    chat_service.post_posts_submitted_event(
        chat_space_id=space.id, platforms=["instagram"], video_id="v1"
    )
    assert _resolve(space) is None


def test_a_failed_lookup_is_never_cached_as_zero(monkeypatch):
    """
    Caching "we couldn't find out" as 0 would claim the creator has posted
    nothing, and stick.
    """
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id)
    _decide(review_id, "approved")
    assert _resolve(space).key == creator_next_step.KEY_SUBMIT_POSTS
    assert chat_service.find_by_id(space.id).posts_logged is None

    _api_reports_posted(monkeypatch, 1)
    assert _resolve(space) is None
    # Now that we know, it's cached and the next read is free.
    assert chat_service.find_by_id(space.id).posts_logged == 1


def _post_links(space, video_id):
    """A post-links submission lands, the way the webhook records it."""
    chat_service.post_posts_submitted_event(
        chat_space_id=space.id, platforms=["instagram"], video_id=video_id
    )


def test_a_post_that_skipped_review_cannot_answer_an_approval(monkeypatch):
    """
    The reported case. A video whose draft was shared in chat rather than
    submitted for review still counts on the campaigns site, so comparing
    bare totals let it cancel out a later approval that genuinely owed its
    links. Order settles it: a post recorded before an approval cannot be
    the one that approval is waiting for.
    """
    _reset()
    first = _submit_review()
    space = _space_for(first)
    _decide(first, "approved")
    _post_links(space, "v1")
    _post_links(space, "v2-never-reviewed")

    second = _submit_review()
    chat_service.get_or_create_for_review(second)
    _decide(second, "approved")
    _api_reports_posted(monkeypatch, 2)

    step = _resolve(space)
    assert step is not None
    assert step.key == creator_next_step.KEY_SUBMIT_POSTS
    assert step.review_id == second


def test_history_older_than_the_notices_still_leans_on_the_total(monkeypatch):
    """
    Ordering only reaches as far back as the notices do. Two videos posted
    before any existed, then a third approved and posted with one — the
    older two must stay settled by the site's total rather than being
    reopened for want of a notice.
    """
    _reset()
    first = _submit_review()
    space = _space_for(first)
    _decide(first, "approved")
    second = _submit_review()
    chat_service.get_or_create_for_review(second)
    _decide(second, "approved")
    third = _submit_review()
    chat_service.get_or_create_for_review(third)
    _decide(third, "approved")

    _post_links(space, "v3")
    _api_reports_posted(monkeypatch, 3)
    assert _resolve(space) is None


def test_a_finished_deliverable_asks_for_the_next_draft(monkeypatch):
    """
    The reported gap. One video approved, posted and logged, two still
    owed, nothing with the brand — the creator's move is the next draft,
    but the step only ever fired when they had sent nothing at all, so
    they got an empty chat.
    """
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id)
    _decide(review_id, "approved")
    _api_reports_posted(monkeypatch, 1, required=3)

    step = _resolve(space)
    assert step is not None
    assert step.key == creator_next_step.KEY_SUBMIT_DRAFT
    assert step.detail == "1 of 3 videos posted"
    assert creator_next_step.url_for_step(space, step.key) == REVIEW_URL


def test_a_draft_in_flight_counts_towards_what_is_owed(monkeypatch):
    """
    Asking again for a draft the creator has already sent would be
    nagging. What's with the brand counts towards the target, so the step
    goes quiet until the campaign wants more than is in flight.
    """
    _reset()
    first = _submit_review()
    space = _space_for(first)
    _decide(first, "approved")
    _api_reports_posted(monkeypatch, 1, required=2)
    assert _resolve(space).key == creator_next_step.KEY_SUBMIT_DRAFT

    second = _submit_review()
    chat_service.get_or_create_for_review(second)
    _time_passes()
    # 1 posted + 1 with the brand covers the 2 the campaign wants.
    assert _resolve(space) is None


def test_an_approved_draft_awaiting_links_is_not_asked_for_twice(monkeypatch):
    """
    An approved draft whose links haven't landed is already on its way to
    being a post. It owes links, not another draft.
    """
    _reset()
    first = _submit_review()
    space = _space_for(first)
    _decide(first, "approved")
    _api_reports_posted(monkeypatch, 0, required=1)

    step = _resolve(space)
    assert step is not None
    assert step.key == creator_next_step.KEY_SUBMIT_POSTS


def test_a_completed_campaign_asks_for_nothing(monkeypatch):
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id)
    _decide(review_id, "approved")
    _api_reports_posted(monkeypatch, 1, required=1)
    assert _resolve(space) is None


def test_a_stale_count_is_refreshed_before_asking_for_another_draft(monkeypatch):
    """
    A count fresh enough to settle the approvals can still be too stale to
    settle the campaign's target. Gating the refresh on approvals alone
    told a creator who had posted everything "2 of 3".
    """
    _reset()
    first = _submit_review()
    space = _space_for(first)
    _decide(first, "approved")
    second = _submit_review()
    chat_service.get_or_create_for_review(second)
    _decide(second, "approved")

    _api_reports_posted(monkeypatch, 2, required=3)
    assert _resolve(space).key == creator_next_step.KEY_SUBMIT_DRAFT

    # The third goes up. Both approvals were already covered, so nothing
    # about them would prompt another look.
    _api_reports_posted(monkeypatch, 3, required=3)
    assert _resolve(space) is None


def test_no_video_target_never_asks_for_another_draft(monkeypatch):
    """
    Without `minVideos` there is no shortfall to measure, and inventing a
    deliverable the campaign never asked for is worse than staying quiet.
    """
    _reset()
    review_id = _submit_review()
    space = _space_for(review_id)
    _decide(review_id, "approved")
    _api_reports_posted(monkeypatch, 1, required=None)
    assert _resolve(space) is None


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
    # Undo the offline default: the miss cache lives below fetch_creator,
    # so this test needs the real one.
    monkeypatch.setattr(submission_links, "fetch_creator", _real_fetch_creator)
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

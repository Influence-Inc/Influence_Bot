"""
Tests for the campaign-long chat space and the draft-submission widget.

The behaviour under test: a creator's chat space is opened once per
(creator, campaign, brand) and reused for every draft they submit on that
campaign, so the brand's notes on draft 1 are still on screen when draft 2
arrives. Each submission announces itself in the conversation as a
`review_submission` event (the card widget), and each decision as a
`review_decision` notice — the space itself stays open until the campaign
is archived.

Run with `python -m pytest tests/test_chat_space_persistence.py`, or
directly with `python tests/test_chat_space_persistence.py`.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

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
from services import chat_service, review_approval  # noqa: E402

init_db()


def _reset():
    db = SessionLocal()
    try:
        db.query(ChatMessage).delete()
        db.query(ChatSpace).delete()
        db.query(ReviewSubmission).delete()
        db.commit()
    finally:
        db.close()


_UNSET = object()


def _submit_review(
    *,
    username="riu.drafts",
    campaign_slug="reve/reve-features",
    brand="Reve",
    video_link="https://drive.google.com/file/d/abc/view",
    notes="First cut, let me know!",
    submitted_at=None,
    decision=None,
    decided_at=None,
    email=_UNSET,
) -> int:
    """Insert a ReviewSubmission the way the review_submitted webhook does."""
    db = SessionLocal()
    try:
        row = ReviewSubmission(
            campaign_slug=campaign_slug,
            campaign_name="Reve Features",
            brand_name=brand,
            creator_username=username,
            creator_email=(
                f"{username}@example.com" if email is _UNSET else email
            ),
            video_link=video_link,
            notes=notes,
            decision=decision,
            decided_at=decided_at,
            submitted_at=submitted_at or datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _post_text(space_id, *, created_at=None, party="brand"):
    db = SessionLocal()
    try:
        db.add(ChatMessage(
            chat_space_id=space_id,
            sender_party=party,
            body="a note",
            kind=chat_service.KIND_TEXT,
            created_at=created_at or datetime.now(timezone.utc),
        ))
        db.commit()
    finally:
        db.close()


def _kinds(space_id):
    return [m["kind"] for m in chat_service.list_messages(chat_space_id=space_id)]


# ---------------------------------------------------------------------------
# Space reuse
# ---------------------------------------------------------------------------

def test_resubmission_lands_in_the_same_space():
    _reset()
    first = chat_service.get_or_create_for_review(_submit_review())
    second = chat_service.get_or_create_for_review(_submit_review())

    assert first is not None and second is not None
    assert first.id == second.id
    assert first.public_slug == second.public_slug
    # Exactly one space exists for this creator + campaign + brand.
    db = SessionLocal()
    try:
        assert db.query(ChatSpace).count() == 1
    finally:
        db.close()


def test_space_follows_the_newest_review():
    _reset()
    r1 = _submit_review()
    space = chat_service.get_or_create_for_review(r1)
    r2 = _submit_review()
    chat_service.get_or_create_for_review(r2)

    assert chat_service.find_by_id(space.id).latest_review_id == r2


def test_a_different_creator_gets_a_different_space():
    _reset()
    a = chat_service.get_or_create_for_review(_submit_review(username="riu.drafts"))
    b = chat_service.get_or_create_for_review(_submit_review(username="someone.else"))

    assert a.id != b.id


def test_a_different_campaign_gets_a_different_space():
    _reset()
    a = chat_service.get_or_create_for_review(_submit_review(campaign_slug="reve/one"))
    b = chat_service.get_or_create_for_review(_submit_review(campaign_slug="reve/two"))

    assert a.id != b.id


def test_reuse_survives_the_creator_email_arriving_later():
    # `creator.email` is nullable in the review webhook and often gets
    # filled in mid-campaign. The reuse key prefers email when present, so
    # without the fallback key the second draft would open a second space
    # and strand the first round's feedback.
    _reset()
    first = chat_service.get_or_create_for_review(_submit_review(email=None))
    second = chat_service.get_or_create_for_review(
        _submit_review(email="riu.drafts@example.com")
    )

    assert second.id == first.id
    # …and it still resolves in the other direction.
    third = chat_service.get_or_create_for_review(_submit_review(email=None))
    assert third.id == first.id


def test_archived_space_is_not_resurrected():
    _reset()
    first = chat_service.get_or_create_for_review(_submit_review())
    assert chat_service.archive_space(first.id) is True

    second = chat_service.get_or_create_for_review(_submit_review())
    assert second.id != first.id
    assert chat_service.find_by_id(first.id).status == "archived"


def test_space_closed_by_the_legacy_approval_flow_reopens():
    # Rows closed by the old per-review flow still carry status "approved".
    # The next draft continues the same conversation rather than starting a
    # new space beside it.
    _reset()
    first = chat_service.get_or_create_for_review(_submit_review())
    assert chat_service.close_for_approval(first.id) is True

    second = chat_service.get_or_create_for_review(_submit_review())
    assert second.id == first.id
    assert second.status == "active"


# ---------------------------------------------------------------------------
# Submission + decision events
# ---------------------------------------------------------------------------

def test_submission_posts_a_draft_card_on_the_creator_side():
    _reset()
    review_id = _submit_review(notes="First cut, let me know!")
    space = chat_service.get_or_create_for_review(review_id)
    chat_service.post_review_submission_event(review_id, chat_space_id=space.id)

    msgs = chat_service.list_messages(chat_space_id=space.id)
    assert len(msgs) == 1
    card = msgs[0]
    assert card["kind"] == chat_service.KIND_REVIEW_SUBMISSION
    # Posted as the creator, so it renders on the creator's side.
    assert card["party"] == "creator"
    assert card["body"] == "First cut, let me know!"
    assert card["event"]["submission_number"] == 1
    assert card["event"]["video_link"] == "https://drive.google.com/file/d/abc/view"


def test_submission_card_is_idempotent_per_review():
    _reset()
    review_id = _submit_review()
    space = chat_service.get_or_create_for_review(review_id)
    chat_service.post_review_submission_event(review_id, chat_space_id=space.id)
    chat_service.post_review_submission_event(review_id, chat_space_id=space.id)

    assert _kinds(space.id) == [chat_service.KIND_REVIEW_SUBMISSION]


def test_draft_numbers_increment_within_the_campaign():
    _reset()
    r1 = _submit_review()
    space = chat_service.get_or_create_for_review(r1)
    chat_service.post_review_submission_event(r1, chat_space_id=space.id)
    r2 = _submit_review()
    chat_service.get_or_create_for_review(r2)
    chat_service.post_review_submission_event(r2, chat_space_id=space.id)

    msgs = chat_service.list_messages(chat_space_id=space.id)
    assert [m["event"]["submission_number"] for m in msgs] == [1, 2]


def test_second_draft_lands_under_the_first_rounds_feedback():
    # The whole point: draft 2's card arrives in the same feed as the
    # brand's notes on draft 1.
    _reset()
    r1 = _submit_review()
    space = chat_service.get_or_create_for_review(r1)
    chat_service.post_review_submission_event(r1, chat_space_id=space.id)
    _post_text(space.id, party="brand")

    r2 = _submit_review()
    again = chat_service.get_or_create_for_review(r2)
    chat_service.post_review_submission_event(r2, chat_space_id=again.id)

    assert again.id == space.id
    assert _kinds(space.id) == [
        chat_service.KIND_REVIEW_SUBMISSION,
        chat_service.KIND_TEXT,
        chat_service.KIND_REVIEW_SUBMISSION,
    ]


def test_approval_records_a_notice_and_leaves_the_chat_open():
    _reset()
    review_id = _submit_review()
    space = chat_service.get_or_create_for_review(review_id)
    chat_service.post_review_submission_event(review_id, chat_space_id=space.id)

    chat_service.post_review_decision_event(
        review_id=review_id, decision="approved", actor_name="jennifer",
    )

    assert chat_service.find_by_id(space.id).status == "active"
    msgs = chat_service.list_messages(chat_space_id=space.id)
    assert msgs[-1]["kind"] == chat_service.KIND_REVIEW_DECISION
    assert msgs[-1]["event"]["decision"] == "approved"
    assert msgs[-1]["body"] == "Draft 1 approved"


def test_repeated_changes_requested_clicks_post_one_notice():
    _reset()
    review_id = _submit_review()
    space = chat_service.get_or_create_for_review(review_id)
    for _ in range(3):
        chat_service.post_review_decision_event(
            review_id=review_id, decision="changes_requested",
        )
    # An approval after a changes-requested notice is new information.
    chat_service.post_review_decision_event(
        review_id=review_id, decision="approved",
    )
    # …but a repeat of the latest decision still collapses.
    chat_service.post_review_decision_event(
        review_id=review_id, decision="approved",
    )

    events = [
        m["event"]["decision"]
        for m in chat_service.list_messages(chat_space_id=space.id)
    ]
    assert events == ["changes_requested", "approved"]


def test_decision_resolves_the_space_for_an_older_review():
    # `latest_review_id` points at draft 2, but approving draft 1 must still
    # find the campaign's chat space (via the shared reuse key).
    _reset()
    r1 = _submit_review()
    space = chat_service.get_or_create_for_review(r1)
    r2 = _submit_review()
    chat_service.get_or_create_for_review(r2)

    assert chat_service.find_space_for_review(r1).id == space.id
    msg = chat_service.post_review_decision_event(
        review_id=r1, decision="approved",
    )
    assert msg is not None


# ---------------------------------------------------------------------------
# Auto-approval sweep scoping
# ---------------------------------------------------------------------------

def _sweep_collecting_approvals():
    approved: list[int] = []
    original = review_approval.approve_review_core

    def _fake(*, review_id, actor_id, actor_name):
        approved.append(review_id)
        return True

    review_approval.approve_review_core = _fake
    try:
        review_approval.run_auto_approval_sweep()
    finally:
        review_approval.approve_review_core = original
    return approved


def test_the_draft_card_alone_does_not_pause_the_silence_clock():
    # The card is written by the bot, not by a person — if it counted as
    # activity, nothing would ever auto-approve once this feature shipped.
    _reset()
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    review_id = _submit_review(submitted_at=stale)
    space = chat_service.get_or_create_for_review(review_id)
    chat_service.post_review_submission_event(review_id, chat_space_id=space.id)
    chat_service.post_review_decision_event(
        review_id=review_id, decision="changes_requested",
    )

    assert _sweep_collecting_approvals() == [review_id]


def test_someone_talking_after_the_draft_pauses_the_clock():
    _reset()
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    review_id = _submit_review(submitted_at=stale)
    space = chat_service.get_or_create_for_review(review_id)
    chat_service.post_review_submission_event(review_id, chat_space_id=space.id)
    _post_text(space.id)

    assert _sweep_collecting_approvals() == []


def test_talk_about_an_earlier_draft_does_not_keep_a_later_one_alive():
    # Draft 1 was discussed two days ago; draft 2 has been sitting in
    # silence for 25h and should still auto-approve.
    _reset()
    now = datetime.now(timezone.utc)
    r1 = _submit_review(
        submitted_at=now - timedelta(hours=50),
        decision="changes_requested",
        decided_at=now - timedelta(hours=49),
    )
    space = chat_service.get_or_create_for_review(r1)
    _post_text(space.id, created_at=now - timedelta(hours=48))

    r2 = _submit_review(submitted_at=now - timedelta(hours=25))
    chat_service.get_or_create_for_review(r2)
    chat_service.post_review_submission_event(r2, chat_space_id=space.id)

    # r1 saw a reply after its decision, so it is not swept; r2 has heard
    # nothing since it was submitted.
    assert _sweep_collecting_approvals() == [r2]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_chat_page_renders_the_widget_code():
    from flask import Flask, render_template_string
    from templates.chat_pages import CHAT_PAGE

    _reset()
    space = chat_service.get_or_create_for_review(_submit_review())
    app = Flask(__name__)
    with app.app_context():
        html = render_template_string(
            CHAT_PAGE, space=space, self_party="creator", chat_title="t",
            initial_read_state={}, is_admin=False,
        )
    assert "review_submission" in html      # card branch in renderMessage
    assert "review_decision" in html        # system-notice branch
    assert "reviewCardHtml" in html
    assert ".rcard-media" in html           # card styling shipped
    # The card links out to the draft, and only for http(s) URLs.
    assert "isHttpUrl" in html


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

"""
Tests for services.review_approval.run_auto_approval_sweep candidate
selection.

Focus is the "no action in 24h" path: a review the brand never acted on
should auto-approve after 24h ONLY when its chat space has no activity.
Any message in the space (from anyone) pauses that silence clock, because
the chat space is pre-created at review-post time and can carry a message
before the brand clicks a button.

Run with `python -m pytest tests/test_auto_approval_sweep.py`, or directly
with `python tests/test_auto_approval_sweep.py`.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.models import (  # noqa: E402
    ChatMessage,
    ChatSpace,
    ReviewSubmission,
    SessionLocal,
    init_db,
)
from services import review_approval  # noqa: E402

init_db()

_STALE = datetime.now(timezone.utc) - timedelta(hours=25)


def _reset():
    db = SessionLocal()
    try:
        db.query(ChatMessage).delete()
        db.query(ChatSpace).delete()
        db.query(ReviewSubmission).delete()
        db.commit()
    finally:
        db.close()


def _sweep_collecting_approvals():
    """Run the sweep with approve_review_core stubbed to record ids."""
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


def _make_review(*, decision=None, submitted_at=_STALE):
    db = SessionLocal()
    try:
        row = ReviewSubmission(
            creator_username="tharun.fyi",
            creator_email="tharunr16@gmail.com",
            brand_name="Reve",
            decision=decision,
            submitted_at=submitted_at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _make_space(review_id, *, status="active"):
    db = SessionLocal()
    try:
        space = ChatSpace(
            reuse_key=f"key-{review_id}",
            creator_username="tharun.fyi",
            latest_review_id=review_id,
            status=status,
        )
        db.add(space)
        db.commit()
        db.refresh(space)
        return space.id
    finally:
        db.close()


def _post(space_id, party="creator"):
    db = SessionLocal()
    try:
        db.add(ChatMessage(chat_space_id=space_id, sender_party=party, body="hi"))
        db.commit()
    finally:
        db.close()


def test_no_action_auto_approves_when_no_chat_space():
    _reset()
    rid = _make_review()

    assert _sweep_collecting_approvals() == [rid]


def test_no_action_auto_approves_when_chat_space_is_empty():
    _reset()
    rid = _make_review()
    _make_space(rid)

    assert _sweep_collecting_approvals() == [rid]


def test_no_action_paused_when_we_posted_before_the_brand():
    # The new behavior: a message we send in the pre-created chat space —
    # while the brand has stayed silent and clicked nothing — pauses the
    # no-action clock so the video is NOT auto-approved.
    _reset()
    rid = _make_review()
    space_id = _make_space(rid)
    _post(space_id, party="creator")

    assert _sweep_collecting_approvals() == []


def test_no_action_paused_by_a_message_in_an_archived_space():
    # Activity pauses the clock regardless of the space's status.
    _reset()
    rid = _make_review()
    space_id = _make_space(rid, status="archived")
    _post(space_id, party="admin")

    assert _sweep_collecting_approvals() == []


def test_recent_no_action_review_is_not_swept():
    _reset()
    rid = _make_review(submitted_at=datetime.now(timezone.utc) - timedelta(hours=1))
    _make_space(rid)

    assert _sweep_collecting_approvals() == []


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

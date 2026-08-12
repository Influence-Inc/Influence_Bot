"""
Tests for the "Ignore" button on review notifications
(services/review_ignore.py and everything it switches off).

What an ignored submission — a link the creator sent by mistake — must do:

* stay out of the 24h auto-approval sweep, so no "your video is approved"
  email goes to the creator for something nobody reviewed;
* stop counting as a video in review, so the creator keeps getting their
  deadline reminder emails;
* be reversible, putting back whatever decision the review already carried.

Plus the button itself: rendered on the INFLUENCE copy of a review message
and never on the brand's, and backfilled onto messages that were posted
before it existed.

Run with `python -m pytest tests/test_review_ignore.py`, or directly with
`python tests/test_review_ignore.py`.
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.models import (  # noqa: E402
    ChatMessage,
    ChatSpace,
    ReviewSubmission,
    SessionLocal,
    init_db,
)
from services import review_approval, review_ignore, review_messages  # noqa: E402
from services.review_coverage import review_coverage  # noqa: E402
from templates.slack_blocks import build_review_submitted_blocks  # noqa: E402

init_db()

STALE = datetime.now(timezone.utc) - timedelta(hours=25)
CAMPAIGN_SLUG = "reve/reve-features"
LINK = "https://drive.google.com/a"


def _reset():
    db = SessionLocal()
    try:
        db.query(ChatMessage).delete()
        db.query(ChatSpace).delete()
        db.query(ReviewSubmission).delete()
        db.commit()
    finally:
        db.close()


def _make_review(
    *,
    decision=None,
    ignored=False,
    submitted_at=STALE,
    decided_at=None,
    video_link=LINK,
    slack_channel="C-reviews",
    slack_ts="1700000000.0001",
):
    db = SessionLocal()
    try:
        row = ReviewSubmission(
            campaign_slug=CAMPAIGN_SLUG,
            campaign_name="Reve Features",
            brand_name="Reve",
            creator_username="tharun.fyi",
            creator_email="tharunr16@gmail.com",
            video_link=video_link,
            decision=decision,
            decided_at=decided_at,
            ignored=ignored,
            submitted_at=submitted_at,
            slack_channel=slack_channel,
            slack_ts=slack_ts,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
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


def _creator(*, min_videos=1, posted=0, reviews=(LINK,)):
    return {
        "username": "tharun.fyi",
        "email": "tharunr16@gmail.com",
        "campaign_id": "camp1",
        "campaign_name": "Reve Features",
        "campaign_slug": CAMPAIGN_SLUG,
        "brand_name": "Reve",
        "deadline": (date.today() + timedelta(days=1)).isoformat(),
        "deliverables": {
            "minVideos": min_videos,
            "minViews": None,
            "actualVideos": posted,
        },
        "totalVideosPosted": posted,
        "reviews": [
            {"id": f"r{i}", "videoLink": link} for i, link in enumerate(reviews)
        ],
    }


def _decision_of(review_id):
    db = SessionLocal()
    try:
        row = db.query(ReviewSubmission).get(review_id)
        return row.decision, bool(row.ignored)
    finally:
        db.close()


# --- Auto-approval -----------------------------------------------------------

def test_ignored_review_is_not_auto_approved():
    _reset()
    _make_review(ignored=True)

    assert _sweep_collecting_approvals() == []


def test_ignored_changes_requested_review_is_not_auto_approved():
    # Case 2 of the sweep: changes requested, then silence. Ignoring has to
    # stop that clock too.
    _reset()
    _make_review(decision="changes_requested", decided_at=STALE, ignored=True)

    assert _sweep_collecting_approvals() == []


def test_a_review_that_is_not_ignored_still_auto_approves():
    _reset()
    rid = _make_review()

    assert _sweep_collecting_approvals() == [rid]


def test_approve_review_core_refuses_an_ignored_review():
    # Backstop for a stale Slack button: the sweep filters ignored reviews
    # out, but nothing else may approve one either.
    _reset()
    rid = _make_review(ignored=True)

    assert review_approval.approve_review_core(
        review_id=rid, actor_id="U1", actor_name="someone",
    ) is False
    assert _decision_of(rid) == (None, True)


# --- Deadline reminder coverage ---------------------------------------------

def test_ignored_review_does_not_cover_the_creators_deliverables():
    # The creator owes 1 video and the only thing they "shared" was ignored,
    # so they're not waiting on the brand — the reminder email must go out.
    _reset()
    _make_review(ignored=True)

    assert not review_coverage(_creator()).covered


def test_a_live_review_still_covers_the_creators_deliverables():
    _reset()
    _make_review()

    assert review_coverage(_creator()).covered


def test_ignoring_a_changes_requested_review_keeps_it_out_of_play():
    _reset()
    _make_review(decision="changes_requested", ignored=True)

    assert not review_coverage(_creator()).covered


def test_resubmitting_the_same_link_puts_it_back_in_play():
    # A later row for the same video wins, so a creator who re-sends a link
    # that was ignored once is covered again.
    _reset()
    _make_review(ignored=True)
    _make_review(ignored=False)

    assert review_coverage(_creator()).covered


# --- State transitions -------------------------------------------------------

def test_ignore_sets_the_flag_and_records_who():
    _reset()
    rid = _make_review(ignored=False)

    assert review_ignore.ignore_review(
        review_id=rid, actor_id="U1", actor_name="jennifer",
    ) == review_ignore.IGNORED

    db = SessionLocal()
    try:
        row = db.query(ReviewSubmission).get(rid)
        assert row.ignored is True
        assert row.ignored_by_name == "jennifer"
        assert row.ignored_at is not None
    finally:
        db.close()


def test_ignoring_twice_is_reported_as_already_ignored():
    _reset()
    rid = _make_review(ignored=True)

    assert review_ignore.ignore_review(
        review_id=rid, actor_id="U1", actor_name="jennifer",
    ) == review_ignore.ALREADY_IGNORED


def test_an_approved_review_cannot_be_ignored():
    # The approval email is already out; there's nothing left to hold back.
    _reset()
    rid = _make_review(decision="approved")

    assert review_ignore.ignore_review(
        review_id=rid, actor_id="U1", actor_name="jennifer",
    ) == review_ignore.ALREADY_APPROVED
    assert _decision_of(rid) == ("approved", False)


def test_ignoring_an_unknown_review_is_reported_not_found():
    _reset()

    assert review_ignore.ignore_review(
        review_id=987654, actor_id="U1", actor_name="jennifer",
    ) == review_ignore.NOT_FOUND


def test_undo_restores_the_decision_the_review_already_had():
    # The ignore flag is separate from `decision` precisely so this works:
    # a draft that was in "changes requested" goes back to it.
    _reset()
    rid = _make_review(decision="changes_requested")
    review_ignore.ignore_review(review_id=rid, actor_id="U1", actor_name="jennifer")

    assert review_ignore.restore_review(review_id=rid) == review_ignore.RESTORED
    assert _decision_of(rid) == ("changes_requested", False)


def test_undo_puts_the_review_back_on_the_auto_approval_clock():
    _reset()
    rid = _make_review(ignored=True)
    assert _sweep_collecting_approvals() == []

    review_ignore.restore_review(review_id=rid)

    assert _sweep_collecting_approvals() == [rid]


def test_undo_on_a_review_that_is_not_ignored_changes_nothing():
    _reset()
    rid = _make_review()

    assert review_ignore.restore_review(review_id=rid) == review_ignore.NOT_IGNORED
    assert _decision_of(rid) == (None, False)


# --- The button --------------------------------------------------------------

def _action_ids(blocks):
    for block in blocks:
        if block.get("type") == "actions":
            return [el.get("action_id") for el in block.get("elements", [])]
    return []


def test_the_admin_card_carries_the_ignore_button():
    blocks = build_review_submitted_blocks(
        creator_username="tharun.fyi",
        campaign_name="Reve Features",
        brand_name="Reve",
        video_link=LINK,
        notes="",
        review_id=7,
        show_meta=True,
        admin_chat_url="https://example.test/admin/chats/1",
        include_ignore=True,
    )

    assert _action_ids(blocks) == ["review_approve", None, "review_ignore"]


def test_the_brand_card_does_not_carry_the_ignore_button():
    blocks = build_review_submitted_blocks(
        creator_username="tharun.fyi",
        campaign_name="Reve Features",
        brand_name="Reve",
        video_link=LINK,
        notes="",
        review_id=7,
        chat_url="https://example.test/chat/invite/abc",
    )

    assert _action_ids(blocks) == ["review_approve", "review_request_changes"]


# --- Backfill onto messages posted before the button existed -----------------

def _rewritten_review_ids(**kwargs):
    """Run the backfill with the Slack write stubbed, returning the ids hit."""
    hit: list[int] = []
    original = review_messages.rewrite_admin_review_message

    def _fake(card, *, blocks, text):
        hit.append(card.review_id)
        assert _action_ids(blocks)[-1] == "review_ignore"
        return True

    review_messages.rewrite_admin_review_message = _fake
    try:
        review_ignore.backfill_ignore_buttons(**kwargs)
    finally:
        review_messages.rewrite_admin_review_message = original
    return hit


def test_backfill_updates_reviews_still_awaiting_a_decision():
    _reset()
    rid = _make_review()

    assert _rewritten_review_ids() == [rid]


def test_backfill_includes_changes_requested_reviews():
    # Their admin copy still has live buttons, and they're still on the
    # sweep's clock, so they need the button too.
    _reset()
    rid = _make_review(decision="changes_requested")

    assert _rewritten_review_ids() == [rid]


def test_backfill_skips_settled_and_unpostable_reviews():
    _reset()
    _make_review(decision="approved")
    _make_review(ignored=True)
    _make_review(slack_ts=None)  # never made it to Slack
    _make_review(submitted_at=datetime.now(timezone.utc) - timedelta(days=90))
    live = _make_review()

    assert _rewritten_review_ids() == [live]


def test_backfill_age_window_is_configurable():
    _reset()
    _make_review(submitted_at=datetime.now(timezone.utc) - timedelta(days=45))

    assert _rewritten_review_ids() == []
    assert len(_rewritten_review_ids(max_age_days=60)) == 1


# --- The Slack handlers ------------------------------------------------------

class FakeApp:
    """Captures the handlers `register_actions` registers, by action_id."""

    def __init__(self):
        self.handlers = {}

    def action(self, action_id):
        def register(fn):
            self.handlers[action_id] = fn
            return fn
        return register


class FakeClient:
    def __init__(self):
        self.updates = []

    def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}


def _click(action_id, review_id, blocks):
    """Fire a button click at the registered handler, returning what it did."""
    from bot import actions as actions_module

    app = FakeApp()
    actions_module.register_actions(app)

    client, replies, brand_writes = FakeClient(), [], []

    original = review_messages.rewrite_brand_review_message

    def _fake_brand_write(review_id, *, with_buttons, text, footer=None):
        brand_writes.append({"with_buttons": with_buttons, "footer": footer})
        return True

    review_messages.rewrite_brand_review_message = _fake_brand_write
    try:
        app.handlers[action_id](
            ack=lambda: None,
            body={
                "user": {"id": "U1", "username": "jennifer"},
                "channel": {"id": "C-reviews"},
                "message": {"ts": "1700000000.0001", "blocks": blocks},
                "team": {"id": "T1"},
                "actions": [{"action_id": action_id, "value": str(review_id)}],
            },
            client=client,
            respond=lambda **kwargs: replies.append(kwargs),
        )
    finally:
        review_messages.rewrite_brand_review_message = original
    return client.updates, replies, brand_writes


def _admin_blocks_for(review_id):
    return review_messages.build_admin_blocks(
        review_messages.load_review_card(review_id)
    )


def test_clicking_ignore_swaps_the_buttons_for_an_undo():
    _reset()
    rid = _make_review()

    updates, replies, brand_writes = _click(
        "review_ignore", rid, _admin_blocks_for(rid),
    )

    assert _decision_of(rid) == (None, True)
    assert replies == []
    # Approve / Open as Admin / Ignore are gone; Undo ignore is offered.
    assert _action_ids(updates[0]["blocks"]) == ["review_unignore"]
    # And the brand's copy loses its buttons, with a note explaining why.
    assert len(brand_writes) == 1
    assert brand_writes[0]["with_buttons"] is False
    assert "No review needed" in str(brand_writes[0]["footer"])


def test_clicking_undo_restores_the_full_action_row():
    _reset()
    rid = _make_review()
    _click("review_ignore", rid, _admin_blocks_for(rid))

    updates, replies, brand_writes = _click(
        "review_unignore", rid, _admin_blocks_for(rid),
    )

    assert _decision_of(rid) == (None, False)
    assert replies == []
    assert _action_ids(updates[0]["blocks"]) == [
        "review_approve", "review_request_changes", "review_ignore",
    ]
    assert brand_writes[0]["with_buttons"] is True


def test_approving_an_ignored_review_is_refused_with_a_note():
    _reset()
    rid = _make_review(ignored=True)

    updates, replies, _brand = _click(
        "review_approve", rid, _admin_blocks_for(rid),
    )

    assert _decision_of(rid) == (None, True)
    assert updates == []
    assert "ignored" in replies[0]["text"]


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

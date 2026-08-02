"""
Tests for the deadline-reminder wiring in services/scheduler_service.py.

Covers what review coverage changes end to end: the creator email is held
when their videos in review cover what's outstanding, while the internal
Slack alert still posts — annotated so the team chases the review instead
of the creator.

Run with `python -m pytest tests/test_deadline_reminder_email.py`, or
directly with `python tests/test_deadline_reminder_email.py`.
"""

import json
import os
import sys
from datetime import date, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.models import (  # noqa: E402
    DeadlineReminder,
    EmailLog,
    ReviewSubmission,
    SessionLocal,
    init_db,
)
from services.email_service import EmailSendResult  # noqa: E402
from services.scheduler_service import SchedulerService  # noqa: E402

init_db()


class FakeSlackClient:
    def __init__(self):
        self.messages = []

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True, "channel": "C1", "ts": "1.0"}


class FakeEmailService:
    def __init__(self):
        self.sends = []

    def send_followup_if_not_sent(self, **kwargs):
        self.sends.append(kwargs)
        return EmailSendResult.SENT


def _fresh_scheduler():
    db = SessionLocal()
    try:
        db.query(DeadlineReminder).delete()
        db.query(EmailLog).delete()
        db.query(ReviewSubmission).delete()
        db.commit()
    finally:
        db.close()
    slack, email = FakeSlackClient(), FakeEmailService()
    return SchedulerService(slack, email, reelstats_api=None), slack, email


def _creator(reviews, min_videos=1, posted=0):
    return {
        "username": "tharun.fyi",
        "email": "tharunr16@gmail.com",
        "campaign_id": "camp1",
        "campaign_name": "Reve Features",
        "campaign_slug": "reve/reve-features",
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


def _blocks_text(message):
    return json.dumps(message["blocks"])


def test_email_sent_when_nothing_is_in_review():
    scheduler, slack, email = _fresh_scheduler()
    scheduler.check_deadline_reminder_for(_creator(reviews=[]))

    assert len(email.sends) == 1
    assert email.sends[0]["template_type"] == "deadline_1_day"
    assert len(slack.messages) == 1
    assert "Creator email skipped" not in _blocks_text(slack.messages[0])


def test_email_held_when_reviews_cover_the_remaining_videos():
    scheduler, slack, email = _fresh_scheduler()
    scheduler.check_deadline_reminder_for(
        _creator(reviews=["https://drive.google.com/a"])
    )

    assert email.sends == []
    # The team still hears about the deadline, with the reason attached.
    assert len(slack.messages) == 1
    assert "Creator email skipped" in _blocks_text(slack.messages[0])


def test_email_still_sent_when_reviews_cover_only_part_of_the_gap():
    scheduler, slack, email = _fresh_scheduler()
    scheduler.check_deadline_reminder_for(
        _creator(reviews=["https://drive.google.com/a"], min_videos=2)
    )

    assert len(email.sends) == 1
    assert "Creator email skipped" not in _blocks_text(slack.messages[0])


def test_held_email_is_not_marked_as_sent_in_the_baseline():
    # Seeding an EmailLog row for a suppressed email would mark the tier
    # "already emailed" forever, so it could never fire if the brand later
    # sends the draft back for changes.
    scheduler, _slack, _email = _fresh_scheduler()
    scheduler.seed_baseline([
        _creator(reviews=["https://drive.google.com/a"]),
        _creator(reviews=[]) | {"username": "other.creator"},
    ])

    db = SessionLocal()
    try:
        emailed = {row.creator_username for row in db.query(EmailLog).all()}
        reminded = {row.creator_username for row in db.query(DeadlineReminder).all()}
    finally:
        db.close()

    assert emailed == {"other.creator"}
    # The Slack-dedup row is still seeded for both — that message does post.
    assert reminded == {"tharun.fyi", "other.creator"}


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

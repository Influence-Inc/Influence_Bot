"""
Tests for the creator "new chat message" email (services/chat_notifications.py).

Rule under test: the email to the creator quotes the brand/admin message in
FULL, even when it's long — only the Slack pings use a truncated preview.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CHAT_SECRET_KEY", "test-chat-secret-key")
# _chat_url needs a base URL to build the magic link, else the email is skipped.
os.environ.setdefault("PUBLIC_BASE_URL", "https://i.influence.technology")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.models import ChatSpace, SessionLocal, init_db  # noqa: E402
from services import chat_notifications, chat_service  # noqa: E402

init_db()


def _make_space():
    db = SessionLocal()
    try:
        space = ChatSpace(
            reuse_key="k" + os.urandom(4).hex(),
            public_slug="slug-" + os.urandom(4).hex(),
            creator_username="riu.drafts",
            creator_email="riu@example.com",
            campaign_slug="reve/feature",
            campaign_name="Feature Group + Mobile",
            brand_name="Reve",
            status="active",
        )
        db.add(space)
        db.commit()
        db.refresh(space)
        db.expunge(space)
        return space
    finally:
        db.close()


def _capture_emails(monkeypatch_target):
    sent = []

    def fake_send_email(to_email, subject, body, *a, **k):
        sent.append({"to": to_email, "subject": subject, "body": body})
        return True

    chat_notifications._email_service.send_email = fake_send_email  # type: ignore
    return sent


def test_creator_email_includes_full_long_message():
    space = _make_space()
    # A message well over the 200-char Slack preview cap, with a distinctive
    # head and tail so truncation would be obvious.
    long_msg = (
        "Hi Riu! The video's looking great — a few notes to make it even better:\n"
        + ("- Tighten the :02-:05 stretch so it flows at natural speed. " * 6)
        + "\nFINAL_TAIL_MARKER: showcase the camera modes over prompt-based editing."
    )
    assert len(long_msg) > 200

    msg = chat_service.post_message(
        chat_space_id=space.id,
        sender_party="brand",
        sender_identifier="reve",
        sender_display_name="Reve Team",
        body=long_msg,
    )

    sent = _capture_emails(chat_notifications)
    chat_notifications.notify_new_message(
        chat_space_id=space.id, sender_party="brand", message_id=msg.id
    )

    assert len(sent) == 1, "expected exactly one creator email"
    body = sent[0]["body"]
    # Full message present, head and tail — and no truncation ellipsis.
    assert "Hi Riu! The video's looking great" in body
    assert "FINAL_TAIL_MARKER" in body
    assert long_msg in body
    assert "…" not in body


def test_admin_message_email_also_full():
    space = _make_space()
    long_msg = (
        "Admin note. "
        + ("Please revise this specific section carefully. " * 10)
        + "ADMIN_TAIL_MARKER"
    )
    assert len(long_msg) > 200
    msg = chat_service.post_message(
        chat_space_id=space.id,
        sender_party="admin",
        sender_identifier="influence-admin",
        sender_display_name="Influence",
        body=long_msg,
    )
    sent = _capture_emails(chat_notifications)
    chat_notifications.notify_new_message(
        chat_space_id=space.id, sender_party="admin", message_id=msg.id
    )
    assert len(sent) == 1
    assert long_msg in sent[0]["body"]
    assert "…" not in sent[0]["body"]


def test_brand_message_email_is_attributed_to_the_brand():
    space = _make_space()
    msg = chat_service.post_message(
        chat_space_id=space.id,
        sender_party="brand",
        sender_identifier="reve",
        sender_display_name="Reve Team",
        body="Loved it, one small tweak please.",
    )
    sent = _capture_emails(chat_notifications)
    chat_notifications.notify_new_message(
        chat_space_id=space.id, sender_party="brand", message_id=msg.id
    )
    assert sent[0]["subject"] == "Reve team messaged you — Content Review"
    assert "Reve Team just sent you a message" in sent[0]["body"]


def test_admin_message_email_is_not_attributed_to_the_brand():
    """Messages from our own side must not read as if the brand sent them —
    nor announce INFLUENCE as the sender. They're framed as feedback."""
    space = _make_space()
    msg = chat_service.post_message(
        chat_space_id=space.id,
        sender_party="admin",
        sender_identifier="influence-admin",
        sender_display_name="Influence",
        body="Quick note on the edit before we send it to the brand.",
    )
    sent = _capture_emails(chat_notifications)
    chat_notifications.notify_new_message(
        chat_space_id=space.id, sender_party="admin", message_id=msg.id
    )
    subject = sent[0]["subject"]
    body = sent[0]["body"]
    assert subject == "You've received feedback on your Reve content"
    assert "messaged you" not in subject
    assert "messaged you" not in body
    assert "Reve Team just sent you" not in body
    assert "You've received feedback" in body
    # The message itself and the reply link still come through.
    assert "Quick note on the edit" in body
    assert "https://i.influence.technology" in body


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all chat message email tests passed")

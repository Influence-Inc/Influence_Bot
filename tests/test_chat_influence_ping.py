"""
Tests for the INFLUENCE-team chat ping (services/chat_notifications._notify_influence_team).

Chat pings thread under the review-submitted "content to be reviewed" post so
the conversation stays grouped, but Slack doesn't notify channel members of a
thread reply unless they're following the thread — and nobody on the team is.

Rules under test:
  - Inbound messages (creator/brand) are broadcast to the channel
    (reply_broadcast=True) so the team is actually notified, while still
    threading under the review post (thread_ts preserved).
  - The team's own admin-sent messages stay quiet threaded replies
    (reply_broadcast falsy) — no need to broadcast their words back at them.
  - SLACK_REVIEWS_NOTIFY, when set, prepends a mention to inbound pings (in
    both the notification text and the block body) but not to admin pings.
"""

import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CHAT_SECRET_KEY", "test-chat-secret-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://i.influence.technology")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from models.models import (  # noqa: E402
    ChatSpace,
    SessionLocal,
    SlackInstallation,
    init_db,
)
from services import chat_notifications, chat_service  # noqa: E402

init_db()


class _FakeSlack:
    """Records every chat_postMessage call across all instances."""

    calls: list[dict] = []

    def __init__(self, *a, **k):
        pass

    def chat_postMessage(self, **kwargs):
        _FakeSlack.calls.append(kwargs)
        return {"ok": True, "channel": kwargs.get("channel"), "ts": "111.222"}


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
            # Pre-anchor a thread so the ping is a threaded reply, mirroring a
            # space whose review post's Slack coords were captured.
            admin_slack_channel="C_REVIEWS",
            admin_slack_ts="900.001",
        )
        db.add(space)
        db.commit()
        db.refresh(space)
        db.expunge(space)
        return space
    finally:
        db.close()


def _make_space_with_brand_install():
    """A space wired to a brand Slack install, plus a threaded brand post, so
    the brand-facing ping in notify_new_message fires."""
    db = SessionLocal()
    try:
        suffix = os.urandom(4).hex()
        install = SlackInstallation(
            brand="reve-" + suffix,
            team_id="T_BRAND_" + suffix,
            bot_token="xoxb-brand",
            channel_id="C_BRAND",
        )
        db.add(install)
        db.commit()
        db.refresh(install)
        space = ChatSpace(
            reuse_key="k" + os.urandom(4).hex(),
            public_slug="slug-" + os.urandom(4).hex(),
            creator_username="riu.drafts",
            creator_email="riu@example.com",
            campaign_slug="reve/feature",
            campaign_name="Feature Group + Mobile",
            brand_name="Reve",
            status="active",
            brand_install_id=install.id,
            brand_slack_channel="C_BRAND",
            brand_slack_ts="800.001",
            admin_slack_channel="C_REVIEWS",
            admin_slack_ts="900.001",
        )
        db.add(space)
        db.commit()
        db.refresh(space)
        db.expunge(space)
        return space
    finally:
        db.close()


def _setup():
    """Install fakes so no real Slack/email traffic goes out. Returns the
    captured chat_postMessage calls list (reset for each test)."""
    _FakeSlack.calls = []
    chat_notifications.WebClient = _FakeSlack  # type: ignore
    chat_notifications._email_service.send_email = lambda *a, **k: True  # type: ignore
    Config.SLACK_BOT_TOKEN = "xoxb-test"
    return _FakeSlack.calls


def _ping_for(calls):
    """The single ping posted into the reviews channel (no brand install on the
    test space, so the team ping is the only chat_postMessage)."""
    assert len(calls) == 1, f"expected exactly one Slack post, got {len(calls)}"
    return calls[0]


def test_creator_message_is_broadcast_and_threaded():
    Config.SLACK_REVIEWS_NOTIFY = None
    space = _make_space()
    calls = _setup()
    msg = chat_service.post_message(
        chat_space_id=space.id,
        sender_party="creator",
        sender_identifier="riu.drafts",
        sender_display_name="Riu",
        body="Here's the new cut!",
    )
    chat_notifications.notify_new_message(
        chat_space_id=space.id, sender_party="creator", message_id=msg.id
    )
    ping = _ping_for(calls)
    assert ping["reply_broadcast"] is True, "creator message must broadcast to channel"
    assert ping["thread_ts"] == "900.001", "still threads under the review post"


def test_brand_message_is_broadcast():
    Config.SLACK_REVIEWS_NOTIFY = None
    space = _make_space()
    calls = _setup()
    msg = chat_service.post_message(
        chat_space_id=space.id,
        sender_party="brand",
        sender_identifier="reve",
        sender_display_name="Reve Team",
        body="Can you tighten the intro?",
    )
    chat_notifications.notify_new_message(
        chat_space_id=space.id, sender_party="brand", message_id=msg.id
    )
    ping = _ping_for(calls)
    assert ping["reply_broadcast"] is True, "brand message must broadcast to channel"


def test_admin_message_is_not_broadcast():
    Config.SLACK_REVIEWS_NOTIFY = None
    space = _make_space()
    calls = _setup()
    msg = chat_service.post_message(
        chat_space_id=space.id,
        sender_party="admin",
        sender_identifier="influence-admin",
        sender_display_name="Influence",
        body="On it — will chase the creator.",
    )
    chat_notifications.notify_new_message(
        chat_space_id=space.id, sender_party="admin", message_id=msg.id
    )
    ping = _ping_for(calls)
    assert not ping["reply_broadcast"], "the team's own message must not broadcast"
    assert ping["thread_ts"] == "900.001", "admin message still threads for context"


def test_creator_message_broadcasts_to_brand_channel():
    Config.SLACK_REVIEWS_NOTIFY = None
    space = _make_space_with_brand_install()
    calls = _setup()
    msg = chat_service.post_message(
        chat_space_id=space.id,
        sender_party="creator",
        sender_identifier="riu.drafts",
        sender_display_name="Riu",
        body="Here's the new cut!",
    )
    chat_notifications.notify_new_message(
        chat_space_id=space.id, sender_party="creator", message_id=msg.id
    )
    # A creator message fires both the brand ping and the INFLUENCE-team ping.
    brand_pings = [c for c in calls if c.get("channel") == "C_BRAND"]
    assert len(brand_pings) == 1, "expected one ping into the brand channel"
    brand = brand_pings[0]
    assert brand["reply_broadcast"] is True, "brand ping must broadcast to channel"
    assert brand["thread_ts"] == "800.001", "still threads under the brand review post"


def test_admin_message_broadcasts_to_brand_channel():
    Config.SLACK_REVIEWS_NOTIFY = None
    space = _make_space_with_brand_install()
    calls = _setup()
    msg = chat_service.post_message(
        chat_space_id=space.id,
        sender_party="admin",
        sender_identifier="influence-admin",
        sender_display_name="Influence",
        body="Sharing a note for the brand.",
    )
    chat_notifications.notify_new_message(
        chat_space_id=space.id, sender_party="admin", message_id=msg.id
    )
    brand_pings = [c for c in calls if c.get("channel") == "C_BRAND"]
    assert len(brand_pings) == 1
    assert brand_pings[0]["reply_broadcast"] is True, "admin->brand ping must broadcast"


def test_mention_prepended_for_inbound_only():
    Config.SLACK_REVIEWS_NOTIFY = "<!subteam^S0REVIEW>"
    try:
        # Creator (inbound) — mention present in text and block body.
        space = _make_space()
        calls = _setup()
        msg = chat_service.post_message(
            chat_space_id=space.id,
            sender_party="creator",
            sender_identifier="riu.drafts",
            sender_display_name="Riu",
            body="Draft 2 is up.",
        )
        chat_notifications.notify_new_message(
            chat_space_id=space.id, sender_party="creator", message_id=msg.id
        )
        ping = _ping_for(calls)
        assert ping["text"].startswith("<!subteam^S0REVIEW> ")
        block_text = ping["blocks"][0]["text"]["text"]
        assert "<!subteam^S0REVIEW>" in block_text

        # Admin (outbound) — no mention, even when configured.
        space2 = _make_space()
        calls2 = _setup()
        Config.SLACK_REVIEWS_NOTIFY = "<!subteam^S0REVIEW>"
        msg2 = chat_service.post_message(
            chat_space_id=space2.id,
            sender_party="admin",
            sender_identifier="influence-admin",
            sender_display_name="Influence",
            body="Noted.",
        )
        chat_notifications.notify_new_message(
            chat_space_id=space2.id, sender_party="admin", message_id=msg2.id
        )
        ping2 = _ping_for(calls2)
        assert "<!subteam^S0REVIEW>" not in ping2["text"]
        assert "<!subteam^S0REVIEW>" not in ping2["blocks"][0]["text"]["text"]
    finally:
        Config.SLACK_REVIEWS_NOTIFY = None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all INFLUENCE-team chat ping tests passed")

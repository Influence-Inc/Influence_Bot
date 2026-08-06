"""
Tests for admin/creator/brand chat-UI parity (bot/chat_routes.py).

What's under test: the admin side of the review chat now renders the *same*
rich UI as the creator/brand chat (`CHAT_PAGE`) and drives the same
`/chat/<slug>/...` endpoints — while keeping every admin-only feature
(breadcrumb, metadata, export, archive/reopen, posting as "Influence").

An authenticated admin is resolved to an `_AdminSession` stand-in (party
"admin"), so posting, reacting, marking-read and polling all work through
the shared endpoints without a magic-link session.

Run with `python -m pytest tests/test_admin_chat_ui.py`, or directly with
`python tests/test_admin_chat_ui.py`.
"""

import os
import sys

# App modules read config at import time. Point everything at an in-memory
# SQLite DB and set the chat secrets the admin session/token flow needs.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CHAT_SECRET_KEY", "test-chat-secret-key")
os.environ.setdefault("CHAT_ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("PUBLIC_BASE_URL", "https://chat.test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402

from bot.chat_routes import register_chat_routes  # noqa: E402
from config import Config  # noqa: E402
from models.models import ChatSpace, SessionLocal, init_db  # noqa: E402
from services import chat_service  # noqa: E402

init_db()

# Requests use an https base URL so the Secure admin cookie is resent by the
# Werkzeug test-client cookie jar.
BASE = "https://chat.test"


def _client():
    app = Flask(__name__)
    register_chat_routes(app)
    app.testing = True
    return app.test_client()


def _make_space(status: str = "active") -> ChatSpace:
    db = SessionLocal()
    try:
        space = ChatSpace(
            reuse_key="k" + os.urandom(4).hex(),
            public_slug="slug-" + os.urandom(4).hex(),
            creator_username="virat",
            creator_email=None,  # keep notifications a no-op in tests
            campaign_slug="influuu/launch",
            campaign_name="Influuu",
            brand_name="Reve",
            status=status,
        )
        db.add(space)
        db.commit()
        db.refresh(space)
        db.expunge(space)
        return space
    finally:
        db.close()


def _login_admin(client) -> None:
    resp = client.post(
        "/admin/chats/login",
        data={"token": Config.CHAT_ADMIN_TOKEN},
        base_url=BASE,
    )
    assert resp.status_code == 302, resp.status_code


def test_admin_view_uses_shared_chat_ui_with_admin_chrome():
    space = _make_space()
    client = _client()
    _login_admin(client)

    resp = client.get(f"/admin/chats/{space.id}", base_url=BASE)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Same rich UI as creator/brand: the shared CHAT_PAGE markers.
    assert 'data-space-slug="%s"' % space.public_slug in html
    assert 'data-self-party="admin"' in html
    assert "connectSSE" in html          # live SSE wiring
    assert "composer-typing" in html     # typing indicator
    assert "react-btn" in html or "reactBtnHtml" in html  # reactions

    # Admin chrome: just the slim back link to the dashboard — no action
    # buttons (export / archive / reopen were intentionally removed).
    assert "admin-bar" in html
    assert "&lsaquo; Campaign dashboard" in html
    assert "Posting as Influence" in html
    assert "/export.md" not in html
    assert "/export.json" not in html
    assert "/archive" not in html
    assert "Reopen" not in html


def test_admin_view_requires_login():
    space = _make_space()
    client = _client()
    resp = client.get(f"/admin/chats/{space.id}", base_url=BASE)
    # Unauthenticated admins get the token gate, not the chat.
    assert resp.status_code == 200
    assert "Admin token" in resp.get_data(as_text=True)


def test_admin_can_post_react_and_read_via_shared_endpoints():
    space = _make_space()
    slug = space.public_slug
    client = _client()
    _login_admin(client)

    # Post through the same endpoint the creator/brand composer uses.
    resp = client.post(
        f"/chat/{slug}/messages",
        data={"body": "Hi from the admin side"},
        base_url=BASE,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    msg_id = resp.get_json()["id"]

    # It persists as an admin message authored by "Influence".
    msgs = chat_service.list_messages(chat_space_id=space.id)
    assert len(msgs) == 1
    assert msgs[0]["party"] == "admin"
    assert msgs[0]["sender"] == "Influence"

    # Polling as the admin returns it too.
    poll = client.get(f"/chat/{slug}/messages?since=0", base_url=BASE)
    assert poll.status_code == 200
    assert poll.get_json()["messages"][0]["id"] == msg_id

    # React + mark-read + typing all succeed for the admin session.
    react = client.post(
        f"/chat/{slug}/messages/{msg_id}/react",
        json={"emoji": "✅"},
        base_url=BASE,
    )
    assert react.status_code == 200 and react.get_json()["ok"] is True

    read = client.post(f"/chat/{slug}/read", json={"up_to": msg_id}, base_url=BASE)
    assert read.status_code == 200

    typing = client.post(f"/chat/{slug}/typing", base_url=BASE)
    assert typing.status_code == 200


def test_non_admin_without_session_is_still_rejected():
    space = _make_space()
    client = _client()  # no admin login, no chat session
    resp = client.post(
        f"/chat/{space.public_slug}/messages",
        data={"body": "nope"},
        base_url=BASE,
    )
    assert resp.status_code == 401


def test_admin_cannot_post_to_closed_chat():
    space = _make_space(status="approved")
    client = _client()
    _login_admin(client)
    resp = client.post(
        f"/chat/{space.public_slug}/messages",
        data={"body": "should be blocked"},
        base_url=BASE,
    )
    assert resp.status_code == 410

    # But the admin can still open the read-only record (back link present,
    # composer disabled).
    view = client.get(f"/admin/chats/{space.id}", base_url=BASE)
    assert view.status_code == 200
    body = view.get_data(as_text=True)
    assert "admin-bar" in body
    assert "&lsaquo; Campaign dashboard" in body
    assert 'contenteditable="false"' in body  # read-only composer on a closed chat


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all admin chat UI parity tests passed")

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

    # No admin chrome at all now — the admin view is the shared chat UI,
    # distinguished only by the "admin" self-party and the composer hint.
    assert "Posting as Influence" in html
    assert "admin-bar" not in html
    assert "Campaign dashboard" not in html
    assert "/export.md" not in html
    assert "/export.json" not in html
    assert "/admin/chats/%s/archive" % space.id not in html


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

    # But the admin can still open the read-only record (composer disabled).
    view = client.get(f"/admin/chats/{space.id}", base_url=BASE)
    assert view.status_code == 200
    body = view.get_data(as_text=True)
    assert 'data-self-party="admin"' in body
    assert 'contenteditable="false"' in body  # read-only composer on a closed chat


def test_admin_can_silently_edit_message():
    space = _make_space()
    slug = space.public_slug
    client = _client()
    _login_admin(client)

    mid = client.post(
        f"/chat/{slug}/messages", data={"body": "orignal typo"}, base_url=BASE
    ).get_json()["id"]

    resp = client.post(
        f"/chat/{slug}/messages/{mid}/edit",
        json={"body": "Fixed text\nwith a second line"},
        base_url=BASE,
    )
    assert resp.status_code == 200 and resp.get_json()["ok"] is True

    msgs = chat_service.list_messages(chat_space_id=space.id)
    assert msgs[0]["body"] == "Fixed text\nwith a second line"
    # Still one message — an edit never creates a new one.
    assert len(msgs) == 1


def test_admin_can_edit_in_closed_chat():
    # Silent edits are an admin correction tool, so they work even after the
    # chat is closed (posting is blocked, editing is not).
    space = _make_space()
    slug = space.public_slug
    client = _client()
    _login_admin(client)
    mid = client.post(
        f"/chat/{slug}/messages", data={"body": "before"}, base_url=BASE
    ).get_json()["id"]
    assert chat_service.close_for_approval(space.id) is True

    resp = client.post(
        f"/chat/{slug}/messages/{mid}/edit", json={"body": "after"}, base_url=BASE
    )
    assert resp.status_code == 200
    assert chat_service.list_messages(chat_space_id=space.id)[0]["body"] == "after"


def test_edit_requires_admin():
    space = _make_space()
    slug = space.public_slug
    admin = _client()
    _login_admin(admin)
    mid = admin.post(
        f"/chat/{slug}/messages", data={"body": "hi"}, base_url=BASE
    ).get_json()["id"]

    # No admin cookie, no chat session at all -> rejected before the admin gate.
    anon = _client()
    resp = anon.post(
        f"/chat/{slug}/messages/{mid}/edit", json={"body": "hacked"}, base_url=BASE
    )
    assert resp.status_code == 401

    # A legitimate creator session is authenticated but not an admin -> 403.
    from utils.chat_tokens import SESSION_COOKIE, create_session
    _row, cookie = create_session(
        chat_space_id=space.id, party="creator",
        identifier="c@example.com", display_name="Creator",
    )
    creator = _client()
    creator.set_cookie(SESSION_COOKIE, cookie, domain="chat.test")
    resp = creator.post(
        f"/chat/{slug}/messages/{mid}/edit", json={"body": "hacked"}, base_url=BASE
    )
    assert resp.status_code == 403

    # Body untouched by either attempt.
    assert chat_service.list_messages(chat_space_id=space.id)[0]["body"] == "hi"


def test_chat_page_js_escapes_survive_render():
    # CHAT_PAGE is a non-raw triple-quoted Python string, so a JS regex/string
    # written with a single backslash (\\n, \\r, \\t) collapses into a real
    # control char in the delivered <script> and breaks the whole page. The
    # pure-Python suite can't execute JS, so guard the delivered bytes instead.
    from flask import Flask, render_template_string
    from templates.chat_pages import CHAT_PAGE

    space = _make_space()
    app = Flask(__name__)
    with app.app_context():
        html = render_template_string(
            CHAT_PAGE, space=space, self_party="admin", chat_title="t",
            initial_read_state={}, is_admin=True,
        )
    # No stray carriage return / other control chars leaked into the output.
    for ch in ("\r", "\x08", "\x0b", "\x0c"):
        assert ch not in html, f"control char {ch!r} leaked into delivered page"
    # The newline-normalising regexes must reach the browser as real escapes.
    assert r"replace(/\r\n/g, '\n')" in html
    assert r"replace(/\n{3,}/g, '\n\n')" in html


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all admin chat UI parity tests passed")

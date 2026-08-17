"""
Tests for `/chat/<slug>/go/<step>` and the next-step wiring in the chat
page (bot/chat_routes.py, templates/chat_pages.py).

The destination is never taken from the request. The route reads the step
key, checks it against what this space's state actually says is open, and
resolves the URL from the space's own columns — so the creator's
submission token never lands in the page's markup, and the endpoint can't
be pointed anywhere else. Everything else about it (who may use it, what
happens when the step has moved on) follows from that.

Run with `python -m pytest tests/test_chat_next_step_route.py`, or
directly with `python tests/test_chat_next_step_route.py`.
"""

import os
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CHAT_SECRET_KEY", "test-chat-secret-key")
os.environ.setdefault("CHAT_ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("PUBLIC_BASE_URL", "https://chat.test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402

from bot.chat_routes import register_chat_routes  # noqa: E402
from models.models import (  # noqa: E402
    ChatMessage,
    ChatSpace,
    ReviewSubmission,
    SessionLocal,
    init_db,
)
from services import chat_service, submission_links  # noqa: E402
from utils.chat_tokens import SESSION_COOKIE, create_session  # noqa: E402

init_db()

BASE = "https://chat.test"
CAMPAIGN_SLUG = "influuu/launch"
REVIEW_URL = "https://campaigns.test/influuu/launch/submit-for-review?username=virat&t=tok"
POSTS_URL = "https://campaigns.test/influuu/launch/submit-links?username=virat&t=tok"


def _client():
    app = Flask(__name__)
    register_chat_routes(app)
    app.testing = True
    return app.test_client()


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


def _approved_space():
    """A space whose creator owes their live post links."""
    _reset()
    db = SessionLocal()
    try:
        review = ReviewSubmission(
            campaign_slug=CAMPAIGN_SLUG,
            campaign_name="Influuu",
            brand_name="Reve",
            creator_username="virat",
            creator_email=None,  # keeps notifications a no-op in tests
            video_link="https://drive.google.com/file/d/abc/view",
            submitted_at=datetime.now(timezone.utc),
        )
        db.add(review)
        db.commit()
        review_id = review.id
    finally:
        db.close()

    space = chat_service.get_or_create_for_review(review_id)
    submission_links.remember(
        space.id,
        submission_links.SubmissionLinks(
            submit_for_review_url=REVIEW_URL, submit_posts_url=POSTS_URL
        ),
    )
    db = SessionLocal()
    try:
        row = db.query(ReviewSubmission).get(review_id)
        row.decision = "approved"
        row.decided_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    chat_service.post_review_decision_event(
        review_id=review_id, decision="approved", actor_name="Reve"
    )
    return chat_service.find_by_id(space.id)


def _as(client, space, party):
    _row, cookie = create_session(
        chat_space_id=space.id, party=party, identifier=f"{party}@test",
        display_name=party,
    )
    client.set_cookie(SESSION_COOKIE, cookie, domain="chat.test")
    return client


# ---------------------------------------------------------------------------
# The redirect
# ---------------------------------------------------------------------------

def test_creator_is_sent_to_the_page_their_step_calls_for():
    space = _approved_space()
    client = _as(_client(), space, "creator")

    resp = client.get(f"/chat/{space.public_slug}/go/submit-posts", base_url=BASE)
    assert resp.status_code == 302
    target = urlparse(resp.headers["Location"])
    assert target.netloc == "campaigns.test"
    assert target.path == "/influuu/launch/submit-links"

    # And it carries the way back, so submitting doesn't dead-end.
    query = parse_qs(target.query)
    assert query["return"] == [f"{BASE}/chat/{space.public_slug}"]
    # The original query string survives being appended to.
    assert query["username"] == ["virat"]
    assert query["t"] == ["tok"]
    # No caching: the right destination changes as the review does.
    assert resp.headers.get("Cache-Control") == "no-store"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"


def test_a_step_that_is_not_open_goes_back_to_the_chat():
    """
    A bookmark for a step that's since been done — or one that was never
    this space's step — lands in the conversation, not on a form nobody
    needs to fill in.
    """
    space = _approved_space()
    client = _as(_client(), space, "creator")

    resp = client.get(f"/chat/{space.public_slug}/go/resubmit-draft", base_url=BASE)
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/chat/{space.public_slug}"


def test_an_unknown_step_key_goes_back_to_the_chat():
    space = _approved_space()
    client = _as(_client(), space, "creator")

    resp = client.get(f"/chat/{space.public_slug}/go/../../evil", base_url=BASE)
    assert resp.status_code in (302, 404)
    if resp.status_code == 302:
        assert urlparse(resp.headers["Location"]).netloc in ("", "chat.test")


def test_the_step_is_the_creators_alone():
    space = _approved_space()

    brand = _as(_client(), space, "brand")
    assert brand.get(
        f"/chat/{space.public_slug}/go/submit-posts", base_url=BASE
    ).status_code == 403

    stranger = _client()
    assert stranger.get(
        f"/chat/{space.public_slug}/go/submit-posts", base_url=BASE
    ).status_code == 401


def test_no_resolvable_url_does_not_redirect_anywhere(monkeypatch):
    space = _approved_space()
    db = SessionLocal()
    try:
        row = db.query(ChatSpace).get(space.id)
        row.submit_posts_url = None
        row.submit_for_review_url = None
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        submission_links, "fetch_from_api", lambda *a, **kw: submission_links.EMPTY
    )

    client = _as(_client(), space, "creator")
    resp = client.get(f"/chat/{space.public_slug}/go/submit-posts", base_url=BASE)
    # No step resolves at all now, so this is a stale link: back to the chat.
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/chat/{space.public_slug}"


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def test_the_page_never_carries_the_submission_token():
    space = _approved_space()
    client = _as(_client(), space, "creator")

    html = client.get(
        f"/chat/{space.public_slug}", base_url=BASE
    ).get_data(as_text=True)

    assert "campaigns.test" not in html
    assert "t=tok" not in html
    # It points at our own route instead, and knows which step is open.
    assert '"route": "submit-posts"' in html or '"route":"submit-posts"' in html
    assert "Add your live post links" in html


def test_the_poll_carries_the_current_step():
    space = _approved_space()
    client = _as(_client(), space, "creator")

    payload = client.get(
        f"/chat/{space.public_slug}/messages", base_url=BASE
    ).get_json()
    assert payload["next_step"]["key"] == "submit_posts"

    chat_service.post_posts_submitted_event(
        chat_space_id=space.id, platforms=["instagram"], video_id="v1"
    )
    payload = client.get(
        f"/chat/{space.public_slug}/messages", base_url=BASE
    ).get_json()
    assert payload["next_step"] is None


def test_an_approved_chat_keeps_the_step_after_the_composer_closes():
    """
    Approval closes the chat for typing. That's exactly when posting the
    links is the only thing left to do, so the step has to outlive the
    composer instead of the page dead-ending on a disabled input.
    """
    space = _approved_space()
    chat_service.close_for_approval(space.id)
    client = _as(_client(), space, "creator")

    html = client.get(
        f"/chat/{space.public_slug}", base_url=BASE
    ).get_data(as_text=True)
    assert "composer no-input" in html
    assert "Add your live post links" in html


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

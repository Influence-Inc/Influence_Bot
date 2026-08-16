"""
Tests for AI-drafted admin replies (services/ai_drafts.py + the
`/chat/<slug>/ai-draft` route).

What's under test: the admin side of the review chat can ask Claude for a few
sendable replies built from the chat's own transcript. Drafts are never posted
— the endpoint only returns text for the composer — so the guarantees that
matter are (a) only an authenticated admin can spend a model call, (b) the
transcript handed to the model labels who said what, and (c) every failure
path comes back as one status code and one message the UI can show.

No network: the Anthropic client is replaced with a stub, so the request shape
and the response parsing are exercised without an API key.

Run with `python -m pytest tests/test_ai_drafts.py`, or directly with
`python tests/test_ai_drafts.py`.
"""

import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CHAT_SECRET_KEY", "test-chat-secret-key")
os.environ.setdefault("CHAT_ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("PUBLIC_BASE_URL", "https://chat.test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402

from bot.chat_routes import register_chat_routes  # noqa: E402
from config import Config  # noqa: E402
from models.models import ChatSpace, SessionLocal, init_db  # noqa: E402
from services import ai_drafts, chat_service  # noqa: E402

init_db()

BASE = "https://chat.test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
            creator_email=None,
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
        "/admin/chats/login", data={"token": Config.CHAT_ADMIN_TOKEN}, base_url=BASE
    )
    assert resp.status_code == 302, resp.status_code


class _patched:
    """Temporarily swap attributes on an object (no pytest fixtures — these
    tests also run under the plain `python tests/...` runner at the bottom)."""

    def __init__(self, obj, **values):
        self.obj, self.values, self.saved = obj, values, {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = getattr(self.obj, key)
            setattr(self.obj, key, value)
        return self.obj

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            setattr(self.obj, key, value)
        return False


class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Response:
    def __init__(self, payload, stop_reason="end_turn"):
        self.content = [_Block(json.dumps(payload) if not isinstance(payload, str) else payload)]
        self.stop_reason = stop_reason


class _StubAnthropic:
    """Stands in for the `anthropic` module inside ai_drafts."""

    class AuthenticationError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class APIStatusError(Exception):
        status_code = 500

    class APIConnectionError(Exception):
        pass

    def __init__(self, response=None, raises=None):
        self.response, self.raises = response, raises
        self.calls = []
        stub = self

        class _Messages:
            def create(self, **kwargs):
                stub.calls.append(kwargs)
                if stub.raises is not None:
                    raise stub.raises
                return stub.response

        class _Client:
            def __init__(self, api_key=None):
                self.api_key = api_key
                self.messages = _Messages()

        self.Anthropic = _Client


def _stub_sdk(stub):
    """Patch both the SDK import and the API-key check for one call."""
    return _patched(
        ai_drafts,
        _import_anthropic=lambda: stub,
        is_configured=lambda: True,
    )


# ---------------------------------------------------------------------------
# Transcript context
# ---------------------------------------------------------------------------

def test_build_context_labels_every_speaker_and_event():
    space = _make_space()
    chat_service.post_message(
        chat_space_id=space.id, sender_party="creator",
        sender_identifier="virat@x.com", sender_display_name="virat",
        body="Here's the new cut", kind="review_submission",
        event={"submission_number": 2, "video_link": "https://drive.example/v2"},
    )
    chat_service.post_message(
        chat_space_id=space.id, sender_party="brand",
        sender_identifier="ops@reve.com", sender_display_name="Maya",
        body="The hook is still slow",
    )
    chat_service.post_message(
        chat_space_id=space.id, sender_party="admin",
        sender_identifier="influence-admin", sender_display_name="Influence",
        body="On it — I'll get a tighter open",
    )

    context = ai_drafts.build_context(space.id)
    assert "[Draft 2 submitted by @virat] link: https://drive.example/v2" in context
    assert "creator's note: Here's the new cut" in context
    assert "Maya (Reve, brand): The hook is still slow" in context
    assert "Jennifer (you, INFLUENCE): On it" in context
    # Oldest first, so the model reads the conversation in order.
    assert context.index("Draft 2 submitted") < context.index("Maya (Reve")


def test_build_context_keeps_only_the_most_recent_messages():
    space = _make_space()
    for i in range(6):
        chat_service.post_message(
            chat_space_id=space.id, sender_party="creator",
            sender_identifier="virat@x.com", sender_display_name="virat",
            body=f"message {i}",
        )
    context = ai_drafts.build_context(space.id, limit=3)
    assert "message 5" in context and "message 3" in context
    assert "message 2" not in context
    assert len(context.splitlines()) == 3


def test_build_context_of_unknown_space_is_empty():
    assert ai_drafts.build_context(999999) == ""


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def test_draft_replies_sends_transcript_and_returns_cleaned_drafts():
    space = _make_space()
    chat_service.post_message(
        chat_space_id=space.id, sender_party="brand",
        sender_identifier="ops@reve.com", sender_display_name="Maya",
        body="Can we see a revised cut?",
    )
    stub = _StubAnthropic(
        _Response({"drafts": ['  "Sending it over today"  ', "On it — tomorrow AM", ""]})
    )
    with _stub_sdk(stub):
        drafts = ai_drafts.draft_replies(
            chat_space_id=space.id, instruction="push for a date"
        )

    # Quotes stripped, blanks dropped.
    assert drafts == ["Sending it over today", "On it — tomorrow AM"]

    call = stub.calls[0]
    assert call["model"] == Config.CLAUDE_MODEL
    assert call["output_config"]["effort"] == Config.CLAUDE_EFFORT
    assert call["output_config"]["format"]["type"] == "json_schema"
    # Persona is cached in `system`; per-space context rides in the user turn.
    assert call["system"][0]["text"] == ai_drafts.SYSTEM_PROMPT
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    prompt = call["messages"][0]["content"]
    assert "Maya (Reve, brand): Can we see a revised cut?" in prompt
    assert "push for a date" in prompt
    assert "Creator: @virat" in prompt


def test_draft_replies_without_api_key_is_not_configured():
    space = _make_space()
    with _patched(Config, ANTHROPIC_API_KEY=None):
        assert ai_drafts.is_configured() is False
        try:
            ai_drafts.draft_replies(chat_space_id=space.id)
        except ai_drafts.DraftError as exc:
            assert exc.code == "not_configured"
        else:
            raise AssertionError("expected DraftError")


def test_draft_replies_surfaces_upstream_failures_as_draft_errors():
    space = _make_space()
    cases = [
        ("auth", lambda s: s.AuthenticationError("bad key")),
        ("rate_limited", lambda s: s.RateLimitError("slow down")),
        ("upstream", lambda s: s.APIStatusError("boom")),
        ("upstream", lambda s: s.APIConnectionError("no route")),
    ]
    for expected, make in cases:
        stub = _StubAnthropic()
        stub.raises = make(_StubAnthropic)
        with _stub_sdk(stub):
            try:
                ai_drafts.draft_replies(chat_space_id=space.id)
            except ai_drafts.DraftError as exc:
                assert exc.code == expected, (expected, exc.code)
            else:
                raise AssertionError(f"expected DraftError for {expected}")


def test_draft_replies_rejects_unusable_model_output():
    space = _make_space()
    for payload, stop_reason in [
        ("not json at all", "end_turn"),
        ({"drafts": []}, "end_turn"),
        ('{"drafts": ["half', "max_tokens"),  # truncated mid-JSON
    ]:
        stub = _StubAnthropic(_Response(payload, stop_reason=stop_reason))
        with _stub_sdk(stub):
            try:
                ai_drafts.draft_replies(chat_space_id=space.id)
            except ai_drafts.DraftError as exc:
                assert exc.code == "empty"
            else:
                raise AssertionError("expected DraftError")


def test_draft_replies_honours_a_refusal():
    space = _make_space()
    stub = _StubAnthropic(_Response({"drafts": ["x"]}, stop_reason="refusal"))
    with _stub_sdk(stub):
        try:
            ai_drafts.draft_replies(chat_space_id=space.id)
        except ai_drafts.DraftError as exc:
            assert exc.code == "refused"
        else:
            raise AssertionError("expected DraftError")


def test_draft_replies_caps_the_number_of_options():
    space = _make_space()
    stub = _StubAnthropic(_Response({"drafts": ["a", "b", "c", "d", "e", "f"]}))
    with _stub_sdk(stub):
        assert len(ai_drafts.draft_replies(chat_space_id=space.id, count=99)) == ai_drafts.MAX_DRAFTS


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

def test_admin_gets_drafts_and_nothing_is_posted():
    space = _make_space()
    client = _client()
    _login_admin(client)
    captured = {}

    def fake(*, chat_space_id, instruction=""):
        captured.update(space_id=chat_space_id, instruction=instruction)
        return ["Chasing that now", "Want me to nudge Maya?"]

    with _patched(ai_drafts, draft_replies=fake):
        resp = client.post(
            f"/chat/{space.public_slug}/ai-draft?as=admin",
            json={"instruction": "ask about the deadline"},
            base_url=BASE,
        )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["drafts"] == ["Chasing that now", "Want me to nudge Maya?"]
    assert captured == {"space_id": space.id, "instruction": "ask about the deadline"}
    # Drafting is not sending.
    assert chat_service.list_messages(chat_space_id=space.id) == []


def test_ai_draft_requires_an_admin():
    from utils.chat_tokens import SESSION_COOKIE, create_session

    space = _make_space()
    called = []

    def fake(**kwargs):
        called.append(kwargs)
        return ["nope"]

    with _patched(ai_drafts, draft_replies=fake):
        anon = _client()
        assert anon.post(
            f"/chat/{space.public_slug}/ai-draft", json={}, base_url=BASE
        ).status_code == 401

        _row, cookie = create_session(
            chat_space_id=space.id, party="creator",
            identifier="virat@x.com", display_name="virat",
        )
        creator = _client()
        creator.set_cookie(SESSION_COOKIE, cookie, domain="chat.test")
        assert creator.post(
            f"/chat/{space.public_slug}/ai-draft", json={}, base_url=BASE
        ).status_code == 403

        # ...and the `as=admin` tag grants nothing without an admin cookie.
        assert creator.post(
            f"/chat/{space.public_slug}/ai-draft?as=admin", json={}, base_url=BASE
        ).status_code == 403

    assert called == []  # no model call on any rejected request


def test_ai_draft_blocked_on_a_closed_chat():
    space = _make_space(status="approved")
    client = _client()
    _login_admin(client)
    called = []

    def fake(**kwargs):
        called.append(kwargs)
        return ["x"]

    with _patched(ai_drafts, draft_replies=fake):
        resp = client.post(
            f"/chat/{space.public_slug}/ai-draft?as=admin", json={}, base_url=BASE
        )
    assert resp.status_code == 410
    assert called == []  # nothing can be sent there, so nothing is generated


def test_draft_errors_map_to_status_codes_with_a_message():
    space = _make_space()
    client = _client()
    _login_admin(client)
    for code, status in [
        ("not_configured", 503),
        ("rate_limited", 429),
        ("upstream", 502),
        ("refused", 422),
    ]:
        def fake(*, chat_space_id, instruction="", _code=code):
            raise ai_drafts.DraftError(_code, "human readable reason")

        with _patched(ai_drafts, draft_replies=fake):
            resp = client.post(
                f"/chat/{space.public_slug}/ai-draft?as=admin", json={}, base_url=BASE
            )
        assert resp.status_code == status, (code, resp.status_code)
        body = resp.get_json()
        assert body["error"] == code
        assert body["message"] == "human readable reason"


def test_ai_draft_is_rate_limited_per_admin_session():
    space = _make_space()
    client = _client()
    _login_admin(client)
    with _patched(ai_drafts, draft_replies=lambda **kw: ["ok"]):
        statuses = [
            client.post(
                f"/chat/{space.public_slug}/ai-draft?as=admin", json={}, base_url=BASE
            ).status_code
            for _ in range(12)
        ]
    assert statuses[:10] == [200] * 10
    assert statuses[10:] == [429, 429]


# ---------------------------------------------------------------------------
# Composer UI
# ---------------------------------------------------------------------------

def _render(space, is_admin, enabled):
    from flask import Flask, render_template_string
    from templates.chat_pages import CHAT_PAGE

    app = Flask(__name__)
    with app.app_context():
        return render_template_string(
            CHAT_PAGE, space=space, self_party="admin" if is_admin else "creator",
            chat_title="t", initial_read_state={}, is_admin=is_admin,
            ai_drafts_enabled=enabled,
        )


def test_draft_button_is_admin_only_and_needs_the_feature_configured():
    space = _make_space()

    admin = _render(space, is_admin=True, enabled=True)
    assert 'id="aiBtn"' in admin
    assert 'id="drafts"' in admin
    assert "/ai-draft" in admin
    assert "Suggested replies" in admin

    # Creator/brand never see it, even when the server has a key.
    creator = _render(space, is_admin=False, enabled=True)
    assert 'id="aiBtn"' not in creator
    assert 'id="drafts"' not in creator

    # Nor does the admin when no key is configured.
    unconfigured = _render(space, is_admin=True, enabled=False)
    assert 'id="aiBtn"' not in unconfigured
    assert "✨ drafts a reply" not in unconfigured


def test_draft_button_disabled_on_a_closed_chat():
    space = _make_space(status="archived")
    html = _render(space, is_admin=True, enabled=True)
    marker = html[html.index('id="aiBtn"'):]
    assert "disabled" in marker[: marker.index(">")]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all AI draft tests passed")

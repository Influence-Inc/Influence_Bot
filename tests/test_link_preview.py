"""
Tests for draft-link previews (services/link_preview.py + the
`/chat/<slug>/link-preview/<msg_id>` route).

What's under test: a draft card's video link is turned into a real thumbnail
served from our own origin — Google Drive / YouTube / Vimeo via their known
thumbnail endpoints, everything else via the page's `og:image` — with the
fetch hardened against being used as a general-purpose proxy, and every
failure degrading to "no preview" (404) so the card keeps its placeholder.

Run with `python -m pytest tests/test_link_preview.py`, or directly with
`python tests/test_link_preview.py`.
"""

import http.server
import os
import socket
import sys
import threading

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CHAT_SECRET_KEY", "test-chat-secret-key")
os.environ.setdefault("CHAT_ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("PUBLIC_BASE_URL", "https://chat.test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402

from bot.chat_routes import register_chat_routes  # noqa: E402
from config import Config  # noqa: E402
from models.models import ChatSpace, SessionLocal, init_db  # noqa: E402
from services import chat_service, link_preview  # noqa: E402

init_db()

BASE = "https://chat.test"

# A 1x1 PNG — small, and unambiguously an image.
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# A stub origin, so the fetch path is exercised for real (no network needed)
# ---------------------------------------------------------------------------

class _StubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, status, ctype, body: bytes, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        path = self.path.split("?")[0]
        origin = f"http://127.0.0.1:{self.server.server_port}"
        if path == "/page":
            html = (
                '<html><head><meta property="og:title" content="Draft">'
                '<meta property="og:image" content="/thumb.png">'
                "</head><body>ignored</body></html>"
            ).encode()
            self._send(200, "text/html; charset=utf-8", html)
        elif path == "/page-no-og":
            self._send(200, "text/html", b"<html><head></head><body>hi</body></html>")
        elif path == "/thumb.png":
            self._send(200, "image/png", PNG)
        elif path == "/redirect-to-thumb":
            self._send(302, "text/plain", b"", {"Location": f"{origin}/thumb.png"})
        elif path == "/redirect-to-private":
            self._send(302, "text/plain", b"", {"Location": "http://169.254.169.254/latest/meta-data/"})
        elif path == "/not-an-image":
            self._send(200, "text/html", b"<html>login required</html>")
        elif path == "/svg":
            self._send(200, "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        elif path == "/huge.png":
            self._send(200, "image/png", b"\x00" * (link_preview._MAX_IMAGE_BYTES + 1024))
        else:
            self._send(404, "text/plain", b"nope")


class _StubOrigin:
    """Serves the fixtures above on loopback for the duration of a test.

    `_resolve_public_ip` is patched to allow 127.0.0.1 while it runs — the
    guard itself is tested separately.
    """

    def __init__(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self._real_guard = link_preview._resolve_public_ip
        link_preview._resolve_public_ip = lambda host: host == "127.0.0.1"
        link_preview.clear_cache()
        return self

    def __exit__(self, *exc):
        link_preview._resolve_public_ip = self._real_guard
        link_preview.clear_cache()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        return False


# ---------------------------------------------------------------------------
# Known-host thumbnail URLs
# ---------------------------------------------------------------------------

def test_google_drive_share_links_resolve_to_a_thumbnail_endpoint():
    for url in (
        "https://drive.google.com/file/d/1AbC_dEfGhIjKlMnO/view?usp=sharing",
        "https://drive.google.com/open?id=1AbC_dEfGhIjKlMnO",
        "https://docs.google.com/file/d/1AbC_dEfGhIjKlMnO/edit",
    ):
        candidates = link_preview._direct_thumbnail_urls(url)
        assert candidates, url
        assert "1AbC_dEfGhIjKlMnO" in candidates[0]
        assert candidates[0].startswith("https://drive.google.com/thumbnail?id=")


def test_youtube_links_resolve_to_a_thumbnail_endpoint():
    for url in (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    ):
        candidates = link_preview._direct_thumbnail_urls(url)
        assert candidates, url
        assert all("dQw4w9WgXcQ" in c for c in candidates)


def test_unknown_host_has_no_direct_candidate_and_falls_back_to_scraping():
    # A Dropbox link has no predictable thumbnail URL, so it goes the og:image
    # route instead of guessing.
    assert link_preview._direct_thumbnail_urls(
        "https://www.dropbox.com/s/xyz/draft.mp4?dl=0"
    ) == []
    # A Drive *folder* carries no file id either.
    assert link_preview._direct_thumbnail_urls(
        "https://drive.google.com/drive/folders/1AbC_dEfGhIjKlMnO"
    ) == []


def test_open_graph_parser_prefers_the_most_specific_tag():
    parser = link_preview._OpenGraphParser()
    parser.feed(
        '<html><head>'
        '<meta name="twitter:image" content="https://x.test/twitter.jpg">'
        '<meta property="og:image" content="https://x.test/og.jpg">'
        '<meta property="og:image:secure_url" content="https://x.test/secure.jpg">'
        '</head><body>'
        '<meta property="og:image" content="https://x.test/body-should-be-ignored.jpg">'
        '</body></html>'
    )
    assert parser.best() == "https://x.test/secure.jpg"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def test_scrapes_og_image_and_returns_the_bytes():
    with _StubOrigin() as origin:
        result = link_preview.fetch_thumbnail(f"{origin.url}/page")
        assert result is not None
        body, ctype = result
        assert body == PNG
        assert ctype == "image/png"


def test_relative_og_image_is_resolved_against_the_page():
    # The fixture's og:image is "/thumb.png" — a bare path. Getting bytes back
    # at all proves it was joined onto the page URL.
    with _StubOrigin() as origin:
        assert link_preview.fetch_thumbnail(f"{origin.url}/page") is not None


def test_page_without_og_image_has_no_preview():
    with _StubOrigin() as origin:
        assert link_preview.fetch_thumbnail(f"{origin.url}/page-no-og") is None


def test_redirects_are_followed():
    with _StubOrigin() as origin:
        result = link_preview._get_bytes(
            f"{origin.url}/redirect-to-thumb",
            accept="image/png",
            max_bytes=link_preview._MAX_IMAGE_BYTES,
            want_image=True,
        )
        assert result is not None and result[0] == PNG


def test_a_redirect_to_an_internal_address_is_refused():
    # The classic SSRF hop: a public URL that 302s at cloud metadata.
    with _StubOrigin() as origin:
        assert link_preview._get_bytes(
            f"{origin.url}/redirect-to-private",
            accept="image/png",
            max_bytes=link_preview._MAX_IMAGE_BYTES,
            want_image=True,
        ) is None


def test_non_image_and_svg_responses_are_rejected():
    with _StubOrigin() as origin:
        for path in ("/not-an-image", "/svg"):
            assert link_preview._get_bytes(
                f"{origin.url}{path}",
                accept="image/png",
                max_bytes=link_preview._MAX_IMAGE_BYTES,
                want_image=True,
            ) is None, path


def test_oversized_images_are_rejected():
    with _StubOrigin() as origin:
        assert link_preview._get_bytes(
            f"{origin.url}/huge.png",
            accept="image/png",
            max_bytes=link_preview._MAX_IMAGE_BYTES,
            want_image=True,
        ) is None


def test_results_are_cached_per_url():
    with _StubOrigin() as origin:
        url = f"{origin.url}/page"
        assert link_preview.fetch_thumbnail(url) is not None
        # Take the origin away: a second call must be served from cache.
        origin.server.shutdown()
        assert link_preview.fetch_thumbnail(url) is not None


def test_a_busy_fetcher_gives_up_without_poisoning_the_cache():
    # All slots taken: the call bails rather than parking a web thread, and
    # crucially doesn't record a "no preview" that would stick for 10 minutes.
    with _StubOrigin() as origin:
        url = f"{origin.url}/page"
        held = []
        try:
            while link_preview._FETCH_SLOTS.acquire(blocking=False):
                held.append(1)
            real_wait = link_preview._SLOT_WAIT
            link_preview._SLOT_WAIT = 0.05
            try:
                assert link_preview.fetch_thumbnail(url) is None
            finally:
                link_preview._SLOT_WAIT = real_wait
            assert link_preview._cache_get(url) == (False, None)
        finally:
            for _ in held:
                link_preview._FETCH_SLOTS.release()

        # With the slots back, the same link resolves normally.
        assert link_preview.fetch_thumbnail(url) is not None


def test_internal_hosts_are_refused():
    # The real guard, unpatched.
    for host in ("localhost", "127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"):
        assert not link_preview._resolve_public_ip(host), host


def test_non_http_schemes_are_never_fetched():
    for url in ("file:///etc/passwd", "ftp://example.com/x.png", "javascript:alert(1)", ""):
        assert link_preview.fetch_thumbnail(url) is None, url


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

def _client():
    app = Flask(__name__)
    register_chat_routes(app)
    app.testing = True
    return app.test_client()


def _make_space() -> ChatSpace:
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
            status="active",
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
    assert resp.status_code == 302


def _post_draft_card(space, link: str):
    return chat_service.post_message(
        chat_space_id=space.id,
        sender_party="creator",
        sender_identifier="creator:test",
        sender_display_name="virat",
        body="",
        kind=chat_service.KIND_REVIEW_SUBMISSION,
        event={"submission_number": 2, "video_link": link},
        allow_empty=True,
    )


def test_link_preview_serves_the_thumbnail_for_a_draft_card():
    space = _make_space()
    msg = _post_draft_card(space, "https://drive.google.com/file/d/abc1234567/view")
    client = _client()
    _login_admin(client)

    calls = []
    real = link_preview.fetch_thumbnail
    link_preview.fetch_thumbnail = lambda url: (calls.append(url), (PNG, "image/png"))[1]
    try:
        resp = client.get(
            f"/chat/{space.public_slug}/link-preview/{msg.id}?as=admin", base_url=BASE
        )
    finally:
        link_preview.fetch_thumbnail = real

    assert resp.status_code == 200
    assert resp.data == PNG
    assert resp.mimetype == "image/png"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    # The URL came from the stored event, not from the request.
    assert calls == ["https://drive.google.com/file/d/abc1234567/view"]


def test_link_preview_404s_when_the_host_has_no_preview():
    space = _make_space()
    msg = _post_draft_card(space, "https://example.test/private-draft")
    client = _client()
    _login_admin(client)

    real = link_preview.fetch_thumbnail
    link_preview.fetch_thumbnail = lambda url: None
    try:
        resp = client.get(
            f"/chat/{space.public_slug}/link-preview/{msg.id}?as=admin", base_url=BASE
        )
    finally:
        link_preview.fetch_thumbnail = real
    assert resp.status_code == 404


def test_link_preview_404s_for_a_card_with_no_link():
    space = _make_space()
    msg = _post_draft_card(space, "")
    client = _client()
    _login_admin(client)
    resp = client.get(
        f"/chat/{space.public_slug}/link-preview/{msg.id}?as=admin", base_url=BASE
    )
    assert resp.status_code == 404


def test_link_preview_requires_a_session():
    space = _make_space()
    msg = _post_draft_card(space, "https://drive.google.com/file/d/abc1234567/view")
    client = _client()  # no session, no admin cookie
    resp = client.get(
        f"/chat/{space.public_slug}/link-preview/{msg.id}", base_url=BASE
    )
    assert resp.status_code == 401


def test_a_message_from_another_space_is_not_reachable():
    space_a = _make_space()
    space_b = _make_space()
    msg = _post_draft_card(space_a, "https://drive.google.com/file/d/abc1234567/view")
    # The same message id, asked for through a different space's slug.
    assert chat_service.draft_link_for_message(
        chat_space_id=space_b.id, message_id=msg.id
    ) is None
    assert chat_service.draft_link_for_message(
        chat_space_id=space_a.id, message_id=msg.id
    ) == "https://drive.google.com/file/d/abc1234567/view"


def test_a_plain_text_message_is_not_a_preview_target():
    space = _make_space()
    msg = chat_service.post_message(
        chat_space_id=space.id,
        sender_party="creator",
        sender_identifier="creator:test",
        sender_display_name="virat",
        body="here's the link https://drive.google.com/file/d/abc1234567/view",
    )
    assert chat_service.draft_link_for_message(
        chat_space_id=space.id, message_id=msg.id
    ) is None


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))

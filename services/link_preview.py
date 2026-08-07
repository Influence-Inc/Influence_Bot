"""
Thumbnail previews for draft video links.

A `review_submission` card in the chat shows the draft's video link. Left to
itself the card is a flat placeholder, so this module turns the link into an
actual picture of the video — the way iMessage, Slack and WhatsApp all render
a shared link.

Two strategies, tried in order:

1. **Known hosts get a direct thumbnail URL.** Google Drive, YouTube, Vimeo
   and Loom all expose a predictable (or cheaply discoverable) thumbnail
   endpoint. Going straight there avoids downloading and parsing a page.
2. **Everything else gets its `og:image` scraped** from the landing page,
   which is what every link-unfurler does.

Whatever comes back is fetched server-side and handed to the browser from our
own origin. That keeps the creator's Drive/Dropbox URL out of the brand's
browser history, sidesteps hotlink blocking, and means a link that has no
preview simply 404s so the card keeps its placeholder artwork.

Everything here is best-effort: any failure returns ``None`` and the caller
falls back to the placeholder. Nothing raises.

Safety: the links come from creator submissions, so fetches are treated as
untrusted. `_resolve_public_ip` rejects anything pointing at a private,
loopback, or otherwise internal address, and redirects are followed manually
so every hop gets the same check (a public host that 302s to 169.254.169.254
is the classic SSRF hop). Responses are size-capped while streaming.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

# Pretend to be a normal browser: several hosts (Dropbox and Notion among
# them) omit Open Graph tags for unknown clients.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

_CONNECT_TIMEOUT = 4.0
_READ_TIMEOUT = 6.0
_MAX_REDIRECTS = 4
_MAX_IMAGE_BYTES = 3 * 1024 * 1024      # a card thumbnail is never this big
_MAX_HTML_BYTES = 512 * 1024            # og: tags live in <head>; 512K is plenty

# Cache successes for a long while (a draft's thumbnail doesn't change) and
# failures briefly, so a private Drive link doesn't get re-fetched on every
# poll but does recover soon after the creator fixes the sharing settings.
_CACHE_TTL_OK = 6 * 60 * 60
_CACHE_TTL_MISS = 10 * 60
_CACHE_MAX = 128

# url -> (expires_at, (bytes, content_type) | None)
_CACHE: dict[str, tuple[float, tuple[bytes, str] | None]] = {}
_CACHE_LOCK = threading.Lock()

# The web dyno runs one worker with a fixed thread pool, and each of those
# threads is also serving SSE streams. Cap how many can be parked on an
# outbound fetch at once so a burst of first-time cards can't starve the
# chat itself; anything over the cap gives up quickly and is retried by the
# next page load rather than queueing.
_FETCH_SLOTS = threading.BoundedSemaphore(8)
_SLOT_WAIT = 2.0


# ---------------------------------------------------------------------------
# Known hosts
# ---------------------------------------------------------------------------

# /file/d/<id>/view (Drive's share URL) and /d/<id> (the googleusercontent and
# Docs shapes) both carry the file id in the path.
_DRIVE_PATH_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]{10,})")
_DRIVE_QUERY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _google_drive_file_id(url: str) -> str:
    """The file id out of any of Drive's URL shapes, or ''."""
    parsed = urlparse(url)
    m = _DRIVE_PATH_ID_RE.search(parsed.path or "")
    if m:
        return m.group(1)
    # /open?id=..., /uc?id=..., /thumbnail?id=...
    ids = parse_qs(parsed.query or "").get("id") or []
    if ids and _DRIVE_QUERY_ID_RE.match(ids[0] or ""):
        return ids[0]
    return ""


def _youtube_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = _host(url)
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
        return candidate if _YT_ID_RE.match(candidate) else ""
    if "youtube.com" not in host:
        return ""
    vs = parse_qs(parsed.query or "").get("v") or []
    if vs and _YT_ID_RE.match(vs[0] or ""):
        return vs[0]
    # /shorts/<id>, /embed/<id>, /live/<id>
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
        return parts[1] if _YT_ID_RE.match(parts[1]) else ""
    return ""


def _oembed_thumbnail(endpoint: str, url: str) -> str:
    """Ask an oEmbed provider for its `thumbnail_url`."""
    fetched = _get_bytes(
        f"{endpoint}?url={quote(url, safe='')}",
        accept="application/json",
        max_bytes=_MAX_HTML_BYTES,
        want_image=False,
    )
    if not fetched:
        return ""
    body, _ctype = fetched
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return ""
    thumb = data.get("thumbnail_url") if isinstance(data, dict) else None
    return thumb if isinstance(thumb, str) else ""


def _direct_thumbnail_urls(url: str) -> list[str]:
    """Thumbnail URLs to try before falling back to scraping the page.

    Ordered best-first; the first one that returns a real image wins.
    """
    host = _host(url)

    if host.endswith("google.com"):
        file_id = _google_drive_file_id(url)
        if file_id:
            return [
                f"https://drive.google.com/thumbnail?id={file_id}&sz=w640",
                f"https://lh3.googleusercontent.com/d/{file_id}=w640",
            ]

    video_id = _youtube_video_id(url)
    if video_id:
        return [
            f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        ]

    if host.endswith("vimeo.com"):
        thumb = _oembed_thumbnail("https://vimeo.com/api/oembed.json", url)
        return [thumb] if thumb else []

    if host.endswith("loom.com"):
        thumb = _oembed_thumbnail("https://www.loom.com/v1/oembed", url)
        return [thumb] if thumb else []

    return []


# ---------------------------------------------------------------------------
# Open Graph scraping
# ---------------------------------------------------------------------------

class _OpenGraphParser(HTMLParser):
    """Collects the handful of meta tags that can name a preview image.

    Stops paying attention once </head> closes — everything we want is in the
    head, and the body of a Drive or Dropbox page is megabytes of markup.
    """

    # Best first.
    _KEYS = (
        "og:image:secure_url",
        "og:image:url",
        "og:image",
        "twitter:image:src",
        "twitter:image",
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: dict[str, str] = {}
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done or tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        key = (a.get("property") or a.get("name") or "").strip().lower()
        content = (a.get("content") or "").strip()
        if key in self._KEYS and content and key not in self.images:
            self.images[key] = content

    def handle_endtag(self, tag):
        if tag == "head":
            self.done = True

    def best(self) -> str:
        for key in self._KEYS:
            if self.images.get(key):
                return self.images[key]
        return ""


def _scrape_og_image(page_url: str) -> str:
    fetched = _get_bytes(
        page_url,
        accept="text/html,application/xhtml+xml",
        max_bytes=_MAX_HTML_BYTES,
        want_image=False,
    )
    if not fetched:
        return ""
    body, _ctype = fetched
    parser = _OpenGraphParser()
    try:
        parser.feed(body.decode("utf-8", "replace"))
    except Exception:  # malformed markup — take whatever was parsed
        pass
    image = parser.best()
    if not image:
        return ""
    return urljoin(page_url, image)


# ---------------------------------------------------------------------------
# Safe fetching
# ---------------------------------------------------------------------------

def is_previewable_url(url: str) -> bool:
    """Cheap syntactic check — http(s) with a hostname."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _resolve_public_ip(host: str) -> bool:
    """True when every address `host` resolves to is publicly routable.

    Blocks the SSRF targets that matter on a PaaS: loopback, RFC1918, the
    link-local metadata range, and anything otherwise reserved.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _get_bytes(
    url: str,
    *,
    accept: str,
    max_bytes: int,
    want_image: bool,
) -> tuple[bytes, str] | None:
    """GET `url`, following redirects by hand so each hop is re-validated.

    Returns (body, content_type), or None on any failure — including a body
    that is larger than `max_bytes` or, when `want_image`, isn't an image.
    """
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        if not is_previewable_url(current):
            return None
        host = urlparse(current).hostname or ""
        if not _resolve_public_ip(host):
            logger.info("link preview: refusing non-public host %s", host)
            return None
        try:
            resp = requests.get(
                current,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": accept,
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            logger.info("link preview: fetch failed for %s (%s)", current, exc)
            return None

        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location") or ""
                if not location:
                    return None
                current = urljoin(current, location)
                continue
            if resp.status_code != 200:
                return None

            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if want_image and not ctype.startswith("image/"):
                return None
            if want_image and ctype == "image/svg+xml":
                return None  # SVG is script-capable; never proxy it

            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                return None

            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
            body = b"".join(chunks)
        finally:
            resp.close()

        if not body:
            return None
        return body, (ctype or ("image/jpeg" if want_image else "text/html"))

    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _cache_get(url: str):
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(url)
        if entry is None:
            return False, None
        expires_at, value = entry
        if expires_at < now:
            _CACHE.pop(url, None)
            return False, None
        return True, value


def _cache_put(url: str, value: tuple[bytes, str] | None) -> None:
    ttl = _CACHE_TTL_OK if value else _CACHE_TTL_MISS
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            # Drop whatever expires soonest; good enough for a cache this size.
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            _CACHE.pop(oldest, None)
        _CACHE[url] = (time.time() + ttl, value)


def clear_cache() -> None:
    """Drop every cached preview. Used by tests."""
    with _CACHE_LOCK:
        _CACHE.clear()


def fetch_thumbnail(url: str) -> tuple[bytes, str] | None:
    """The preview image for `url` as (bytes, content_type), or None.

    None means "this link has no usable preview" — the caller should render
    its placeholder rather than an error.
    """
    url = (url or "").strip()
    if not is_previewable_url(url):
        return None

    hit, cached = _cache_get(url)
    if hit:
        return cached

    if not _FETCH_SLOTS.acquire(timeout=_SLOT_WAIT):
        # Busy right now — don't cache this as "no preview", so the card
        # picks the thumbnail up on the next load.
        logger.info("link preview: fetch slots busy, skipping %s", url)
        return None

    result = None
    try:
        candidates = _direct_thumbnail_urls(url)
        scraped = ""
        if not candidates:
            scraped = _scrape_og_image(url)
            if scraped:
                candidates = [scraped]
        for candidate in candidates:
            result = _get_bytes(
                candidate,
                accept="image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8",
                max_bytes=_MAX_IMAGE_BYTES,
                want_image=True,
            )
            if result:
                break
        # A known host whose direct thumbnail didn't pan out (a Drive file
        # that isn't link-shared, say) still gets the generic treatment.
        if result is None and not scraped:
            scraped = _scrape_og_image(url)
            if scraped:
                result = _get_bytes(
                    scraped,
                    accept="image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8",
                    max_bytes=_MAX_IMAGE_BYTES,
                    want_image=True,
                )
    except Exception as exc:  # never let a preview break the chat
        logger.warning("link preview failed for %s: %s", url, exc)
        result = None
    finally:
        _FETCH_SLOTS.release()

    _cache_put(url, result)
    return result

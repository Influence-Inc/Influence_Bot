"""
The creator's two personal submission pages on the campaigns site.

Every creator on a campaign has exactly two places they ever need to go:

  - **submit for review** — hand a draft (or a revised draft) to the brand
  - **submit posts** — share the live post link(s) once it's published

Both are minted by ``creatorSubmissionLinks()`` on the campaigns site and
are unique per (creator, campaign), carrying a submission token when the
username is ambiguous. They reach us two ways:

1. On the ``review_submitted`` / ``video_links_submitted`` webhook, as
   ``creator.submissionLinks``. That's the cheap path.
2. From ``GET /api/bot/campaigns``, under ``creators[].submissionLinks``.
   That's the fallback for spaces opened before the webhook carried them.

Resolution is cache-first (the columns on ChatSpace), then the API, then
None — and writes back whatever the API returned so the next read is free.
A caller that gets None must render nothing at all: a dead link in a
creator's face is worse than no link, which is the same rule the approval
email already follows.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from models.models import ChatSpace, SessionLocal
from services.reelstats_api import ReelStatsAPI

logger = logging.getLogger(__name__)

# A lookup that came back empty is remembered for a while so a space the
# API has nothing for doesn't re-hit it on every render. The ReelStats
# client allows 30s per call, and `for_space` sits on the chat page's
# render path — without this, one creator missing from the API would make
# their chat crawl. Successful lookups don't need an entry: they're
# written to the space's own columns and never fetched again.
_MISS_TTL_SECONDS = 600.0
_misses: dict[tuple[str, str], float] = {}


@dataclass(frozen=True)
class SubmissionLinks:
    """The pair of URLs, either side of which may be missing."""

    submit_for_review_url: Optional[str] = None
    submit_posts_url: Optional[str] = None

    def __bool__(self) -> bool:
        return bool(self.submit_for_review_url or self.submit_posts_url)

    @property
    def complete(self) -> bool:
        return bool(self.submit_for_review_url and self.submit_posts_url)


EMPTY = SubmissionLinks()


def _clean(value) -> Optional[str]:
    """Keep only an absolute http(s) URL; anything else is not a link."""
    text = (value or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return None
    return text


def from_payload(creator: Optional[dict]) -> SubmissionLinks:
    """Read `creator.submissionLinks` off a webhook payload."""
    links = (creator or {}).get("submissionLinks") or {}
    if not isinstance(links, dict):
        return EMPTY
    return SubmissionLinks(
        submit_for_review_url=_clean(links.get("submitForReviewUrl")),
        submit_posts_url=_clean(links.get("submitPostsUrl")),
    )


def _miss_key(campaign_slug: str, creator_username: str) -> tuple[str, str]:
    return (campaign_slug, creator_username.lower().lstrip("@"))


def _recently_missed(key: tuple[str, str]) -> bool:
    seen_at = _misses.get(key)
    if seen_at is None:
        return False
    if time.monotonic() - seen_at < _MISS_TTL_SECONDS:
        return True
    _misses.pop(key, None)
    return False


def fetch_creator(
    campaign_slug: Optional[str],
    creator_username: Optional[str],
    *,
    use_miss_cache: bool = True,
) -> Optional[dict]:
    """
    This creator's record on this campaign from the live ReelStats API.

    The shared primitive behind every lookup here: it carries the
    submission links, and also `deliverables`, which is what
    services/creator_next_step.py reads to learn how many of the
    creator's videos already have their post links logged.

    Returns None on any failure — missing slug/username, network error,
    no campaign match, no creator match. Callers treat that as "we don't
    know", never as an error worth surfacing.

    `use_miss_cache=False` forces the call even if a recent lookup came
    back empty, for the once-per-event paths where latency is fine and
    being right matters more.
    """
    if not campaign_slug or not creator_username:
        return None
    key = _miss_key(campaign_slug, creator_username)
    if use_miss_cache and _recently_missed(key):
        return None
    try:
        campaigns = ReelStatsAPI().get_campaigns()
    except Exception as exc:
        logger.warning("ReelStats creator lookup failed: %s", exc)
        _misses[key] = time.monotonic()
        return None
    target_user = key[1]
    for campaign in campaigns:
        if campaign.get("slug") != campaign_slug:
            continue
        for creator in campaign.get("creators", []):
            uname = (creator.get("username") or "").lower().lstrip("@")
            if uname == target_user:
                _misses.pop(key, None)
                return creator
    _misses[key] = time.monotonic()
    return None


def fetch_from_api(
    campaign_slug: Optional[str],
    creator_username: Optional[str],
    *,
    use_miss_cache: bool = True,
) -> SubmissionLinks:
    """
    Look up both URLs from the live ReelStats API for this (creator,
    campaign). EMPTY when we can't, which callers render as "no link".
    """
    creator = fetch_creator(
        campaign_slug, creator_username, use_miss_cache=use_miss_cache
    )
    return from_payload(creator) if creator is not None else EMPTY


def posts_logged_from_payload(creator: Optional[dict]) -> Optional[int]:
    """
    How many of this creator's videos already have their post links
    logged, per the campaigns site: `deliverables.actualVideos`, which is
    `videos.filter(v => v.hasLinks).length` at the source.

    None when the record doesn't carry a usable number — the caller must
    not read that as zero, which would claim the creator has posted
    nothing.
    """
    if not creator:
        return None
    deliverables = creator.get("deliverables")
    if not isinstance(deliverables, dict):
        return None
    value = deliverables.get("actualVideos")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def remember_posts_logged(chat_space_id: int, count: Optional[int]) -> None:
    """
    Cache the authoritative post-links count on the space.

    Never raises and never writes None: not knowing has to stay
    distinguishable from knowing the answer is zero, or a failed lookup
    would permanently claim the creator has posted nothing.
    """
    if not chat_space_id or count is None or count < 0:
        return
    db = SessionLocal()
    try:
        space = db.query(ChatSpace).get(chat_space_id)
        if space is None or space.posts_logged == count:
            return
        space.posts_logged = count
        db.commit()
    except Exception as exc:
        logger.warning(
            "Could not cache posts_logged on chat space %s: %s", chat_space_id, exc
        )
    finally:
        db.close()


def refresh_posts_logged(space) -> Optional[int]:
    """
    Re-read the count from the campaigns site and cache it.

    Called when a `video_links_submitted` webhook says it changed — the
    only thing that moves this number — so the chat's render path never
    has to. Returns the new count, or None if we couldn't find out.
    """
    if space is None:
        return None
    creator = fetch_creator(
        space.campaign_slug, space.creator_username, use_miss_cache=False
    )
    count = posts_logged_from_payload(creator)
    if count is None:
        return None
    remember_posts_logged(space.id, count)
    return count


def remember(
    chat_space_id: int, links: SubmissionLinks, *, overwrite: bool = False
) -> None:
    """
    Cache what we know onto the chat space.

    Only fills blanks by default: a URL already stored came from a webhook
    for this exact creator, and re-minted URLs are equivalent, so there's
    nothing to gain by churning the row. Never raises — caching is an
    optimisation, and losing it must not fail the caller's real work.
    """
    if not links or not chat_space_id:
        return
    db = SessionLocal()
    try:
        space = db.query(ChatSpace).get(chat_space_id)
        if space is None:
            return
        changed = False
        for attr, value in (
            ("submit_for_review_url", links.submit_for_review_url),
            ("submit_posts_url", links.submit_posts_url),
        ):
            if value and (overwrite or not getattr(space, attr, None)):
                setattr(space, attr, value)
                changed = True
        if changed:
            db.commit()
    except Exception as exc:
        logger.warning(
            "Could not cache submission links on chat space %s: %s",
            chat_space_id, exc,
        )
    finally:
        db.close()


def for_space(space: ChatSpace, *, allow_fetch: bool = True) -> SubmissionLinks:
    """
    Resolve both URLs for a chat space: cache, then API, then nothing.

    `allow_fetch=False` keeps this to the cached columns — for request
    paths that must not block on an outbound HTTP call.
    """
    if space is None:
        return EMPTY
    cached = SubmissionLinks(
        submit_for_review_url=_clean(getattr(space, "submit_for_review_url", None)),
        submit_posts_url=_clean(getattr(space, "submit_posts_url", None)),
    )
    if cached.complete or not allow_fetch:
        return cached

    fetched = fetch_from_api(space.campaign_slug, space.creator_username)
    if not fetched:
        return cached
    remember(space.id, fetched)
    # Prefer what we just fetched, but never lose a cached side the API
    # didn't return.
    merged = SubmissionLinks(
        submit_for_review_url=(
            fetched.submit_for_review_url or cached.submit_for_review_url
        ),
        submit_posts_url=fetched.submit_posts_url or cached.submit_posts_url,
    )
    # Keep the in-memory (detached) space consistent with what we stored, so
    # a caller holding it doesn't re-fetch.
    try:
        space.submit_for_review_url = merged.submit_for_review_url
        space.submit_posts_url = merged.submit_posts_url
    except Exception:  # pragma: no cover - detached instance edge case
        pass
    return merged

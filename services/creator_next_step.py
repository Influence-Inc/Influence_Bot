"""
What is it the creator's turn to do?

The review chat is a state machine about one campaign's drafts, and at any
moment at most one of the creator's two submission pages is the right
place to send them. This module is the single answer to that question —
every surface in the chat (the strip above the composer, the action row
under a decision, the header status) renders the same `NextStep`, so the
creator is never offered a menu of destinations and the UI can't drift
out of step with itself.

One video moves through a fixed pipeline — submit a draft, wait for the
brand, revise if asked, post it once approved, share the links — and the
campaign repeats that until the deliverables are met. The question is
never "how much is left overall", it is "whose move is it, now", answered
in order, first match wins:

  ``resubmit_draft``  The newest draft came back with changes requested.
  ``submit_posts``    Fewer videos are live than have been approved, so an
                      approved draft is still waiting to go up.
  *(nothing)*         A draft is under review. Whatever the campaign still
                      wants, the creator cannot act until the brand comes
                      back.
  ``submit_draft``    Nothing is in flight and the campaign still wants a
                      video — either the creator has sent nothing at all,
                      or everything they sent is approved and posted.
  *(nothing)*         Everything sent is done and nothing more is owed, or
                      the space is archived.

The order is the whole design. An earlier version asked whether the
campaign's video target had been met, counting drafts under review towards
it, and so asked a creator to start their second draft while their first
was still sitting unapproved with the brand — work they might have to
throw away. Wanting more videos eventually is not the same as it being
the creator's move, and only the pipeline position can tell them apart.

Because anything unfinished matches an earlier branch, the deliverable
check at the end needs no accounting: by then every draft the creator has
sent is approved and posted, so the only question left is whether the
campaign wants another.

Which drafts exist and what the brand decided is ours to know, and read
locally. Whether the creator has already posted is not: the campaigns
site owns that, as `deliverables.actualVideos`. We ask it, and cache the
number on the space, so the render path stays local without inventing an
answer.

An earlier version inferred posting from our own `posts_submitted`
notices instead. Those only exist from this feature's first deploy
onward, so a creator who had logged their links before it shipped was
asked for them again, indefinitely — the notice that would have retired
the step was never going to arrive. Counting a fact we don't own was the
mistake; the notices are still written, but they're a floor for when the
API can't be reached, not the ledger.

Approvals and postings are compared as counts, not paired off. Nothing
records which approval a given post fulfils — `videos[]` and `reviews[]`
on the campaigns site share no field — so any pairing would be a guess
dressed up as bookkeeping. Fewer videos live than drafts approved means
one is still waiting to go up, and that is all this needs to know.

A URL we can't resolve means no step at all, rather than a button that
goes nowhere — the same rule the approval email follows.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func

from models.models import ChatMessage, ChatSpace, ReviewSubmission, SessionLocal
from services import submission_links

logger = logging.getLogger(__name__)

KEY_SUBMIT_DRAFT = "submit_draft"
KEY_RESUBMIT_DRAFT = "resubmit_draft"
KEY_SUBMIT_POSTS = "submit_posts"

# URL path segment -> step key, for /chat/<slug>/go/<segment>.
ROUTE_KEYS = {
    "submit-draft": KEY_SUBMIT_DRAFT,
    "resubmit-draft": KEY_RESUBMIT_DRAFT,
    "submit-posts": KEY_SUBMIT_POSTS,
}

CHANGES_REQUESTED = "changes_requested"
APPROVED = "approved"


@dataclass(frozen=True)
class NextStep:
    """The one thing the creator can do next, and where it happens."""

    key: str
    # The action itself, in the creator's words.
    label: str
    # One line of why it's showing, e.g. "Approved 2 days ago".
    detail: str
    # Where /chat/<slug>/go/<route> sends them.
    route: str
    # Set when the step answers a specific draft, so the feed can anchor the
    # action to that draft's decision instead of repeating it on every one.
    review_id: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "detail": self.detail,
            "route": self.route,
            "review_id": self.review_id,
        }


_ROUTE_FOR_KEY = {v: k for k, v in ROUTE_KEYS.items()}


def _drafts_for_space(db, space: ChatSpace) -> list[ReviewSubmission]:
    """
    Every draft this creator has sent on this campaign, oldest first,
    minus the ones the INFLUENCE team took out of play.

    Scoped by campaign the same way `review_coverage` does it — slug when
    we have one, name otherwise — so another brand's campaign can't put a
    step in this conversation.
    """
    username = (space.creator_username or "").strip().lstrip("@")
    if not username:
        return []
    query = db.query(ReviewSubmission).filter(
        func.lower(ReviewSubmission.creator_username) == username.lower()
    )
    if space.campaign_slug:
        query = query.filter(ReviewSubmission.campaign_slug == space.campaign_slug)
    elif space.campaign_name:
        query = query.filter(ReviewSubmission.campaign_name == space.campaign_name)
    rows = query.order_by(ReviewSubmission.id.asc()).all()
    return [r for r in rows if not r.ignored]


def _event_count(db, *, chat_space_id: int, kind: str) -> int:
    """How many events of this kind the space holds."""
    row = (
        db.query(func.count(ChatMessage.id))
        .filter(
            ChatMessage.chat_space_id == chat_space_id,
            ChatMessage.kind == kind,
        )
        .scalar()
    )
    return int(row or 0)


# How long to leave the campaigns site alone between checks on one space.
# Only consulted when we are otherwise about to ask a creator for links,
# so this throttles a rare call rather than a common one.
_RECHECK_SECONDS = 300.0
_last_checked: dict[int, float] = {}


def _due_a_recheck(chat_space_id: int) -> bool:
    last = _last_checked.get(chat_space_id)
    if last is not None and time.monotonic() - last < _RECHECK_SECONDS:
        return False
    _last_checked[chat_space_id] = time.monotonic()
    return True


def _progress(db, space: ChatSpace, *, approved_count: int) -> tuple[int, Optional[int]]:
    """
    `(videos with links logged, videos the campaign requires)`.

    Both come off the same creator record, so they're read together. See
    `_posts_logged` for the caching rules; `videos_required` is
    `deliverables.minVideos`, and None — no target set — is a real answer
    that keeps us quiet rather than one to keep re-asking about.

    Certainty is wanted up to whichever is larger, the approvals awaiting
    links or the campaign's own target. Both can put a step on screen, so
    a count good enough to settle one can still be too stale to settle the
    other — which is how a creator who had posted everything was told
    "2 of 3".
    """
    cached_required = getattr(space, "videos_required", None) or 0
    posts = _posts_logged(db, space, needed=max(approved_count, cached_required))
    # `_posts_logged` refreshes the target in place when it fetches.
    return posts, getattr(space, "videos_required", None)


def _posts_logged(db, space: ChatSpace, *, needed: int) -> int:
    """
    How many of this creator's videos on this campaign already have their
    live post links logged — enough of an answer to decide whether to ask
    for `needed` of them.

    The campaigns site owns this fact (`deliverables.actualVideos`), so we
    ask it rather than inferring one from our own feed, and cache the
    number on the space. Our own `posts_submitted` notices are a floor
    under that: they only start at this feature's first deploy, so they
    undercount every space that predates it, but they can never overcount.

    The API is consulted only when what we already know isn't enough —
    that is, only when the alternative is asking a creator for links they
    may have given us already. Being certain is worth a call at that
    moment; when the count is plainly sufficient, nothing is fetched.
    """
    from services import chat_service
    from services import submission_links

    local = _event_count(
        db, chat_space_id=space.id, kind=chat_service.KIND_POSTS_SUBMITTED
    )
    cached = getattr(space, "posts_logged", None)
    known = local if cached is None else max(int(cached), local)
    # A successful lookup always writes `posts_logged`, so NULL means we
    # have never synced — and until we do we don't know what the campaign
    # asks of this creator either.
    synced = cached is not None
    if (known >= needed and synced) or not _due_a_recheck(space.id):
        return known

    creator = submission_links.fetch_creator(
        space.campaign_slug, space.creator_username
    )
    count = submission_links.posts_logged_from_payload(creator)
    if count is None:
        # Couldn't find out. Fall back to what we had rather than
        # inventing a number in either direction.
        return known
    required = submission_links.videos_required_from_payload(creator)
    submission_links.remember_progress(space.id, count, required)
    try:
        space.posts_logged = count
        space.videos_required = required
    except Exception:  # pragma: no cover - detached instance edge case
        pass
    return max(count, local)


def resolve(space: ChatSpace) -> Optional[NextStep]:
    """
    The creator's open action on this chat space, or None.

    One video moves through a fixed pipeline: submit a draft, wait for the
    brand, revise if asked, post it once approved, share the links. The
    campaign repeats that until the deliverables are met. So the question
    is never "how much is left overall" but "whose move is it, now" —
    answered in order, first match wins.

    Pure read: never posts, never mutates anything but the cached columns
    on the space.
    """
    if space is None or space.status == "archived":
        return None

    db = SessionLocal()
    try:
        drafts = _drafts_for_space(db, space)
        latest = drafts[-1] if drafts else None
        approved = [d for d in drafts if d.decision == APPROVED]
        with_the_brand = [d for d in drafts if d.decision is None]
        posts_logged, videos_required = _progress(
            db, space, approved_count=len(approved)
        )
    except Exception as exc:
        # A next step is a convenience on top of the conversation. If we
        # can't work one out, the chat still has to render.
        logger.warning(
            "Could not resolve next step for chat space %s: %s", space.id, exc
        )
        return None
    finally:
        db.close()

    if latest is not None and latest.decision == CHANGES_REQUESTED:
        # The brand sent the newest draft back.
        step = NextStep(
            key=KEY_RESUBMIT_DRAFT,
            label="Send your revised draft",
            detail="The brand asked for changes",
            route=_ROUTE_FOR_KEY[KEY_RESUBMIT_DRAFT],
            review_id=latest.id,
        )
    elif len(approved) > posts_logged:
        # A draft has been approved and fewer videos are live than have
        # been approved, so one of them is still waiting to go up. This
        # outranks a draft sitting with the brand: posting an approved
        # video is the creator's to do either way.
        step = NextStep(
            key=KEY_SUBMIT_POSTS,
            label="Add your live post links",
            detail="Approved — share the links once it's up",
            route=_ROUTE_FOR_KEY[KEY_SUBMIT_POSTS],
            review_id=approved[-1].id,
        )
    elif with_the_brand:
        # A draft is under review. Whatever the campaign still wants, the
        # creator cannot act until the brand comes back — asking for the
        # next draft here is asking them to work ahead of a decision that
        # might send this one back.
        return None
    elif not drafts:
        step = NextStep(
            key=KEY_SUBMIT_DRAFT,
            label="Submit your draft",
            detail="Nothing is with the brand yet",
            route=_ROUTE_FOR_KEY[KEY_SUBMIT_DRAFT],
        )
    elif videos_required and posts_logged < videos_required:
        # Nothing is in flight — every draft sent is approved and posted —
        # and the campaign still wants a video. That one is theirs to
        # start. No accounting is needed here: anything unfinished would
        # have matched a branch above.
        step = NextStep(
            key=KEY_SUBMIT_DRAFT,
            label="Submit your next draft",
            detail=f"{posts_logged} of {videos_required} videos posted",
            route=_ROUTE_FOR_KEY[KEY_SUBMIT_DRAFT],
        )
    else:
        # Everything sent is done and nothing more is owed.
        return None

    # Last gate: a step we can't send anyone to isn't a step.
    if not url_for_step(space, step.key):
        return None
    return step


def url_for_step(space: ChatSpace, key: str) -> Optional[str]:
    """The campaigns-site URL a step key resolves to for this space."""
    links = submission_links.for_space(space)
    if key == KEY_SUBMIT_POSTS:
        return links.submit_posts_url
    if key in (KEY_SUBMIT_DRAFT, KEY_RESUBMIT_DRAFT):
        return links.submit_for_review_url
    return None

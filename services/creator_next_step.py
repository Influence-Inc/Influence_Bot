"""
What is it the creator's turn to do?

The review chat is a state machine about one campaign's drafts, and at any
moment at most one of the creator's two submission pages is the right
place to send them. This module is the single answer to that question —
every surface in the chat (the strip above the composer, the action row
under a decision, the header status) renders the same `NextStep`, so the
creator is never offered a menu of destinations and the UI can't drift
out of step with itself.

The states, in priority order:

  ``resubmit_draft``  The newest draft came back with changes requested.
                      The ball is squarely with the creator.
  ``submit_posts``    A draft was approved and its live post links haven't
                      landed yet. Nothing else can happen until they do.
  ``submit_draft``    Nothing is in play at all (every draft the creator
                      sent was ignored by the INFLUENCE team).
  *(nothing)*         A draft is sitting with the brand, everything is
                      posted, or the space is archived. Rendering nothing
                      is a real outcome, not a failure.

Priority matters on campaigns with several deliverables, where a creator
can owe a revision on draft 3 while draft 1 is approved and unposted.
Whatever the brand is waiting on wins; the other state is mentioned in
the detail line, never as a second button. That rule is what stops this
becoming a permanent toolbar.

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
    if known >= needed or not _due_a_recheck(space.id):
        return known

    creator = submission_links.fetch_creator(
        space.campaign_slug, space.creator_username
    )
    count = submission_links.posts_logged_from_payload(creator)
    if count is None:
        # Couldn't find out. Fall back to what we had rather than
        # inventing a number in either direction.
        return known
    submission_links.remember_posts_logged(space.id, count)
    try:
        space.posts_logged = count
    except Exception:  # pragma: no cover - detached instance edge case
        pass
    return max(count, local)


def _approval_awaiting_posts(db, space: ChatSpace, drafts) -> Optional[ReviewSubmission]:
    """
    The approved draft whose live post links we're still waiting on, if any.

    Counted, not paired: N approvals against M videos with links logged.
    An earlier version compared the newest decision notice against the
    newest posts notice, which couldn't express which approval had been
    answered — any later decision, including a changes-requested on a
    different draft, put the posts notice "behind" again and reopened a
    step the creator had already done.
    """
    approved = [d for d in drafts if d.decision == APPROVED]
    if not approved:
        return None

    posts_logged = _posts_logged(db, space, needed=len(approved))
    if posts_logged >= len(approved):
        # Every approved draft is accounted for.
        return None
    # The oldest approval nothing has answered yet.
    return approved[min(posts_logged, len(approved) - 1)]


def resolve(space: ChatSpace) -> Optional[NextStep]:
    """
    The creator's open action on this chat space, or None.

    Pure read: never posts, never mutates anything but the cached
    submission-link columns (via `submission_links.for_space`).
    """
    if space is None or space.status == "archived":
        return None

    db = SessionLocal()
    try:
        drafts = _drafts_for_space(db, space)
        latest = drafts[-1] if drafts else None
        awaiting_posts = _approval_awaiting_posts(db, space, drafts)
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
        step = NextStep(
            key=KEY_RESUBMIT_DRAFT,
            label="Send your revised draft",
            detail="The brand asked for changes",
            route=_ROUTE_FOR_KEY[KEY_RESUBMIT_DRAFT],
            review_id=latest.id,
        )
    elif awaiting_posts is not None:
        step = NextStep(
            key=KEY_SUBMIT_POSTS,
            label="Add your live post links",
            detail="Approved — share the links once it's up",
            route=_ROUTE_FOR_KEY[KEY_SUBMIT_POSTS],
            review_id=awaiting_posts.id,
        )
    elif not drafts:
        step = NextStep(
            key=KEY_SUBMIT_DRAFT,
            label="Submit your draft",
            detail="Nothing is with the brand yet",
            route=_ROUTE_FOR_KEY[KEY_SUBMIT_DRAFT],
        )
    else:
        # A draft is with the brand and everything approved has been
        # posted. It isn't the creator's move.
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

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

Evidence is local — our own `review_submissions` rows plus the event
messages in the chat — so resolving a next step never blocks on an
outbound call. Two consequences worth knowing:

- Whether the creator has shared their live post links is read from the
  ``posts_submitted`` event the `video_links_submitted` webhook writes
  into the chat. Those only exist from this feature's first deploy
  onward, so a draft approved before it shows the step until the creator
  submits (which records the event and clears it) or the campaign ends
  and the space is archived. Self-healing, and the page they land on
  shows the slots they already filled.
- A URL we can't resolve means no step at all, rather than a button that
  goes nowhere — the same rule the approval email follows.
"""

from __future__ import annotations

import logging
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


def _latest_event_id(db, *, chat_space_id: int, kind: str) -> int:
    """Id of the newest event of this kind in the space, or 0."""
    row = (
        db.query(func.max(ChatMessage.id))
        .filter(
            ChatMessage.chat_space_id == chat_space_id,
            ChatMessage.kind == kind,
        )
        .scalar()
    )
    return int(row or 0)


def _approval_awaiting_posts(db, space: ChatSpace, drafts) -> Optional[ReviewSubmission]:
    """
    The approved draft whose live post links we're still waiting on, if any.

    Compares message ids rather than timestamps: both the approval notice
    and the posts notice are rows in this space's feed, so their order is
    exact and immune to clock skew between us and the campaigns site.
    """
    approved = [d for d in drafts if d.decision == APPROVED]
    if not approved:
        return None

    # Import here rather than at module scope: chat_service imports this
    # module for the event payloads it attaches to decisions.
    from services import chat_service

    approval_event_id = _latest_event_id(
        db, chat_space_id=space.id, kind=chat_service.KIND_REVIEW_DECISION
    )
    posts_event_id = _latest_event_id(
        db, chat_space_id=space.id, kind=chat_service.KIND_POSTS_SUBMITTED
    )
    if posts_event_id and posts_event_id > approval_event_id:
        # They've shared links since the last thing the brand decided.
        return None
    return approved[-1]


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

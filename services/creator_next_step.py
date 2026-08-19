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
  ``submit_draft``    The campaign still wants a video that nothing in
                      flight will produce — either the creator has sent
                      nothing at all, or they've finished a deliverable
                      and the next one is theirs to start.
  *(nothing)*         A draft is sitting with the brand and nothing more
                      is owed, or the space is archived. Rendering nothing
                      is a real outcome, not a failure.

Priority matters on campaigns with several deliverables, where a creator
can owe a revision on draft 3 while draft 1 is approved and unposted.
Whatever the brand is waiting on wins; the other state is mentioned in
the detail line, never as a second button. That rule is what stops this
becoming a permanent toolbar.

Finishing one deliverable is not finishing the campaign. A creator whose
first video is approved and posted, with two more still owed, is looking
at an empty chat unless the next draft is asked for — so `submit_draft`
is counted against what the campaign wants, not just against whether
anything has ever been sent.

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

Which approval a given post fulfils is not recorded anywhere: `videos[]`
and `reviews[]` on the campaigns site share no field, so a post cannot be
traced back to the draft it came from. Approvals are matched to postings
by order instead — see `_unanswered_approval`. That is enough to stop a
video which skipped review from cancelling out an approval that still
owes its links, but it is an approximation, and a genuine pairing would
have to start with a link between the two on the campaigns site.

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


def _decision_event_ids(db, chat_space_id: int) -> dict:
    """`{review_id: message id}` for the approval notices in this space."""
    from services import chat_service

    rows = (
        db.query(ChatMessage.review_id, func.max(ChatMessage.id))
        .filter(
            ChatMessage.chat_space_id == chat_space_id,
            ChatMessage.kind == chat_service.KIND_REVIEW_DECISION,
            ChatMessage.review_id.isnot(None),
        )
        .group_by(ChatMessage.review_id)
        .all()
    )
    return {int(review_id): int(msg_id) for review_id, msg_id in rows}


def _posting_event_ids(db, chat_space_id: int) -> list:
    """Ids of this space's post-links notices, oldest first."""
    from services import chat_service

    rows = (
        db.query(ChatMessage.id)
        .filter(
            ChatMessage.chat_space_id == chat_space_id,
            ChatMessage.kind == chat_service.KIND_POSTS_SUBMITTED,
        )
        .order_by(ChatMessage.id.asc())
        .all()
    )
    return [int(r[0]) for r in rows]


def _unanswered_approval(db, space: ChatSpace, approved, posts_logged: int):
    """
    The oldest approved draft whose live post links we're still waiting on.

    Bare totals can't answer this. A creator who posts a video whose draft
    never went through the review page inflates the posted count, and that
    extra post silently cancels out an approved draft that genuinely still
    owes its links.

    Nothing in the data pairs a post with the approval it fulfils —
    `videos[]` and `reviews[]` on the campaigns site share no field — so
    this uses the next best thing we do own: order. A post-links notice can
    only answer an approval that came *before* it, and each notice answers
    at most one. An approval with nothing recorded after it is outstanding
    however high the global total climbs.

    Order only reaches back as far as the notices do. For approvals decided
    before this space recorded its first one there is no per-post evidence
    at all, and the campaigns site's total is the only thing to go on —
    which is what keeps spaces that predate the notices from being asked
    for links they gave long ago.
    """
    if not approved:
        return None

    decision_ids = _decision_event_ids(db, space.id)
    postings = _posting_event_ids(db, space.id)

    # Walk the approvals oldest first, letting each consume the earliest
    # notice recorded after it that no earlier approval has taken.
    used = 0
    unanswered = []
    for approval in approved:
        decided_at = decision_ids.get(approval.id)
        if decided_at is not None:
            while used < len(postings) and postings[used] < decided_at:
                used += 1
        if used < len(postings):
            used += 1
        else:
            unanswered.append(approval)

    if not unanswered:
        return None

    first_posting = postings[0] if postings else None
    for approval in unanswered:
        decided_at = decision_ids.get(approval.id)
        modern = (
            first_posting is not None
            and decided_at is not None
            and decided_at > first_posting
        )
        if modern:
            # Recorded evidence covers this one, and none of it answers it.
            return approval

    # Only pre-notice approvals are left, and the site's own total is all we
    # have for them. It says how many are posted but not which, so assume
    # the oldest went up first — the order creators actually work in.
    answered_by_notices = len(approved) - len(unanswered)
    covered = max(0, posts_logged - answered_by_notices)
    if covered >= len(unanswered):
        return None
    return unanswered[covered]


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


def _owes_another_draft(
    *,
    drafts,
    approved_count: int,
    posts_logged: int,
    videos_required: Optional[int],
) -> bool:
    """
    Does the creator still owe a draft the campaign hasn't seen?

    Counted against what the campaign asks for: everything already posted,
    plus everything on its way there — drafts sitting with the brand, and
    approved drafts whose links haven't landed yet. Falling short of
    `videos_required` means one more still has to be made.

    Without a target (`minVideos` unset) there is no shortfall to measure,
    so the honest answer is no. Better to say nothing than to invent a
    deliverable the campaign never asked for.
    """
    if not videos_required:
        return False
    with_the_brand = sum(1 for d in drafts if d.decision is None)
    approved_unposted = max(0, approved_count - posts_logged)
    accounted = posts_logged + with_the_brand + approved_unposted
    return accounted < videos_required


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
        approved = [d for d in drafts if d.decision == APPROVED]
        posts_logged, videos_required = _progress(
            db, space, approved_count=len(approved)
        )
        awaiting_posts = _unanswered_approval(db, space, approved, posts_logged)
        owes_another = _owes_another_draft(
            drafts=drafts,
            approved_count=len(approved),
            posts_logged=posts_logged,
            videos_required=videos_required,
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
    elif owes_another:
        step = NextStep(
            key=KEY_SUBMIT_DRAFT,
            label="Submit your next draft",
            detail=f"{posts_logged} of {videos_required} videos posted",
            route=_ROUTE_FOR_KEY[KEY_SUBMIT_DRAFT],
        )
    else:
        # A draft is with the brand, everything approved has been posted,
        # and nothing is still owed. It isn't the creator's move.
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

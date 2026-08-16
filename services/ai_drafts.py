"""
AI-drafted replies for the admin side of the content-review chat.

Jennifer (party ``admin``) sits in the middle of every creator <-> brand review
chat: she keeps brand feedback specific, keeps the creator moving toward a
resubmission, and unblocks whatever is stalling the review. `draft_replies`
reads the recent transcript of one chat space and asks Claude for a few
sendable options in that voice.

It works two ways. With no instruction it suggests replies off the
conversation. With one — the admin types the point they want to get across in
shorthand — it writes that point in her voice, and the transcript is only
context.

Nothing is ever posted automatically. The admin UI puts drafts in an editable
sheet, the chosen one lands in the composer, and it goes out through the normal
`/chat/<slug>/messages` endpoint like anything the admin typed by hand.

Unset `ANTHROPIC_API_KEY` disables the feature: `is_configured()` is False, the
composer renders without the draft button, and the endpoint answers 503.
"""

from __future__ import annotations

import json
import logging
import re

from config import Config
from services import chat_service

logger = logging.getLogger(__name__)


DEFAULT_DRAFTS = 3
MAX_DRAFTS = 4

# How far back the transcript is read before tail-slicing to the context
# window. Review chats are short; this is a safety valve, not a page size.
_FETCH_LIMIT = 500
# Per-message truncation, so one pasted wall of text can't crowd out the rest
# of the conversation. Generous enough to keep Jennifer's own long feedback
# messages intact — they're the voice the model is matching.
_BODY_CHARS = 1000
# Her feedback messages legitimately run several paragraphs; past this it's the
# model rambling rather than a chat message.
_DRAFT_CHARS = 2000
# The admin's typed note. Long enough to paste a few sentences of intent.
_INSTRUCTION_CHARS = 1500


class DraftError(RuntimeError):
    """A draft couldn't be produced.

    ``code`` is a stable machine-readable reason (the HTTP route maps it to a
    status); ``message`` is shown to the admin in the drafts sheet.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


SYSTEM_PROMPT = """\
You are drafting chat replies for Jennifer, who runs creator campaigns at INFLUENCE.

INFLUENCE is a creator-marketing agency. Each campaign gets a three-way chat \
space where a creator, the brand, and Jennifer review drafts together — videos, \
carousels, whatever the campaign called for. Creators submit drafts, brands \
approve them or request changes, and Jennifer keeps that loop moving.

You are given the campaign details and the recent transcript. Write options for \
Jennifer's next message in the chat.

Her voice is the thing to get right. It is warm, generous and unmistakably a \
person, never a ticket system:

- Open with a greeting and the creator's first name — "Hi " and the name earlier \
messages in the thread use for them. If nobody has used a name yet, their \
@username is fine. Drop the greeting only when the reply is a quick beat in an \
active back-and-forth.
- Praise what is working before asking for anything, and thank them for the work \
— "Looks great :)", "This new draft looks so good! Thank you so much for making \
the revisions!"
- ":)" appears often, and emoji land where the moment fits (💗 😊 😅 🙏 🙌 ✨). \
Not every line, but she is not shy about them.
- Hyphens, not em dashes: "looks great - you're good to post this".
- Length follows the substance. An approval or a quick answer is a line or two. \
Real feedback runs longer: a warm opening line, the notes as a numbered or "- " \
list with one point each, then a closing line.
- Break it up the way she does — a blank line between paragraphs, each list item \
on its own line. Never run a whole message together as one block of text.
- Every ask is softened and every "no" is made easy — "Could you please…", "One \
optional tweak if you're up for it…", "if you prefer to post it as is, feel free \
- totally your call!". Extra rounds get an apology: "So sorry to be sending \
another round of edits - I promise this is the last one!"
- Each note says why, not just what — "so the results look even better", "since \
that filter looks more impressive".
- When something breaks on the creator's side, reassure first: "That's totally \
normal, nothing wrong on your end!", then explain plainly what is happening.
- She speaks for the agency as "we" and "on our end", takes timing pressure off \
("No rush at all!", "totally fine on our end"), and closes looking forward — \
"Can't wait to see the next draft! :)"

Substance:
- Say only what the transcript supports. Don't invent deadlines, rates, metrics, \
deliverable counts, reference links, or promises about what the brand will accept. \
Never write a URL that isn't already in the conversation.
- When a reply would need a fact you don't have, write it to ask for that fact \
rather than guessing it.
- Relay brand feedback as Jennifer's own ask, in her voice — a creator should \
never feel handed a complaint.
- Move the review forward: name the next action and who owns it.

Plain text only — no markdown bold or headings. Numbered lists and "- " bullets \
are part of how she writes longer notes.

Return options that take genuinely different approaches — a different next step, \
a different person addressed, a different level of push — not rewordings of one \
another.\
"""

# Structured outputs guarantee a parseable shape, so no regex salvage or
# retry-on-parse loop is needed around the response.
_OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "drafts": {
                "type": "array",
                "description": "Options for Jennifer's next chat message.",
                "items": {
                    "type": "string",
                    "description": (
                        "One complete message, ready to send. Multi-line: use "
                        "real line breaks between paragraphs and between list "
                        "items, with a blank line between paragraphs."
                    ),
                },
            },
        },
        "required": ["drafts"],
        "additionalProperties": False,
    },
}


def is_configured() -> bool:
    """True when the server has an Anthropic key, i.e. the feature is on."""
    return bool(Config.ANTHROPIC_API_KEY)


def _import_anthropic():
    try:
        import anthropic  # noqa: PLC0415 — optional dependency, imported on use
    except ImportError as exc:  # pragma: no cover - depends on the deployment
        raise DraftError(
            "sdk_missing",
            "The anthropic package isn't installed on the server.",
        ) from exc
    return anthropic


# ---------------------------------------------------------------------------
# Transcript context
# ---------------------------------------------------------------------------

def _speaker(msg: dict, space) -> str:
    party = msg.get("party")
    if party == "admin":
        return "Jennifer (you, INFLUENCE)"
    if party == "brand":
        sender = (msg.get("sender") or "").strip()
        brand = (space.brand_name or "the brand").strip()
        return f"{sender} ({brand}, brand)" if sender else f"{brand} (brand)"
    if party == "creator":
        return "@" + (space.creator_username or msg.get("sender") or "creator")
    return party or "system"


def _clip(text: str, limit: int = _BODY_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " […]"


def build_context(chat_space_id: int, *, limit: int | None = None) -> str:
    """Render the tail of a chat space as a labelled transcript for the model.

    Review events become bracketed stage directions ("[Draft 2 submitted]") so
    the model can tell a resubmission from someone talking about one, and every
    line names its speaker so it knows which side it is drafting for.
    """
    limit = limit or Config.AI_DRAFT_CONTEXT_MESSAGES
    space = chat_service.find_by_id(chat_space_id)
    if space is None:
        return ""
    messages = chat_service.list_messages(
        chat_space_id=chat_space_id, limit=_FETCH_LIMIT
    )
    lines: list[str] = []
    for msg in messages[-limit:]:
        kind = msg.get("kind") or "text"
        event = msg.get("event") or {}
        body = _clip(msg.get("body") or "")
        if kind == "review_submission":
            number = event.get("submission_number") or 1
            line = f"[Draft {number} submitted by @{space.creator_username}]"
            if event.get("video_link"):
                line += f" link: {event['video_link']}"
            if body:
                line += f" — creator's note: {body}"
            lines.append(line)
            continue
        if kind == "review_decision":
            number = event.get("submission_number") or 1
            decided = "approved" if event.get("decision") == "approved" else "sent back for changes"
            actor = (event.get("actor_name") or space.brand_name or "the brand").strip()
            lines.append(f"[Draft {number} {decided} by {actor}]")
            continue
        parts = []
        if body:
            parts.append(body)
        for att in msg.get("attachments") or []:
            parts.append(f"[image attached: {att.get('filename') or 'image'}]")
        if not parts:
            continue
        lines.append(f"{_speaker(msg, space)}: " + " ".join(parts))
    return "\n".join(lines)


def _user_prompt(space, context: str, instruction: str, count: int) -> str:
    header = [
        f"Campaign: {space.campaign_name or space.campaign_slug or 'unnamed'}",
        f"Brand: {space.brand_name or 'unnamed'}",
        f"Creator: @{space.creator_username}",
    ]
    parts = ["\n".join(header)]
    if context:
        parts.append("Recent messages, oldest first:\n" + context)
    else:
        parts.append(
            "The chat is empty — this would be Jennifer's opening message to "
            "the creator and the brand."
        )
    if instruction:
        # The admin typed what they want to get across, in shorthand. That is
        # the content of the message; the transcript above is only context and
        # tone. Saying so keeps the model from drifting back to a generic
        # transcript-driven reply and dropping half of what was asked for.
        parts.append(
            "What Jennifer wants this message to get across, in her own "
            "shorthand:\n"
            + _clip(instruction, _INSTRUCTION_CHARS)
            + "\n\nWrite that, in her voice. Cover all of it, keep her ordering "
            "where she gave one, and don't add asks she didn't make. Where the "
            "note is terse or uses shorthand, expand it into how she would "
            "actually say it."
        )
    parts.append(f"Write {count} options for Jennifer's next message.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    text = (text or "").strip()
    # The model occasionally wraps a message in quotes as if reporting it.
    if len(text) > 1 and text[0] in '"“' and text[-1] in '"”':
        text = text[1:-1].strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Some responses spell their line breaks out instead of writing them, which
    # would otherwise reach the bubble as a visible "\n". Only translate when
    # there are no real breaks to lose.
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\n", "\n")
    # One blank line is a paragraph break; more is slack.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:_DRAFT_CHARS].strip()


def _parse_drafts(response) -> list[str]:
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise DraftError("empty", "The draft was cut short. Try again.")
    text = next(
        (b.text for b in (response.content or []) if getattr(b, "type", None) == "text"),
        "",
    )
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise DraftError("empty", "Claude's reply wasn't in the expected format.") from exc
    items = payload.get("drafts") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise DraftError("empty", "Claude's reply wasn't in the expected format.")
    drafts: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        cleaned = _clean(item)
        if cleaned and cleaned not in drafts:
            drafts.append(cleaned)
    if not drafts:
        raise DraftError("empty", "Claude didn't return a usable draft. Try again.")
    return drafts


def draft_replies(
    *,
    chat_space_id: int,
    instruction: str = "",
    count: int = DEFAULT_DRAFTS,
) -> list[str]:
    """Return up to `count` sendable replies for the admin to choose from.

    `instruction` is what the admin typed into the draft sheet: the point they
    want the message to get across, in shorthand ("hook at 0:03, and we can
    wait for her AI credits, no rush"). When it's there it drives the content
    and the transcript is only context and tone; when it's empty the drafts are
    read off the conversation alone. Raises :class:`DraftError` on every
    failure path so the route can turn it into one status code and one message.
    """
    if not is_configured():
        raise DraftError("not_configured", "AI drafting isn't configured on the server.")
    space = chat_service.find_by_id(chat_space_id)
    if space is None:
        raise DraftError("not_found", "Chat space not found.")

    anthropic = _import_anthropic()
    count = max(1, min(int(count or DEFAULT_DRAFTS), MAX_DRAFTS))
    prompt = _user_prompt(
        space, build_context(chat_space_id), (instruction or "").strip(), count
    )
    client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=Config.CLAUDE_MODEL,
            max_tokens=Config.CLAUDE_MAX_TOKENS,
            # The persona is identical for every space, so it sits in `system`
            # behind a cache breakpoint while the per-space transcript goes in
            # the user turn after it.
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": Config.CLAUDE_EFFORT, "format": _OUTPUT_SCHEMA},
        )
    except anthropic.AuthenticationError as exc:
        logger.warning("AI draft auth failed for space %s: %s", chat_space_id, exc)
        raise DraftError("auth", "The Anthropic API key was rejected.") from exc
    except anthropic.RateLimitError as exc:
        logger.warning("AI draft rate limited for space %s: %s", chat_space_id, exc)
        raise DraftError("rate_limited", "Claude is rate limited. Try again shortly.") from exc
    except anthropic.APIStatusError as exc:
        logger.warning(
            "AI draft request failed for space %s (%s): %s",
            chat_space_id, exc.status_code, exc,
        )
        raise DraftError("upstream", "Claude returned an error. Try again.") from exc
    except anthropic.APIConnectionError as exc:
        logger.warning("AI draft connection failed for space %s: %s", chat_space_id, exc)
        raise DraftError("upstream", "Couldn't reach Claude. Try again.") from exc

    if getattr(response, "stop_reason", None) == "refusal":
        raise DraftError("refused", "Claude declined to draft a reply here.")
    return _parse_drafts(response)[:count]

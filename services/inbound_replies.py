"""
Reading what creators write back.

Resend only sends. A creator who answers a chase by hitting reply lands in a
human inbox the bot never reads, so the ladder keeps climbing past someone who
already said "posting Friday" — and the D+7 final notice goes out to a person
who answered on D+2. This module closes that loop.

Each chase email carries a per-chase ``Reply-To`` of the form
``chase+<token>@<CHASE_REPLY_DOMAIN>``. Resend routes replies to that domain
into ``POST /webhook/resend``, and the token identifies exactly which creator
on which campaign is replying — far better than guessing from the ``From:``
address, which changes, is shared, and cannot distinguish two campaigns the
same creator is running.

What happens to a reply, in order:

1. **Verify the signature.** This path can move a contracted deadline, so an
   unsigned or badly signed request is refused outright. The bot's older
   ``/webhook`` route trusts any POST that reaches it; that pattern must not
   be copied here.
2. **Fetch the body.** The inbound webhook carries metadata only — sender,
   recipients, subject, attachment list. The text is fetched from the
   receiving API by email id.
3. **Read it.** Claude classifies the reply and, when the creator commits to a
   date, extracts it.
4. **Propose, never apply.** A deadline is a contract term. The result is
   posted to Slack as a *proposal* with a Confirm button; nothing reaches the
   dashboard until a person clicks it. An LLM reading an email is not
   authority to rewrite an agreement.

Every reply reaches Slack whatever happens — an unreadable body, a failed
classification, a missing API key. A creator's answer is never silently
dropped; at worst the team sees the raw text and decides for itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import requests

from config import Config
from models.models import ChaseReplyAddress, SessionLocal
from utils import claude_alerts

logger = logging.getLogger(__name__)

RECEIVING_TIMEOUT_SECONDS = 15
# Replies quote the whole thread back. Past this we are almost certainly
# reading our own previous emails rather than anything the creator wrote.
MAX_BODY_CHARS = 8000
# How far ahead a proposed date may sit before it reads as a mis-parse rather
# than a commitment. Mirrors the dashboard's own guard.
MAX_PROPOSED_DAYS = 180

REPLY_LOCAL_PREFIX = "chase+"


# ---------------------------------------------------------------------------
# Per-chase reply addresses
# ---------------------------------------------------------------------------

def reply_address_for(campaign_id: str, creator_username: str) -> Optional[str]:
    """
    The Reply-To address for one creator's chase, minting it on first use.

    Returns None when inbound handling is off, so callers fall back to the
    normal reply-to and nothing changes until the domain is actually routing.
    """
    if not Config.CHASE_INBOUND_ENABLED:
        return None
    if not (campaign_id and creator_username):
        return None

    db = SessionLocal()
    try:
        row = (
            db.query(ChaseReplyAddress)
            .filter_by(campaign_id=campaign_id, creator_username=creator_username)
            .first()
        )
        if row is None:
            row = ChaseReplyAddress(
                token=secrets.token_urlsafe(12),
                campaign_id=campaign_id,
                creator_username=creator_username,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
        token = row.token
        return f"{REPLY_LOCAL_PREFIX}{token}@{Config.CHASE_REPLY_DOMAIN}"
    except Exception:
        db.rollback()
        logger.exception(
            "Could not mint a reply address for @%s on %s",
            creator_username, campaign_id,
        )
        return None
    finally:
        db.close()


def resolve_reply_address(addresses) -> Optional[tuple[str, str]]:
    """
    Find our chase token among a reply's recipients and return
    (campaign_id, creator_username).

    Recipients are checked rather than just the first: mail clients reply to
    several addresses, and the chase address may not be first in the list.
    """
    if isinstance(addresses, str):
        addresses = [addresses]
    for raw in (addresses or []):
        for token in re.findall(
            rf"{re.escape(REPLY_LOCAL_PREFIX)}([A-Za-z0-9_-]+)@", str(raw)
        ):
            db = SessionLocal()
            try:
                row = db.query(ChaseReplyAddress).filter_by(token=token).first()
                if row is not None:
                    return row.campaign_id, row.creator_username
            finally:
                db.close()
    return None


# ---------------------------------------------------------------------------
# Webhook signature
# ---------------------------------------------------------------------------

def verify_signature(raw_body: bytes, headers) -> bool:
    """
    Verify a Resend (Svix-scheme) webhook signature over the *raw* body.

    Parsing and re-serialising the JSON first changes the bytes and the
    signature will never match — callers must pass what arrived on the wire.

    A missing secret fails closed. This endpoint can move a contracted
    deadline, so "not configured" has to mean "refuse", never "trust".
    """
    secret = Config.RESEND_WEBHOOK_SECRET
    if not secret:
        logger.error(
            "RESEND_WEBHOOK_SECRET is not set — refusing inbound mail. "
            "This endpoint can change a deadline; it is never run unsigned."
        )
        return False

    svix_id = headers.get("svix-id") or headers.get("Svix-Id")
    timestamp = headers.get("svix-timestamp") or headers.get("Svix-Timestamp")
    signature_header = headers.get("svix-signature") or headers.get("Svix-Signature")
    if not (svix_id and timestamp and signature_header):
        logger.warning("Inbound mail missing Svix signature headers")
        return False

    key = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        key_bytes = base64.b64decode(key)
    except Exception:
        logger.error("RESEND_WEBHOOK_SECRET is not valid base64 after the prefix")
        return False

    signed = b".".join([
        svix_id.encode("utf-8"), str(timestamp).encode("utf-8"), raw_body,
    ])
    expected = base64.b64encode(
        hmac.new(key_bytes, signed, hashlib.sha256).digest()
    ).decode("utf-8")

    # The header carries a space-separated list of "v1,<sig>" entries so a
    # secret can be rotated without dropping messages mid-flight.
    for part in signature_header.split():
        _, _, candidate = part.partition(",")
        if candidate and hmac.compare_digest(candidate, expected):
            return True

    logger.warning("Inbound mail signature did not match")
    return False


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InboundReply:
    """One creator reply, normalized."""

    email_id: str
    sender: str
    recipients: list
    subject: str
    body: str
    campaign_id: Optional[str] = None
    creator_username: Optional[str] = None


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize(payload: dict) -> Optional[InboundReply]:
    """
    Pull the fields we need out of an inbound webhook.

    Reads `data` when present and the root otherwise, and accepts a couple of
    spellings per field. The webhook shape is the part of this integration
    most likely to shift, and a reply is worth more than strictness: an
    unrecognised layout is logged with its keys so it can be fixed against a
    real sample rather than guessed at again.
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    email_id = str(
        data.get("email_id") or data.get("id") or payload.get("email_id") or ""
    )
    sender = str(data.get("from") or data.get("sender") or "")
    recipients = _as_list(data.get("to") or data.get("recipients"))
    subject = str(data.get("subject") or "")
    # Present on some payloads, fetched separately when not.
    body = str(data.get("text") or data.get("plain_text") or data.get("body") or "")

    if not (email_id or sender or recipients):
        logger.warning(
            "Inbound mail payload not recognised; top-level keys were %s",
            sorted(payload.keys()),
        )
        return None

    return InboundReply(
        email_id=email_id,
        sender=sender,
        recipients=recipients,
        subject=subject,
        body=body,
    )


def fetch_body(email_id: str) -> str:
    """
    Fetch a received email's text from the receiving API.

    The webhook carries metadata only. A failure here is not fatal — the reply
    still reaches Slack without its text, which is worth far more than
    dropping it.
    """
    if not (email_id and Config.RESEND_API_KEY):
        return ""
    url = f"{Config.RESEND_RECEIVING_API_URL.rstrip('/')}/{email_id}"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {Config.RESEND_API_KEY}"},
            timeout=RECEIVING_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.warning("Could not reach the receiving API for %s: %s", email_id, exc)
        return ""
    if response.status_code >= 400:
        logger.warning(
            "Receiving API refused %s (HTTP %s): %s",
            email_id, response.status_code, response.text[:400],
        )
        return ""
    try:
        payload = response.json()
    except ValueError:
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return str(data.get("text") or data.get("plain_text") or data.get("html") or "")


def strip_quoted(body: str) -> str:
    """
    Drop the thread the client quoted back at us.

    Our own previous chase is the bulk of most replies, and feeding it to the
    classifier invites reading our words as the creator's — our emails talk
    about dates constantly.
    """
    lines = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^On .+ wrote:$", stripped):
            break
        if re.match(r"^-{2,}\s*Original Message\s*-{2,}$", stripped, re.I):
            break
        if stripped in ("--", "__") or stripped.startswith("Sent from my "):
            break
        lines.append(line)
    return "\n".join(lines).strip()[:MAX_BODY_CHARS]


# ---------------------------------------------------------------------------
# Reading the reply
# ---------------------------------------------------------------------------

INTENT_NEW_DATE = "commits_to_date"
INTENT_CANNOT_DELIVER = "cannot_deliver"
INTENT_ALREADY_POSTED = "says_already_posted"
INTENT_QUESTION = "asks_a_question"
INTENT_UNCLEAR = "unclear"

_INTERPRET_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [
                    INTENT_NEW_DATE,
                    INTENT_CANNOT_DELIVER,
                    INTENT_ALREADY_POSTED,
                    INTENT_QUESTION,
                    INTENT_UNCLEAR,
                ],
                "description": "What the creator is telling us.",
            },
            "proposed_date": {
                "type": ["string", "null"],
                "description": (
                    "The date they commit to posting by, as YYYY-MM-DD, or null "
                    "when they did not name one. Resolve relative language "
                    "('next Tuesday', 'end of the week') against the reply's "
                    "date given in the prompt."
                ),
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": (
                    "How sure you are of both the intent and the date. Low "
                    "whenever the wording is hedged ('probably', 'I'll try')."
                ),
            },
            "summary": {
                "type": "string",
                "description": "One sentence, for the team to read in Slack.",
            },
        },
        "required": ["intent", "proposed_date", "confidence", "summary"],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = """You read replies from social-media creators to emails \
chasing them about a campaign deadline they have missed.

Your only job is to report what the creator said. You are not deciding \
anything: a person reads your answer and decides whether to act on it.

Rules that matter:

- A date only counts as `commits_to_date` when the creator actually names \
when the content will be posted. "I'll get to it soon", "this week hopefully" \
and "as soon as I can" are `unclear`, not commitments.
- Resolve relative dates against the reply date given to you, and prefer the \
nearest future reading: "Tuesday" means the coming Tuesday.
- If they say the content is already live, that is `says_already_posted` even \
if they also give a date — the team needs to check the links, not reschedule.
- If they are pulling out, cancelling, or saying they cannot do it, that is \
`cannot_deliver`, whatever else the message contains.
- Hedged language means `low` confidence. So does a message that reads like \
it was written by an assistant or auto-responder.
- Quoted text from our own earlier emails may still be present. Ignore it; \
report only what this person wrote."""


def interpret(reply: InboundReply, *, today: Optional[date] = None) -> Optional[dict]:
    """
    Ask Claude what the creator said. Returns the structured reading, or None
    when the feature is unconfigured or the call fails — callers fall back to
    showing the team the raw text.
    """
    if not Config.ANTHROPIC_API_KEY:
        logger.info("ANTHROPIC_API_KEY unset — inbound replies are relayed unread")
        return None

    text = strip_quoted(reply.body)
    if not text:
        return None

    try:
        import anthropic  # noqa: PLC0415 — optional dependency, imported on use
    except ImportError:
        logger.error("anthropic package missing — inbound replies relayed unread")
        return None

    today = today or date.today()
    prompt = (
        f"This reply arrived on {today.isoformat()} "
        f"(a {today.strftime('%A')}).\n\n"
        f"From: {reply.sender}\n"
        f"Subject: {reply.subject}\n\n"
        f"---\n{text}\n---"
    )

    client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=Config.CLAUDE_MODEL,
            max_tokens=Config.CLAUDE_MAX_TOKENS,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": _INTERPRET_SCHEMA},
        )
    except Exception as exc:
        claude_alerts.report(exc)
        logger.warning("Could not read reply %s: %s", reply.email_id, exc)
        return None

    if getattr(response, "stop_reason", None) == "refusal":
        logger.warning("Reading reply %s was declined", reply.email_id)
        return None

    try:
        raw = next(b.text for b in response.content if b.type == "text")
        result = json.loads(raw)
    except (StopIteration, ValueError, AttributeError) as exc:
        logger.warning("Unreadable classification for %s: %s", reply.email_id, exc)
        return None

    result["proposed_date"] = _sane_date(result.get("proposed_date"), today)
    return result


def _sane_date(value, today: date) -> Optional[str]:
    """
    Keep a proposed date only if it could plausibly be a commitment.

    A date in the past or years out is a mis-parse, and the whole point of
    this path is that a misread reply must not reach the dashboard. Dropping
    the date leaves the reply in Slack for a human, which is the right
    failure.
    """
    if not value:
        return None
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None
    if parsed < today:
        logger.info("Discarding proposed date %s — it is in the past", parsed)
        return None
    if parsed > today + timedelta(days=MAX_PROPOSED_DAYS):
        logger.info("Discarding proposed date %s — too far out to be real", parsed)
        return None
    return parsed.isoformat()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

class InboundReplyHandler:
    """
    Turns one verified inbound webhook into a Slack post the team can act on.

    Deliberately forgiving at every step after verification. A reply that
    cannot be attributed, whose body will not load, or that the classifier
    declines to read still reaches the channel — the failure this whole
    feature exists to fix is a creator's answer going unseen, and a partial
    post beats a dropped one every time.
    """

    def __init__(self, slack_client, reelstats_api=None):
        self.client = slack_client
        self.api = reelstats_api

    def handle(self, payload: dict) -> bool:
        from templates.slack_blocks import build_inbound_reply_blocks

        reply = normalize(payload)
        if reply is None:
            return False

        target = resolve_reply_address(reply.recipients)
        if target is None:
            logger.info(
                "Inbound mail from %s carried no chase address; ignoring",
                reply.sender,
            )
            return False
        campaign_id, creator_username = target

        body = reply.body or fetch_body(reply.email_id)
        reply = InboundReply(
            email_id=reply.email_id,
            sender=reply.sender,
            recipients=reply.recipients,
            subject=reply.subject,
            body=body,
            campaign_id=campaign_id,
            creator_username=creator_username,
        )

        reading = interpret(reply)
        campaign_name, current_deadline = self._creator_context(
            campaign_id, creator_username,
        )

        # A reply means the creator is engaging, whatever it says. Hold the
        # ladder so the next rung doesn't go out while a person is reading
        # this — the Confirm and Stop buttons take it from here.
        from services import chase_ladder
        chase_ladder.set_state(
            campaign_id,
            creator_username,
            status="snoozed",
            snoozed_until=chase_ladder.today_local() + timedelta(days=3),
            set_by="inbound-reply",
            reason="Creator replied by email",
        )

        blocks = build_inbound_reply_blocks(
            creator_username=creator_username,
            campaign_name=campaign_name,
            campaign_id=campaign_id,
            subject=reply.subject,
            body=strip_quoted(body),
            reading=reading,
            current_deadline=current_deadline,
        )
        try:
            self.client.chat_postMessage(
                channel=Config.SLACK_CHANNEL_DEADLINES,
                text=f"@{creator_username} replied about their deadline",
                blocks=blocks,
            )
        except Exception as exc:
            logger.exception("Could not post an inbound reply to Slack: %s", exc)
            return False

        logger.info(
            "Inbound reply from @%s on %s (intent=%s)",
            creator_username, campaign_id,
            (reading or {}).get("intent", "unread"),
        )
        return True

    def _creator_context(self, campaign_id: str, username: str) -> tuple[str, Optional[str]]:
        """Campaign name and current deadline, for the Slack card. Best effort."""
        if self.api is None:
            return "", None
        try:
            campaigns = self.api.get_campaigns(campaign_id=campaign_id, creator=username)
        except Exception:
            return "", None
        for campaign in campaigns or []:
            for creator in campaign.get("creators", []):
                if (creator.get("username") or "").lower() == username.lower():
                    return campaign.get("name", ""), creator.get("deadline")
        return "", None

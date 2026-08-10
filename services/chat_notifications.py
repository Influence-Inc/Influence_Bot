"""
Out-of-band notifications for chat-space activity.

Creating a chat space (brand clicks Request Changes) intentionally sends
no notification — the creator is only emailed once the brand actually
posts a message:

- When a brand posts a message: email the creator + Slack-ping the brand
  channel with a preview and "Open Chat" button.
- When a creator posts a message: Slack-ping the brand channel.

All sends are best-effort and never raise — chat itself must not fail
because a notification dropped.
"""

from __future__ import annotations

import logging
from typing import Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import Config
from models.models import (
    ChatMessage,
    ChatSpace,
    ReviewSubmission,
    SessionLocal,
    SlackInstallation,
)
from services.email_service import EmailService
from templates.email_templates import chat_new_message
from templates.slack_blocks import (
    build_chat_influence_ping_blocks,
    build_chat_new_message_blocks,
)
from utils.chat_tokens import make_invite_token

logger = logging.getLogger(__name__)

_email_service = EmailService()


def _chat_url(space_id: int, party: str, identifier: Optional[str] = None) -> Optional[str]:
    base = Config.PUBLIC_BASE_URL
    if not base:
        logger.warning(
            "PUBLIC_BASE_URL not configured; cannot build chat magic links."
        )
        return None
    token = make_invite_token(
        chat_space_id=space_id, party=party, identifier=identifier
    )
    return f"{base}/chat/invite/{token}"


def _brand_install(space: ChatSpace) -> Optional[SlackInstallation]:
    if not space.brand_install_id:
        return None
    db = SessionLocal()
    try:
        row = db.query(SlackInstallation).get(space.brand_install_id)
        if row is None:
            return None
        db.expunge(row)
        return row
    finally:
        db.close()


def _load_space(chat_space_id: int) -> Optional[ChatSpace]:
    db = SessionLocal()
    try:
        row = db.query(ChatSpace).get(chat_space_id)
        if row is None:
            return None
        db.expunge(row)
        return row
    finally:
        db.close()


def _anchor_admin_slack(space_id: int, channel: Optional[str], ts: Optional[str]) -> None:
    """Persist the first INFLUENCE-team ping's (channel, ts) so later
    creator/brand messages thread underneath it. Best-effort."""
    if not ts:
        return
    db = SessionLocal()
    try:
        space = db.query(ChatSpace).get(space_id)
        if space is None:
            return
        if channel:
            space.admin_slack_channel = channel
        space.admin_slack_ts = ts
        db.commit()
    except Exception as exc:
        logger.warning(
            "Could not anchor chat_space %s to admin message ts=%s: %s",
            space_id, ts, exc,
        )
    finally:
        db.close()


def _review_admin_anchor(space: ChatSpace) -> tuple[Optional[str], Optional[str]]:
    """Return (channel, ts) of the review-submitted admin message so chat
    pings can thread underneath the original "content to be reviewed"
    post. Returns (None, None) when the space has no linked review or the
    review's admin-channel post wasn't captured."""
    if not space.latest_review_id:
        return None, None
    db = SessionLocal()
    try:
        review = db.query(ReviewSubmission).get(space.latest_review_id)
        if review is None:
            return None, None
        return review.slack_channel, review.slack_ts
    finally:
        db.close()


def _notify_influence_team(
    space: ChatSpace, *, sender_party: str, sender_name: str, preview: str
) -> None:
    """
    Slack-ping the INFLUENCE team channel (#content-reviews) so Jennifer's
    team is kept in the loop on chat activity. This is what makes the
    composer's "… and Jennifer will be notified" hint truthful.

    Pings thread under the original review-submitted "content to be
    reviewed" post when its Slack coordinates are known, so every chat
    message (creator, brand, or admin) lands as a reply inside that
    thread instead of a fresh top-level notification, keeping the whole
    conversation grouped. Falls back to the legacy scheme (anchor on the
    first ping via admin_slack_ts) for chat spaces whose review has no
    captured Slack post.

    Threading alone is invisible, though: Slack doesn't notify channel
    members of a thread reply unless they're already following the thread,
    and nobody on the team is. So when the message came from a creator or
    brand — the people the team needs to hear from — the ping is *also*
    broadcast to the channel (``reply_broadcast=True``), which surfaces it
    in the main timeline and notifies the channel while still keeping the
    reply inside the thread. If ``SLACK_REVIEWS_NOTIFY`` is set, a mention
    is prepended so the team gets a hard ping even with the channel muted.
    Messages the team typed themselves (sender_party="admin") stay as quiet
    threaded replies — no need to broadcast their own words back at them.

    Best-effort; never raises.
    """
    if not Config.SLACK_BOT_TOKEN:
        return
    admin_url = ""
    if Config.PUBLIC_BASE_URL:
        admin_url = f"{Config.PUBLIC_BASE_URL}/admin/chats/{space.id}"

    review_channel, review_ts = _review_admin_anchor(space)
    channel = (
        review_channel
        or space.admin_slack_channel
        or Config.SLACK_CHANNEL_REVIEWS
    )
    if not channel:
        return
    thread_ts = review_ts or space.admin_slack_ts or None

    # Inbound messages (creator/brand) are the ones the team must not miss:
    # broadcast them to the channel and optionally tag a mention. The team's
    # own admin-sent messages stay as silent threaded replies.
    broadcast = sender_party in ("creator", "brand")
    mention = Config.SLACK_REVIEWS_NOTIFY if broadcast else None

    text = (
        f"New chat message from {sender_name} — "
        f"{space.brand_name or 'brand'} × @{space.creator_username}"
    )
    if mention:
        text = f"{mention} {text}"

    try:
        blocks = build_chat_influence_ping_blocks(
            creator_username=space.creator_username,
            brand_name=space.brand_name or "the brand",
            campaign_name=space.campaign_name or "—",
            sender_name=sender_name,
            preview=preview,
            admin_url=admin_url,
            mention=mention or "",
        )
        response = WebClient(token=Config.SLACK_BOT_TOKEN).chat_postMessage(
            channel=channel,
            text=text,
            blocks=blocks,
            thread_ts=thread_ts,
            reply_broadcast=broadcast,
        )
        # Only fall back to anchoring on the first ping when we couldn't
        # thread under the review-submitted message.
        if not review_ts and not space.admin_slack_ts and response.get("ok"):
            _anchor_admin_slack(space.id, response.get("channel"), response.get("ts"))
    except SlackApiError as exc:
        err = exc.response.get("error") if exc.response else str(exc)
        logger.warning("INFLUENCE-team chat ping failed: %s", err)
    except Exception as exc:
        logger.warning("INFLUENCE-team chat ping failed: %s", exc)


def notify_new_message(*, chat_space_id: int, sender_party: str, message_id: int) -> None:
    """
    Out-of-band ping for the *other* side.
      - sender=brand  -> email the creator
      - sender=creator -> Slack-ping the brand channel
      - sender=admin -> Slack-ping the brand channel + email creator

    In addition, every creator/brand message pings the INFLUENCE team channel
    (Jennifer) so INFLUENCE stays in the loop on the conversation.
    """
    space = _load_space(chat_space_id)
    if space is None:
        return

    db = SessionLocal()
    try:
        msg = db.query(ChatMessage).get(message_id)
        if msg is None:
            return
        full_body = (msg.body or "").strip()
        sender_name = msg.sender_display_name or msg.sender_party
    finally:
        db.close()
    # The creator email quotes the message in full; the Slack pings (which have
    # tighter block limits) get a short preview.
    preview = full_body
    if len(preview) > 200:
        preview = preview[:197] + "…"

    if sender_party in ("brand", "admin") and space.creator_email:
        creator_url = _chat_url(space.id, party="creator", identifier=space.creator_email)
        if creator_url:
            try:
                tmpl = chat_new_message(
                    creator_name=space.creator_username,
                    brand_name=space.brand_name or "the brand",
                    message=full_body or "(image / attachment)",
                    chat_url=creator_url,
                    sender_party=sender_party,
                )
                _email_service.send_email(
                    space.creator_email,
                    tmpl["subject"],
                    tmpl["body"],
                )
            except Exception as exc:
                logger.warning("chat new-message email failed: %s", exc)

    if sender_party in ("creator", "admin"):
        install = _brand_install(space)
        brand_url = _chat_url(space.id, party="brand")
        # Prefer threading under the brand-workspace review_submitted message
        # captured at post time; fall back to the install's default channel.
        target_channel = space.brand_slack_channel or (install.channel_id if install else None)
        if install and install.bot_token and target_channel and brand_url:
            blocks = build_chat_new_message_blocks(
                creator_username=space.creator_username,
                campaign_name=space.campaign_name or "—",
                sender_name=sender_name,
                preview=preview or "(image / attachment)",
                chat_url=brand_url,
            )
            try:
                # This path only fires for creator/admin messages — both
                # inbound to the brand — and threads under the brand's review
                # post. Slack won't notify the brand of a thread reply they
                # aren't following, so broadcast it to the channel too: the
                # reply stays in the thread but also surfaces in the timeline.
                WebClient(token=install.bot_token).chat_postMessage(
                    channel=target_channel,
                    text=f"New chat message from @{space.creator_username}",
                    blocks=blocks,
                    thread_ts=space.brand_slack_ts or None,
                    reply_broadcast=True,
                )
            except SlackApiError as exc:
                err = exc.response.get("error") if exc.response else str(exc)
                logger.warning("brand new-message Slack post failed: %s", err)
            except Exception as exc:
                logger.warning("brand new-message Slack post failed: %s", exc)

    # Keep the INFLUENCE team (Jennifer) notified of every chat message —
    # including admin-sent ones, so anything typed from the admin side of
    # the chat also shows up (threaded) under the review-submitted post
    # in #content-reviews rather than only living inside the chat UI.
    _notify_influence_team(
        space,
        sender_party=sender_party,
        sender_name=sender_name,
        preview=preview or "(image / attachment)",
    )

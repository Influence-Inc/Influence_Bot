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


def _notify_influence_team(space: ChatSpace, *, sender_name: str, preview: str) -> None:
    """
    Slack-ping the INFLUENCE team channel (#content-reviews) so Jennifer's
    team is kept in the loop on creator <-> brand chat activity. This is what
    makes the composer's "… and Jennifer will be notified" hint truthful.

    The first ping for a chat space anchors a thread (admin_slack_channel /
    admin_slack_ts); every later creator/brand message on that space replies
    in-thread instead of posting a fresh top-level message.

    Best-effort; never raises.
    """
    if not Config.SLACK_BOT_TOKEN or not Config.SLACK_CHANNEL_REVIEWS:
        return
    admin_url = ""
    if Config.PUBLIC_BASE_URL:
        admin_url = f"{Config.PUBLIC_BASE_URL}/admin/chats/{space.id}"
    channel = space.admin_slack_channel or Config.SLACK_CHANNEL_REVIEWS
    try:
        blocks = build_chat_influence_ping_blocks(
            creator_username=space.creator_username,
            brand_name=space.brand_name or "the brand",
            campaign_name=space.campaign_name or "—",
            sender_name=sender_name,
            preview=preview,
            admin_url=admin_url,
        )
        response = WebClient(token=Config.SLACK_BOT_TOKEN).chat_postMessage(
            channel=channel,
            text=(
                f"New chat message from {sender_name} — "
                f"{space.brand_name or 'brand'} × @{space.creator_username}"
            ),
            blocks=blocks,
            thread_ts=space.admin_slack_ts or None,
        )
        if not space.admin_slack_ts and response.get("ok"):
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
        preview = (msg.body or "").strip()
        sender_name = msg.sender_display_name or msg.sender_party
    finally:
        db.close()
    if len(preview) > 200:
        preview = preview[:197] + "…"

    if sender_party in ("brand", "admin") and space.creator_email:
        creator_url = _chat_url(space.id, party="creator", identifier=space.creator_email)
        if creator_url:
            try:
                tmpl = chat_new_message(
                    creator_name=space.creator_username,
                    brand_name=space.brand_name or "the brand",
                    preview=preview or "(image / attachment)",
                    chat_url=creator_url,
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
                WebClient(token=install.bot_token).chat_postMessage(
                    channel=target_channel,
                    text=f"New chat message from @{space.creator_username}",
                    blocks=blocks,
                    thread_ts=space.brand_slack_ts or None,
                )
            except SlackApiError as exc:
                err = exc.response.get("error") if exc.response else str(exc)
                logger.warning("brand new-message Slack post failed: %s", err)
            except Exception as exc:
                logger.warning("brand new-message Slack post failed: %s", exc)

    # Keep the INFLUENCE team (Jennifer) notified of every creator/brand
    # message. Admin messages come *from* INFLUENCE, so they're skipped here.
    if sender_party in ("creator", "brand"):
        _notify_influence_team(
            space, sender_name=sender_name, preview=preview or "(image / attachment)"
        )

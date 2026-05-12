"""
Out-of-band notifications for chat-space activity.

- After a chat space is created (brand clicks Request Changes): email the
  creator a magic link and post a Slack invite into the brand workspace.
- When a brand posts a message: email the creator + Slack-ping the brand
  channel with a preview and "Open Chat" button.
- When a creator posts a message: Slack-ping the brand channel.

All sends are best-effort and never raise — chat itself must not fail
because a notification dropped.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlencode

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
from templates.email_templates import chat_invite, chat_new_message
from templates.slack_blocks import (
    build_chat_new_message_blocks,
    build_chat_space_invite_blocks,
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


def notify_creator_changes_requested(*, chat_space_id: int, actor_name: str = "") -> bool:
    """
    Fired on every Request-Changes click. Emails the creator a fresh
    chat-invite (from contact@influence.technology by default) with their
    magic link. Returns True if the email was sent successfully.

    There is intentionally NO Slack notification fan-out here: the brand
    is being redirected directly into the chat by the URL on the Slack
    button itself, so there's nothing to announce in their channel.
    """
    space = _load_space(chat_space_id)
    if space is None or not space.creator_email:
        return False

    creator_url = _chat_url(space.id, party="creator", identifier=space.creator_email)
    if not creator_url:
        return False

    try:
        tmpl = chat_invite(
            creator_name=space.creator_username,
            brand_name=space.brand_name or "the brand",
            campaign_name=space.campaign_name or "your campaign",
            chat_url=creator_url,
        )
        return _email_service.send_email(
            space.creator_email,
            tmpl["subject"],
            tmpl["body"],
            from_email=Config.CHAT_NOTIFICATION_FROM_EMAIL,
            from_name=Config.CHAT_NOTIFICATION_FROM_NAME,
        )
    except Exception as exc:
        logger.warning("notify_creator_changes_requested email failed: %s", exc)
        return False


def notify_new_message(*, chat_space_id: int, sender_party: str, message_id: int) -> None:
    """
    Out-of-band ping for the *other* side.
      - sender=brand  -> email the creator
      - sender=creator -> Slack-ping the brand channel
      - sender=admin -> Slack-ping the brand channel + email creator
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
                    sender_name=sender_name,
                    preview=preview or "(image / attachment)",
                    chat_url=creator_url,
                )
                _email_service.send_email(
                    space.creator_email,
                    tmpl["subject"],
                    tmpl["body"],
                    from_email=Config.CHAT_NOTIFICATION_FROM_EMAIL,
                    from_name=Config.CHAT_NOTIFICATION_FROM_NAME,
                )
            except Exception as exc:
                logger.warning("chat new-message email failed: %s", exc)

    if sender_party in ("creator", "admin"):
        install = _brand_install(space)
        brand_url = _chat_url(space.id, party="brand")
        if install and install.bot_token and install.channel_id and brand_url:
            blocks = build_chat_new_message_blocks(
                creator_username=space.creator_username,
                campaign_name=space.campaign_name or "—",
                sender_name=sender_name,
                preview=preview or "(image / attachment)",
                chat_url=brand_url,
            )
            try:
                WebClient(token=install.bot_token).chat_postMessage(
                    channel=install.channel_id,
                    text=f"New chat message from @{space.creator_username}",
                    blocks=blocks,
                    thread_ts=space.brand_slack_ts or None,
                )
            except SlackApiError as exc:
                err = exc.response.get("error") if exc.response else str(exc)
                logger.warning("brand new-message Slack post failed: %s", err)
            except Exception as exc:
                logger.warning("brand new-message Slack post failed: %s", exc)

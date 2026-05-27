"""
Slack interactive action handlers for INFLUENCE Bot.
Handles button clicks on notification messages.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from models.models import (
    PaymentRecord,
    ReviewSubmission,
    SessionLocal,
)
from services.email_service import EmailSendResult
from services import chat_service
from services.chat_notifications import notify_creator_changes_requested
from services.review_approval import approve_review_core

logger = logging.getLogger(__name__)


def _strip_review_action_buttons(blocks: list[dict], review_id: int) -> list[dict]:
    """Remove the Approve/Request Changes action row for this review."""
    target_block_id = f"review_actions_{review_id}"
    return [
        b for b in blocks
        if not (b.get("type") == "actions" and b.get("block_id") == target_block_id)
    ]


def register_actions(app):
    """Register interactive component handlers on the Bolt app."""

    @app.action("mark_as_paid")
    def handle_mark_as_paid(ack, body, client, respond):
        """Update the original message to show the creator was marked paid."""
        ack()

        user = body.get("user", {})
        actor = user.get("username") or user.get("name") or user.get("id", "someone")

        action = (body.get("actions") or [{}])[0]
        value = action.get("value", "")
        try:
            campaign_id, creator_username = value.split("|", 1)
        except ValueError:
            campaign_id, creator_username = "", value

        channel_id = (body.get("channel") or {}).get("id")
        message = body.get("message") or {}
        ts = message.get("ts")
        original_blocks = message.get("blocks") or []

        # Persist the decision so it survives restarts.
        db = SessionLocal()
        try:
            record = PaymentRecord(
                campaign_id=campaign_id or None,
                creator_username=creator_username,
                marked_by_id=user.get("id"),
                marked_by_name=actor,
            )
            db.add(record)
            try:
                db.commit()
            except IntegrityError:
                # Already recorded — another click or duplicate button.
                db.rollback()
                logger.info(
                    f"mark_as_paid already recorded for "
                    f"campaign={campaign_id} creator=@{creator_username}"
                )
        finally:
            db.close()

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Remove any actions blocks and any accessory buttons matching this creator,
        # then append a confirmation line.
        updated_blocks = []
        target_value = f"{campaign_id}|{creator_username}"
        for block in original_blocks:
            if block.get("type") == "actions":
                elements = block.get("elements") or []
                if any(
                    el.get("action_id") == "mark_as_paid"
                    and el.get("value") == target_value
                    for el in elements
                ):
                    continue
            if block.get("type") == "section" and "accessory" in block:
                accessory = block.get("accessory") or {}
                if (
                    accessory.get("action_id") == "mark_as_paid"
                    and accessory.get("value") == target_value
                ):
                    block = {k: v for k, v in block.items() if k != "accessory"}
            updated_blocks.append(block)

        updated_blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f":white_check_mark: *Marked as paid* by "
                            f"<@{user.get('id', '')}> — @{creator_username} "
                            f"({timestamp})"
                        ),
                    }
                ],
            }
        )

        if channel_id and ts:
            try:
                client.chat_update(
                    channel=channel_id,
                    ts=ts,
                    text=f"@{creator_username} marked as paid by @{actor}",
                    blocks=updated_blocks,
                )
            except Exception as e:
                logger.error(f"Failed to update message after mark_as_paid: {e}")
                respond(
                    text=(
                        f":white_check_mark: Marked @{creator_username} as paid "
                        f"(couldn't update the original message)."
                    ),
                    response_type="ephemeral",
                )

        logger.info(
            f"mark_as_paid: creator=@{creator_username} "
            f"campaign_id={campaign_id} actor=@{actor}"
        )

    # ------------------------------------------------------------------
    # Review: Approve
    # ------------------------------------------------------------------
    @app.action("review_approve")
    def handle_review_approve(ack, body, client, respond):
        ack()

        user = body.get("user", {})
        actor_id = user.get("id", "")
        actor_name = user.get("username") or user.get("name") or actor_id

        action = (body.get("actions") or [{}])[0]
        try:
            review_id = int(action.get("value", ""))
        except (TypeError, ValueError):
            logger.warning("review_approve clicked with non-integer value")
            return

        channel_id = (body.get("channel") or {}).get("id")
        message = body.get("message") or {}
        ts = message.get("ts")
        original_blocks = message.get("blocks") or []

        db = SessionLocal()
        try:
            row = db.query(ReviewSubmission).get(review_id)
            if row is None:
                logger.warning(f"review_approve for unknown review_id={review_id}")
                respond(
                    text=":warning: Could not find that review in the database.",
                    response_type="ephemeral",
                )
                return
            if row.decision == "approved":
                respond(
                    text=":information_source: This review is already approved.",
                    response_type="ephemeral",
                )
                return
            creator_username = row.creator_username
        finally:
            db.close()

        # Shared approval flow: DB write, creator email, close chat space,
        # admin #content-reviews notification. Same code path the 24h
        # auto-approval sweep uses, so any change to side effects lands
        # in both places.
        approve_review_core(
            review_id=review_id,
            actor_id=actor_id,
            actor_name=actor_name,
        )

        # Update the message the brand clicked: drop the buttons + add a
        # footer. Done here (not in approve_review_core) because it needs
        # the click's channel/ts; the auto-approval path has no such
        # message to update.
        updated_blocks = _strip_review_action_buttons(original_blocks, review_id)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        updated_blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f":white_check_mark: *Approved* by <@{actor_id}> — "
                            f"@{creator_username} ({timestamp})"
                        ),
                    }
                ],
            }
        )

        if channel_id and ts:
            try:
                client.chat_update(
                    channel=channel_id,
                    ts=ts,
                    text=f"Review approved for @{creator_username}",
                    blocks=updated_blocks,
                )
            except Exception as e:
                logger.error(f"Failed to update message after review_approve: {e}")

    # ------------------------------------------------------------------
    # Review: Request Changes
    #
    # The Slack button is hybrid (url + action_id): clicking it opens the
    # brand's chat space in the browser AND fires this handler. We use the
    # action callback to (a) record the decision, (b) email the creator,
    # (c) append a footer to the original Slack message. The buttons
    # themselves stay until Approve is clicked, so the brand can request
    # changes multiple times — each click re-emails the creator and
    # updates the footer.
    # ------------------------------------------------------------------
    @app.action("review_request_changes")
    def handle_review_request_changes(ack, body, client):
        ack()

        user = body.get("user", {})
        actor_id = user.get("id", "")
        actor_name = user.get("username") or user.get("name") or actor_id

        action = (body.get("actions") or [{}])[0]
        try:
            review_id = int(action.get("value", ""))
        except (TypeError, ValueError):
            logger.warning("review_request_changes clicked with non-integer value")
            return

        channel_id = (body.get("channel") or {}).get("id")
        message = body.get("message") or {}
        ts = message.get("ts")
        original_blocks = message.get("blocks") or []

        db = SessionLocal()
        try:
            row = db.query(ReviewSubmission).get(review_id)
            if row is None:
                logger.warning(
                    f"review_request_changes for unknown review_id={review_id}"
                )
                return
            if row.decision == "approved":
                # Already approved — buttons should already be stripped,
                # but in case a stale message is clicked, no-op.
                logger.info(
                    f"review_request_changes ignored: review {review_id} "
                    f"already approved"
                )
                return

            # First-time-or-not bookkeeping: set decision on first click,
            # always bump decided_at/by to reflect the latest click.
            first_time = row.decision is None
            if first_time:
                row.decision = "changes_requested"
            row.decided_by_id = actor_id
            row.decided_by_name = actor_name
            row.decided_at = datetime.now(timezone.utc)
            db.commit()

            creator_email = row.creator_email
            creator_username = row.creator_username
            brand_name = row.brand_name or ""
            campaign_name = row.campaign_name or ""
        finally:
            db.close()

        # Ensure a chat space exists (idempotent — pre-created at review
        # post time, but this is the safety net if PUBLIC_BASE_URL was
        # missing then).
        chat_space_id = None
        try:
            team = body.get("team") or {}
            space = chat_service.get_or_create_for_review(
                review_id, workspace_team_id=team.get("id"),
            )
            if space is not None:
                chat_space_id = space.id
        except Exception as exc:
            logger.exception(
                "Failed to ensure chat space for review %s: %s", review_id, exc
            )

        # Email the creator on the first click only; subsequent Request-
        # Changes clicks on the same review return ALREADY_SENT (the
        # chat-notifications module handles the "first time" check via
        # chat_space.creator_invited_at).
        email_result = EmailSendResult.FAILED
        if chat_space_id and creator_email:
            try:
                email_result = notify_creator_changes_requested(
                    chat_space_id=chat_space_id,
                    actor_name=actor_name,
                )
            except Exception as exc:
                logger.warning("notify_creator_changes_requested failed: %s", exc)
                email_result = EmailSendResult.FAILED

        # On ALREADY_SENT (duplicate Request-Changes click) we skip the
        # footer entirely — the brand was just redirected into the chat
        # via the button URL, no new event happened, and an extra footer
        # per click clutters the message. On the first click we render a
        # short "Chat opened" line without exposing internal email-send
        # status in the brand's view.
        render_footer = False
        if not creator_email:
            render_footer = True
        elif email_result in (EmailSendResult.SENT, EmailSendResult.FAILED):
            render_footer = True

        if render_footer and channel_id and ts:
            updated_blocks = list(original_blocks)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            updated_blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                f":pencil2: *Chat opened* by <@{actor_id}> — "
                                f"@{creator_username} ({timestamp})"
                            ),
                        }
                    ],
                }
            )
            try:
                client.chat_update(
                    channel=channel_id,
                    ts=ts,
                    text=f"Changes requested for @{creator_username}",
                    blocks=updated_blocks,
                )
            except Exception as e:
                logger.error(
                    f"Failed to update message after review_request_changes: {e}"
                )

        logger.info(
            f"review_request_changes: review_id={review_id} "
            f"creator=@{creator_username} actor=@{actor_name} "
            f"first_time={first_time} email_result={email_result.value}"
        )

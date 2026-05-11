"""
Slack interactive action handlers for INFLUENCE Bot.
Handles button clicks on notification messages.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from models.models import (
    PaymentRecord,
    ReviewSubmission,
    SessionLocal,
)
from services.email_service import EmailService
from services import chat_service
from services.chat_notifications import notify_chat_space_created
from templates.email_templates import (
    video_approved,
    video_changes_requested,
)

logger = logging.getLogger(__name__)

_email_service = EmailService()


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

            if row.decision:
                respond(
                    text=(
                        f":information_source: This review already has a decision "
                        f"recorded: *{row.decision}*."
                    ),
                    response_type="ephemeral",
                )
                return

            row.decision = "approved"
            row.decided_by_id = actor_id
            row.decided_by_name = actor_name
            row.decided_at = datetime.now(timezone.utc)
            db.commit()

            creator_email = row.creator_email
            creator_username = row.creator_username
            brand_name = row.brand_name or ""
        finally:
            db.close()

        email_sent = False
        if creator_email:
            template = video_approved(
                creator_name=creator_username, brand_name=brand_name
            )
            email_sent = _email_service.send_approval_notification(
                creator_email, template
            )

        # Update Slack message: drop the buttons, add an approval footer.
        updated_blocks = _strip_review_action_buttons(original_blocks, review_id)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        email_note = (
            "creator emailed" if email_sent
            else ("no creator email on file" if not creator_email
                  else "email failed — check logs")
        )
        updated_blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f":white_check_mark: *Approved* by <@{actor_id}> — "
                            f"@{creator_username} ({timestamp}) · {email_note}"
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

        logger.info(
            f"review_approve: review_id={review_id} "
            f"creator=@{creator_username} actor=@{actor_name} "
            f"email_sent={email_sent}"
        )

    # ------------------------------------------------------------------
    # Review: Request Changes (open modal)
    # ------------------------------------------------------------------
    @app.action("review_request_changes")
    def handle_review_request_changes(ack, body, client):
        ack()

        action = (body.get("actions") or [{}])[0]
        review_id = action.get("value", "")

        channel_id = (body.get("channel") or {}).get("id")
        message = body.get("message") or {}
        ts = message.get("ts")
        trigger_id = body.get("trigger_id")

        # Pre-fetch a bit of context for the modal title.
        db = SessionLocal()
        try:
            try:
                row = db.query(ReviewSubmission).get(int(review_id))
            except (TypeError, ValueError):
                row = None
            if row is None:
                logger.warning(
                    f"review_request_changes for unknown review_id={review_id}"
                )
                return
            if row.decision:
                client.chat_postEphemeral(
                    channel=channel_id,
                    user=body.get("user", {}).get("id"),
                    text=(
                        f":information_source: This review already has a decision "
                        f"recorded: *{row.decision}*."
                    ),
                )
                return
            creator_username = row.creator_username
        finally:
            db.close()

        private_metadata = json.dumps(
            {
                "review_id": int(review_id),
                "channel_id": channel_id,
                "ts": ts,
            }
        )

        try:
            client.views_open(
                trigger_id=trigger_id,
                view={
                    "type": "modal",
                    "callback_id": "review_changes_modal",
                    "private_metadata": private_metadata,
                    "title": {"type": "plain_text", "text": "Request Changes"},
                    "submit": {"type": "plain_text", "text": "Send to Creator"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"Feedback for *@{creator_username}*. "
                                    f"This will email the creator and update the Slack message."
                                ),
                            },
                        },
                        {
                            "type": "input",
                            "block_id": "feedback_block",
                            "label": {
                                "type": "plain_text",
                                "text": "What needs to change?",
                            },
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "feedback_input",
                                "multiline": True,
                                "min_length": 3,
                                "placeholder": {
                                    "type": "plain_text",
                                    "text": "Describe the requested changes…",
                                },
                            },
                        },
                    ],
                },
            )
        except Exception as e:
            logger.error(f"Failed to open request-changes modal: {e}")

    # ------------------------------------------------------------------
    # Review: Request Changes (modal submission)
    # ------------------------------------------------------------------
    @app.view("review_changes_modal")
    def handle_review_changes_submit(ack, body, client, view):
        ack()

        user = body.get("user", {})
        actor_id = user.get("id", "")
        actor_name = user.get("username") or user.get("name") or actor_id

        try:
            meta = json.loads(view.get("private_metadata") or "{}")
        except json.JSONDecodeError:
            meta = {}
        review_id = meta.get("review_id")
        channel_id = meta.get("channel_id")
        ts = meta.get("ts")

        state_values = (view.get("state") or {}).get("values", {})
        feedback = (
            state_values.get("feedback_block", {})
            .get("feedback_input", {})
            .get("value")
            or ""
        ).strip()

        if not review_id or not feedback:
            logger.warning("review_changes_modal submitted without review_id or feedback")
            return

        db = SessionLocal()
        try:
            row = db.query(ReviewSubmission).get(review_id)
            if row is None:
                logger.warning(
                    f"review_changes_modal submission for unknown review_id={review_id}"
                )
                return
            if row.decision:
                logger.info(
                    f"review_changes_modal: review {review_id} already decided "
                    f"as {row.decision}, ignoring"
                )
                return

            row.decision = "changes_requested"
            row.decision_feedback = feedback
            row.decided_by_id = actor_id
            row.decided_by_name = actor_name
            row.decided_at = datetime.now(timezone.utc)
            db.commit()

            creator_email = row.creator_email
            creator_username = row.creator_username
            brand_name = row.brand_name or ""
        finally:
            db.close()

        email_sent = False
        if creator_email:
            template = video_changes_requested(
                creator_name=creator_username,
                brand_name=brand_name,
                feedback=feedback,
            )
            email_sent = _email_service.send_approval_notification(
                creator_email, template
            )

        # Open (or reuse) the persistent chat space for this creator + campaign
        # and notify both sides. Failures here must not block the rest of the
        # Request-Changes flow.
        chat_space_id = None
        try:
            team = body.get("team") or {}
            space = chat_service.get_or_create_for_review(
                review_id, workspace_team_id=team.get("id"),
            )
            if space is not None:
                chat_space_id = space.id
                notify_chat_space_created(space.id)
        except Exception as exc:
            logger.exception("Failed to open chat space for review %s: %s", review_id, exc)

        # Update the original Slack message.
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        email_note = (
            "creator emailed" if email_sent
            else ("no creator email on file" if not creator_email
                  else "email failed — check logs")
        )

        if channel_id and ts:
            try:
                original = client.conversations_history(
                    channel=channel_id, latest=ts, inclusive=True, limit=1
                )
                messages = original.get("messages") or []
                original_blocks = messages[0].get("blocks") if messages else []
            except Exception as e:
                logger.warning(f"Could not fetch original review message: {e}")
                original_blocks = []

            updated_blocks = _strip_review_action_buttons(
                original_blocks or [], review_id
            )
            updated_blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":memo: *Feedback sent to creator:*\n>{feedback}",
                    },
                }
            )
            updated_blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                f":pencil2: *Changes requested* by <@{actor_id}> — "
                                f"@{creator_username} ({timestamp}) · {email_note}"
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
                    f"Failed to update message after review_changes_modal: {e}"
                )

        logger.info(
            f"review_changes_requested: review_id={review_id} "
            f"creator=@{creator_username} actor=@{actor_name} "
            f"email_sent={email_sent}"
        )

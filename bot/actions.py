"""
Slack interactive action handlers for INFLUENCE Bot.
Handles button clicks on notification messages.
"""

import logging
from datetime import datetime, timezone

from slack_sdk import WebClient
from sqlalchemy.exc import IntegrityError

from models.models import (
    PaymentRecord,
    ReviewSubmission,
    SessionLocal,
    SlackInstallation,
)
from services import chat_service
from services.review_approval import approve_review_core
from templates.slack_blocks import build_review_submitted_blocks

logger = logging.getLogger(__name__)


def _strip_review_action_buttons(blocks: list[dict], review_id: int) -> list[dict]:
    """Remove the Approve/Request Changes action row for this review."""
    target_block_id = f"review_actions_{review_id}"
    return [
        b for b in blocks
        if not (b.get("type") == "actions" and b.get("block_id") == target_block_id)
    ]


def _mark_brand_review_approved(review_id: int) -> None:
    """
    Reflect an INFLUENCE (admin) workspace approval on the brand's own copy of
    the review message.

    When the INFLUENCE team clicks Approve in our workspace, the brand still
    sees a live Approve / Request Changes message in their workspace. This
    rebuilds that message with the action buttons removed and an
    "Approved by the INFLUENCE team" footer — deliberately without naming the
    approver. Best-effort; never raises.
    """
    space = chat_service.find_by_review_id(review_id)
    if space is None or not space.brand_slack_ts or not space.brand_slack_channel:
        return
    if not space.brand_install_id:
        return

    db = SessionLocal()
    try:
        install = db.query(SlackInstallation).get(space.brand_install_id)
        review = db.query(ReviewSubmission).get(review_id)
        if install is None or not install.bot_token or review is None:
            return
        bot_token = install.bot_token
        creator_username = review.creator_username
        campaign_name = review.campaign_name or ""
        brand_name = review.brand_name or ""
        video_link = review.video_link or ""
        notes = review.notes or ""
    finally:
        db.close()

    # Rebuild the brand-side card without any action buttons (review_id=None),
    # then append the approval footer.
    blocks = build_review_submitted_blocks(
        creator_username=creator_username,
        campaign_name=campaign_name,
        brand_name=brand_name,
        video_link=video_link,
        notes=notes,
        review_id=None,
        show_meta=False,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f":white_check_mark: *Approved by the INFLUENCE team* — "
                        f"@{creator_username} ({timestamp})"
                    ),
                }
            ],
        }
    )

    try:
        WebClient(token=bot_token).chat_update(
            channel=space.brand_slack_channel,
            ts=space.brand_slack_ts,
            text=f"Review approved for @{creator_username}",
            blocks=blocks,
        )
    except Exception as exc:
        logger.warning(
            "Could not update brand review message on admin approval "
            "(review_id=%s): %s",
            review_id, exc,
        )


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
            # Slack coordinates of the admin (#content-reviews) copy, captured
            # when it was posted. Used below to tell whether this click came
            # from the admin workspace vs. the brand's own copy.
            admin_slack_ts = row.slack_ts
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

        # If the approval came from the INFLUENCE (admin) workspace copy,
        # mirror it onto the brand's own copy: strip their buttons and show
        # that the INFLUENCE team approved (without naming the approver). A
        # brand-side approval already updates the brand copy in place above,
        # so only do this for admin-originated clicks.
        clicked_from_admin = bool(ts and admin_slack_ts and ts == admin_slack_ts)
        if clicked_from_admin:
            try:
                _mark_brand_review_approved(review_id)
            except Exception as exc:
                logger.warning(
                    "Failed to mirror admin approval to brand workspace "
                    "(review_id=%s): %s",
                    review_id, exc,
                )

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

            creator_username = row.creator_username
        finally:
            db.close()

        # Ensure a chat space exists (idempotent — pre-created at review
        # post time, but this is the safety net if PUBLIC_BASE_URL was
        # missing then). Opening the chat space deliberately sends the
        # creator no notification: they're only emailed once the brand
        # actually posts a message in the chat (see
        # services.chat_notifications.notify_new_message).
        try:
            team = body.get("team") or {}
            chat_service.get_or_create_for_review(
                review_id, workspace_team_id=team.get("id"),
            )
        except Exception as exc:
            logger.exception(
                "Failed to ensure chat space for review %s: %s", review_id, exc
            )

        # Append a short "Chat opened" footer to the brand's Slack message on
        # the first Request-Changes click only. Subsequent clicks on the same
        # review just re-open the chat via the button URL, so an extra footer
        # per click would only clutter the message.
        if first_time and channel_id and ts:
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
            f"first_time={first_time}"
        )

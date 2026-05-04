"""
Slack event handlers for INFLUENCE Bot.
Handles messages, app mentions, and team join events.
"""

import logging

from sqlalchemy.exc import IntegrityError

from models.models import ReviewComment, ReviewSubmission, SessionLocal
from services.email_service import EmailService
from templates.email_templates import review_thread_comment

logger = logging.getLogger(__name__)

_email_service = EmailService()


def _handle_review_thread_reply(event, client) -> bool:
    """
    If this message is a thread reply on a review message we posted,
    record it and email the creator. Returns True if it was handled as a
    review comment (so the caller can skip other message handling).
    """
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return False

    channel_id = event.get("channel")
    reply_ts = event.get("ts")
    text = (event.get("text") or "").strip()
    if not channel_id or not reply_ts or not text:
        return False
    # Skip the parent message itself (thread_ts == ts on the root message).
    if thread_ts == reply_ts:
        return False

    db = SessionLocal()
    try:
        review = (
            db.query(ReviewSubmission)
            .filter_by(slack_channel=channel_id, slack_ts=thread_ts)
            .first()
        )
        if review is None:
            return False

        comment = ReviewComment(
            review_id=review.id,
            slack_user_id=event.get("user"),
            slack_ts=reply_ts,
            text=text,
        )
        db.add(comment)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return True  # already captured

        creator_email = review.creator_email
        creator_username = review.creator_username
        brand_name = review.brand_name or ""
        comment_id = comment.id
    finally:
        db.close()

    # Resolve the commenter's display name (best effort) and email creator.
    commenter_display = event.get("user") or "Someone"
    try:
        info = client.users_info(user=event.get("user"))
        profile = (info.get("user") or {}).get("profile", {}) or {}
        commenter_display = (
            profile.get("display_name")
            or profile.get("real_name")
            or commenter_display
        )
    except Exception as e:
        logger.warning(f"users_info failed for commenter: {e}")

    emailed = False
    if creator_email:
        template = review_thread_comment(
            creator_name=creator_username,
            brand_name=brand_name,
            commenter=commenter_display,
            comment=text,
        )
        emailed = _email_service.send_followup(creator_email, template)

    if emailed:
        db = SessionLocal()
        try:
            row = db.query(ReviewComment).get(comment_id)
            if row is not None:
                row.emailed_to_creator = True
                db.commit()
        finally:
            db.close()

    logger.info(
        f"review thread reply captured: review_id={review.id} "
        f"creator=@{creator_username} commenter={commenter_display} "
        f"emailed={emailed}"
    )
    return True


def register_event_handlers(app):
    """Register all Slack event listeners on the Bolt app."""

    @app.event("app_mention")
    def handle_app_mention(event, say):
        """When the bot is @mentioned, respond with a helpful message."""
        user = event.get("user")
        say(
            f"Hey <@{user}>! :wave: I'm the *INFLUENCE Bot*.\n\n"
            f"Here's what I can do:\n"
            f"- `/influence-status` — View active campaign statuses\n"
            f"- `/influence-check` — Manually run all notification checks\n"
            f"- `/influence-help` — See all available commands\n\n"
            f"I also automatically:\n"
            f"- Send milestone alerts when creators hit view targets\n"
            f"- Flag creators for payment when deliverables are complete\n"
            f"- Send deadline reminders (3 days, 1 day, overdue)\n"
            f"- Post a daily payment summary at 9 AM"
        )

    @app.event("message")
    def handle_message(event, client, say):
        """
        Handle incoming messages. Filter out bot messages to avoid loops.
        """
        if event.get("bot_id") or event.get("subtype"):
            return

        # Thread replies on review messages get captured and emailed to creator.
        if _handle_review_thread_reply(event, client):
            return

        text = (event.get("text") or "").lower()

        if "help" in text and "influence" in text:
            say(
                "Need help? Try one of these commands:\n"
                "- `/influence-status` — Campaign overview\n"
                "- `/influence-help` — Full command list"
            )

    @app.event("team_join")
    def handle_team_join(event, client):
        """Welcome new team members."""
        user_id = event.get("user", {}).get("id", "")
        if user_id:
            client.chat_postMessage(
                channel=user_id,
                text=(
                    f"Welcome to the INFLUENCE team! :tada:\n\n"
                    f"I'm the *INFLUENCE Bot* — I help track creator campaigns, "
                    f"view milestones, deadlines, and payment readiness.\n\n"
                    f"Type `/influence-help` in any channel to see what I can do!"
                ),
            )

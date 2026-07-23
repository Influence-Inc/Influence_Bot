"""
Webhook handler for ReelStats server events.

Existing events (immediate Slack messages):
- review_submitted
- video_links_submitted

Live-data events (drive scheduler checks with zero polling delay):
- views_updated
- deliverables_updated
- deadline_check
- creator_updated
"""

import logging

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import Config
from models.models import ReviewSubmission, SessionLocal
from services.brand_routing import post_to_brand_workspace
from templates.slack_blocks import (
    build_review_submitted_blocks,
    build_video_links_submitted_blocks,
)

logger = logging.getLogger(__name__)


class WebhookHandler:
    def __init__(self, slack_client: WebClient, scheduler_service=None):
        self.client = slack_client
        self.scheduler = scheduler_service
        if not Config.SLACK_BOT_TOKEN:
            logger.error(
                "SLACK_BOT_TOKEN is not set — webhook notifications will fail. "
                "Set the SLACK_BOT_TOKEN environment variable."
            )

    def _post_to_slack(
        self, channel: str, text: str, blocks: list[dict], event_label: str
    ) -> tuple[bool, str | None, str | None]:
        """
        Post a message to Slack and return (ok, resolved_channel_id, ts).
        Channel id and ts are needed by callers that want to match future
        thread replies back to the posted message.
        """
        if not channel:
            logger.error(
                f"Cannot post {event_label}: target channel is not configured."
            )
            return False, None, None

        try:
            response = self.client.chat_postMessage(
                channel=channel,
                text=text,
                blocks=blocks,
            )
            if not response.get("ok"):
                logger.error(
                    f"Slack API returned non-ok for {event_label} "
                    f"(channel={channel}): {response.data}"
                )
                return False, None, None
            return True, response.get("channel"), response.get("ts")
        except SlackApiError as e:
            err = e.response.get("error") if e.response else str(e)
            logger.error(
                f"Slack API error posting {event_label} to channel "
                f"{channel}: {err}"
            )
            return False, None, None

    def _build_brand_chat_url(self, review_id: int) -> tuple[str | None, int | None]:
        """
        Pre-create (or reuse) the chat space for this review. Returns
        `(brand_magic_link, chat_space_id)`. The URL is `None` — and the
        caller renders the Request Changes button without a `url` field —
        if PUBLIC_BASE_URL isn't configured or chat creation fails. The
        button still works as a plain action button in that case. The
        chat_space_id is returned alongside so the caller can persist the
        brand-workspace message ts for chat-reply threading.
        """
        if not Config.PUBLIC_BASE_URL:
            return None, None
        try:
            from services import chat_service
            from utils.chat_tokens import make_invite_token

            space = chat_service.get_or_create_for_review(review_id)
            if space is None:
                return None, None
            token = make_invite_token(chat_space_id=space.id, party="brand")
            url = f"{Config.PUBLIC_BASE_URL.rstrip('/')}/chat/invite/{token}"
            return url, space.id
        except Exception as exc:
            logger.warning("Could not pre-build brand chat URL: %s", exc)
            return None, None

    @staticmethod
    def _anchor_chat_space_to_brand_message(
        *, chat_space_id: int, channel: str | None, ts: str
    ) -> None:
        """
        Persist (brand_slack_channel, brand_slack_ts) on the chat space so
        future chat-reply notifications post as Slack thread replies under
        this brand-workspace review_submitted message.
        """
        from models.models import ChatSpace

        db = SessionLocal()
        try:
            space = db.query(ChatSpace).get(chat_space_id)
            if space is None:
                return
            if channel:
                space.brand_slack_channel = channel
            space.brand_slack_ts = ts
            db.commit()
        except Exception as exc:
            logger.warning(
                "Could not anchor chat_space %s to brand message ts=%s: %s",
                chat_space_id, ts, exc,
            )
        finally:
            db.close()

    def handle_event(self, payload: dict) -> bool:
        """Route an incoming webhook event to the appropriate handler."""
        event_type = payload.get("event")

        # Respect TEST_CAMPAIGN_NAME: drop webhooks for other campaigns.
        test_campaign_name = Config.TEST_CAMPAIGN_NAME
        if test_campaign_name:
            campaign_name = payload.get("campaign", {}).get("name")
            if campaign_name != test_campaign_name:
                logger.info(
                    f"Dropping webhook '{event_type}' for '{campaign_name}' "
                    f"(TEST_CAMPAIGN_NAME='{test_campaign_name}')"
                )
                return True

        if event_type == "review_submitted":
            return self._handle_review_submitted(payload)
        elif event_type == "video_links_submitted":
            return self._handle_video_links_submitted(payload)
        elif event_type == "views_updated":
            return self._run_checks(payload, ["milestones"])
        elif event_type == "deliverables_updated":
            return self._run_checks(payload, ["deliverables", "upload_followup"])
        elif event_type == "deadline_check":
            return self._run_checks(payload, ["deadline", "upload_followup"])
        elif event_type == "creator_updated":
            return self._run_checks(
                payload,
                ["milestones", "deliverables", "deadline", "upload_followup"],
            )
        elif event_type in ("campaign_ended", "campaign_completed", "campaign_archived"):
            return self._handle_campaign_ended(payload)
        else:
            logger.warning(f"Unknown webhook event type: {event_type}")
            return False

    # ------------------------------------------------------------------
    # Campaign lifecycle
    # ------------------------------------------------------------------
    def _handle_campaign_ended(self, payload: dict) -> bool:
        """Archive any chat spaces tied to a campaign that has ended."""
        try:
            from services.chat_service import archive_for_campaign

            campaign = payload.get("campaign") or {}
            archived = archive_for_campaign(
                campaign_slug=campaign.get("slug"),
                campaign_name=campaign.get("name"),
                brand_name=campaign.get("brandName") or campaign.get("brand_name"),
            )
            logger.info(
                "Archived %d chat space(s) for campaign=%s (slug=%s)",
                archived, campaign.get("name"), campaign.get("slug"),
            )
            return True
        except Exception as exc:
            logger.exception("Failed to archive chat spaces on campaign end: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Existing handlers
    # ------------------------------------------------------------------
    def _handle_review_submitted(self, payload: dict) -> bool:
        """Handle when a creator submits a video for review."""
        try:
            campaign = payload.get("campaign", {})
            creator = payload.get("creator", {})
            review = payload.get("review", {})

            username = creator.get("username") or "Unknown"
            campaign_name = campaign.get("name") or "Unknown Campaign"
            brand_name = campaign.get("brandName") or campaign.get("brand_name") or ""
            video_link = review.get("videoLink") or review.get("video_link") or ""
            notes = review.get("notes", "") or ""
            submission_links = creator.get("submissionLinks") or {}
            submit_posts_url = submission_links.get("submitPostsUrl") or None

            if not video_link:
                logger.warning(
                    f"review_submitted payload for @{username} on "
                    f"{campaign_name} has no videoLink field"
                )

            db = SessionLocal()
            try:
                submission = ReviewSubmission(
                    campaign_slug=campaign.get("slug"),
                    campaign_name=campaign_name,
                    brand_name=brand_name,
                    creator_username=username,
                    creator_email=creator.get("email"),
                    video_link=video_link,
                    notes=notes,
                    submit_posts_url=submit_posts_url,
                )
                db.add(submission)
                db.commit()
                review_id = submission.id
            finally:
                db.close()

            # Pre-create (or reuse) the chat space NOW so the brand's magic
            # link can be baked into the Request Changes button. The button
            # uses both `url` and `action_id`: clicking it opens the chat in
            # the brand's browser AND fires our backend handler, which
            # records the decision + emails the creator.
            brand_chat_url, chat_space_id = self._build_brand_chat_url(review_id)

            # The admin (#content-reviews) copy gets an "Open as Admin" button
            # into the admin side of the same chat space instead of a Request
            # Changes button — the INFLUENCE team enters as admins, not the
            # brand.
            admin_chat_url = None
            if Config.PUBLIC_BASE_URL and chat_space_id:
                admin_chat_url = (
                    f"{Config.PUBLIC_BASE_URL.rstrip('/')}/admin/chats/{chat_space_id}"
                )

            admin_blocks = build_review_submitted_blocks(
                creator_username=username,
                campaign_name=campaign_name,
                brand_name=brand_name,
                video_link=video_link,
                notes=notes,
                review_id=review_id,
                show_meta=True,
                admin_chat_url=admin_chat_url,
            )
            text = f"New review submitted by @{username} for {campaign_name}"

            ok, resolved_channel, ts = self._post_to_slack(
                channel=Config.SLACK_CHANNEL_REVIEWS,
                text=text,
                blocks=admin_blocks,
                event_label="review_submitted",
            )

            if ok and ts:
                db = SessionLocal()
                try:
                    row = db.query(ReviewSubmission).get(review_id)
                    if row is not None:
                        row.slack_channel = resolved_channel
                        row.slack_ts = ts
                        db.commit()
                finally:
                    db.close()

            # Mirror to the brand's own workspace with the same Request
            # Changes button (URL-baked). The first click (admin or brand)
            # wins via the DB-level "already decided" guard in bot/actions.py.
            brand_blocks = build_review_submitted_blocks(
                creator_username=username,
                campaign_name=campaign_name,
                brand_name=brand_name,
                video_link=video_link,
                notes=notes,
                review_id=review_id,
                show_meta=False,
                chat_url=brand_chat_url,
            )
            brand_channel, brand_ts = post_to_brand_workspace(
                brand_name, text, brand_blocks
            )

            # Anchor chat-reply notifications to this review_submitted message
            # in the brand workspace so each chat reply lands as a thread
            # reply under it (instead of a top-level message). On reuse, the
            # chat space "moves" to the newest review's brand-workspace post.
            if chat_space_id and brand_ts:
                self._anchor_chat_space_to_brand_message(
                    chat_space_id=chat_space_id,
                    channel=brand_channel,
                    ts=brand_ts,
                )

            if ok:
                logger.info(
                    f"Review submitted notification sent to "
                    f"{Config.SLACK_CHANNEL_REVIEWS}: "
                    f"@{username} for {campaign_name} "
                    f"(review_id={review_id}, link={video_link or 'none'})"
                )
            return ok

        except Exception as e:
            logger.exception(f"Failed to handle review_submitted: {e}")
            return False

    def _handle_video_links_submitted(self, payload: dict) -> bool:
        """Handle when a creator submits video links (posted content)."""
        try:
            campaign = payload.get("campaign", {})
            creator = payload.get("creator", {})
            video = payload.get("video", {})

            username = creator.get("username") or "Unknown"
            campaign_name = campaign.get("name") or "Unknown Campaign"

            links = []
            for platform in ("instagram", "tiktok", "youtube"):
                url = video.get(platform)
                if url:
                    links.append({"platform": platform, "url": url})

            if not links:
                logger.warning(
                    f"video_links_submitted payload for @{username} on "
                    f"{campaign_name} contains no platform URLs"
                )

            brand_name = campaign.get("brandName") or campaign.get("brand_name") or ""
            video_title = video.get("title", "")
            admin_blocks = build_video_links_submitted_blocks(
                creator_username=username,
                campaign_name=campaign_name,
                brand_name=brand_name,
                video_title=video_title,
                links=links,
                show_meta=True,
            )
            text = f"Video links submitted by @{username} for {campaign_name}"

            ok, _channel, _ts = self._post_to_slack(
                channel=Config.SLACK_CHANNEL_UPLOADS,
                text=text,
                blocks=admin_blocks,
                event_label="video_links_submitted",
            )

            # Mirror to the brand's own workspace (no Brand/Campaign rows
            # there — the workspace itself identifies the brand).
            brand_blocks = build_video_links_submitted_blocks(
                creator_username=username,
                campaign_name=campaign_name,
                brand_name=brand_name,
                video_title=video_title,
                links=links,
                show_meta=False,
            )
            post_to_brand_workspace(brand_name, text, brand_blocks)

            if ok:
                logger.info(
                    f"Video links submitted notification sent to "
                    f"{Config.SLACK_CHANNEL_UPLOADS}: "
                    f"@{username} for {campaign_name} "
                    f"(platforms={[l['platform'] for l in links]})"
                )
            return ok

        except Exception as e:
            logger.exception(f"Failed to handle video_links_submitted: {e}")
            return False

    # ------------------------------------------------------------------
    # Live-data handlers
    # ------------------------------------------------------------------
    def _run_checks(self, payload: dict, checks: list[str]) -> bool:
        """Run the named per-creator scheduler checks against the payload."""
        if self.scheduler is None:
            logger.error("No scheduler wired into WebhookHandler; cannot run checks")
            return False

        creator = self._flatten_creator(payload)
        if not creator.get("username") or not creator.get("campaign_id"):
            logger.warning(
                f"Live-data webhook missing username/campaign_id: {payload!r}"
            )
            return False

        try:
            if "milestones" in checks:
                self.scheduler.check_milestones_for(creator)
            if "deliverables" in checks:
                self.scheduler.check_deliverables_complete_for(creator)
            if "deadline" in checks:
                self.scheduler.check_deadline_reminder_for(creator)
            if "upload_followup" in checks:
                self.scheduler.check_upload_followup_for(creator)
            return True
        except Exception as e:
            logger.error(
                f"Failed running {checks} for @{creator.get('username')}: {e}"
            )
            return False

    @staticmethod
    def _flatten_creator(payload: dict) -> dict:
        """Normalize a webhook payload into the scheduler's flat creator dict."""
        campaign = payload.get("campaign", {}) or {}
        creator = payload.get("creator", {}) or {}
        return {
            **creator,
            "campaign_id": campaign.get("id", ""),
            "campaign_name": campaign.get("name", ""),
            "brand_name": campaign.get("brandName", ""),
            "campaign_slug": campaign.get("slug", ""),
        }

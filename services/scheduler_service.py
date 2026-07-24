"""
Scheduler for INFLUENCE Bot.
Polls the ReelStats API every 5 minutes and runs all notification checks:
- View milestones (250k, 500k, 1M, 1.5M, 2M, 5M, 10M, ...)
- Deliverables complete → payment flag
- Deadline reminders (3 days, 1 day, overdue)
- Upload follow-ups (videos behind schedule within 5 days of deadline)
- Daily payment summary
"""

import logging
from datetime import datetime, date, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from slack_sdk import WebClient
from sqlalchemy.exc import IntegrityError

from config import Config
from models.models import (
    SessionLocal,
    AppState,
    MilestoneAlert,
    DeliverableAlert,
    DeadlineReminder,
    UploadFollowup,
    EmailLog,
    PaymentRecord,
)
from services.brand_routing import post_to_brand_workspace
from services.reelstats_api import ReelStatsAPI
from services.email_service import EmailService, EmailSendResult
from services.review_approval import run_auto_approval_sweep
from templates.slack_blocks import (
    _format_upload_date,
    build_milestone_blocks,
    build_deliverable_complete_blocks,
    build_deadline_reminder_blocks,
    build_upload_followup_blocks,
    build_payment_summary_blocks,
)

logger = logging.getLogger(__name__)

MILESTONE_THRESHOLDS = [
    250_000, 500_000, 1_000_000, 1_500_000, 2_000_000,
    5_000_000, 10_000_000, 20_000_000, 50_000_000, 100_000_000,
]

# AppState key marking that the silent notification baseline has been recorded.
BASELINE_STATE_KEY = "notification_baseline_done"


class SchedulerService:
    def __init__(
        self,
        slack_client: WebClient,
        email_service: EmailService,
        reelstats_api: ReelStatsAPI,
    ):
        self.client = slack_client
        self.email_service = email_service
        self.api = reelstats_api
        self.scheduler = BackgroundScheduler()

    def start(self):
        """Start the scheduler with all polling jobs."""
        poll_seconds = Config.POLL_INTERVAL_SECONDS

        # Main polling job — safety-net fallback for missed webhook events.
        self.scheduler.add_job(
            self.run_all_checks,
            trigger=IntervalTrigger(seconds=poll_seconds),
            id="poll_and_check",
            name=f"Poll ReelStats API every {poll_seconds}s and run all checks",
            replace_existing=True,
        )

        # Daily payment summary at 9 AM
        self.scheduler.add_job(
            self.send_payment_summary,
            trigger="cron",
            hour=9,
            minute=0,
            id="daily_payment_summary",
            name="Daily payment summary",
            replace_existing=True,
        )

        # 24h auto-approval sweep — runs every 30 min so the worst-case
        # auto-approve latency is 24h + 30m. Reviews are auto-approved
        # if (a) no button was clicked within 24h of submission, or
        # (b) Request Changes was clicked but the chat stayed empty for
        # 24h.
        self.scheduler.add_job(
            run_auto_approval_sweep,
            trigger=IntervalTrigger(minutes=30),
            id="auto_approval_sweep",
            name="Auto-approve reviews stale for 24h",
            replace_existing=True,
        )

        self.scheduler.start()
        logger.info(
            f"Scheduler started: polling every {poll_seconds}s, "
            f"daily summary at 9 AM, auto-approval sweep every 30m"
        )

    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler shut down")

    def run_all_checks(self):
        """Fetch data from ReelStats API and run all notification checks."""
        creators = self.api.get_all_creators()
        if not creators:
            logger.info("No creators returned from API, skipping checks")
            return

        # First run after a fresh deploy: the dedup tables may be empty (on
        # Railway, SQLite lives on the ephemeral filesystem and is wiped every
        # redeploy). Record the current state silently so pre-existing
        # milestones/deadlines/deliverables/uploads aren't re-announced — only
        # genuinely new events after this point notify. The watermark lives in
        # the same DB: on ephemeral storage it re-seeds each deploy; on a
        # persistent DB it runs exactly once.
        if not self._baseline_done():
            recorded = self.seed_baseline(creators)
            self._mark_baseline_done()
            logger.info(
                "Notification baseline recorded silently (%d dedup rows) — "
                "suppressing pre-existing notifications for this deploy. "
                "Only new events from here on will be posted.",
                recorded,
            )
            return

        logger.info(f"Running checks on {len(creators)} creators")

        self.check_milestones(creators)
        self.check_deliverables_complete(creators)
        self.check_deadline_reminders(creators)
        self.check_upload_followups(creators)

    # ------------------------------------------------------------------
    # Silent baseline (redeploy suppression)
    # ------------------------------------------------------------------
    def _baseline_done(self) -> bool:
        """True once the silent baseline has been recorded for this DB."""
        db = SessionLocal()
        try:
            row = db.query(AppState).filter_by(key=BASELINE_STATE_KEY).first()
            return row is not None and row.value == "done"
        finally:
            db.close()

    def _mark_baseline_done(self):
        """Persist the baseline watermark so live checks run from now on."""
        db = SessionLocal()
        try:
            row = db.query(AppState).filter_by(key=BASELINE_STATE_KEY).first()
            if row is None:
                db.add(AppState(key=BASELINE_STATE_KEY, value="done"))
            else:
                row.value = "done"
            db.commit()
        except IntegrityError:
            db.rollback()
        finally:
            db.close()

    def seed_baseline(self, creators: list[dict]) -> int:
        """
        Record the current notification-worthy state into the dedup tables
        WITHOUT sending any Slack message or email. Mirrors the qualifying
        predicate of each check_* method so a live run immediately after finds
        every pre-existing item already deduped. Idempotent.
        """
        db = SessionLocal()
        recorded = 0
        try:
            today = date.today()
            for creator in creators:
                username = creator.get("username", "")
                campaign_id = creator.get("campaign_id", "")
                deliverables = creator.get("deliverables", {}) or {}
                all_complete = deliverables.get("allComplete") is True

                # Milestones — per video, per threshold already crossed.
                for video in (creator.get("videos") or []):
                    video_id = video.get("id") or ""
                    if not video_id:
                        continue
                    video_views = video.get("totalViews", 0) or 0
                    for threshold in MILESTONE_THRESHOLDS:
                        if video_views >= threshold:
                            recorded += self._seed_row(
                                db, MilestoneAlert,
                                campaign_id=campaign_id,
                                creator_username=username,
                                video_id=video_id,
                                milestone_value=threshold,
                            )

                # Deliverables complete.
                if all_complete:
                    recorded += self._seed_row(
                        db, DeliverableAlert,
                        campaign_id=campaign_id,
                        creator_username=username,
                    )

                # Deadline reminder for the current tier (skip finished
                # creators, matching check_deadline_reminder_for). Also seed the
                # EmailLog row so the creator isn't re-emailed on redeploy.
                if not all_complete:
                    reminder_type = _deadline_reminder_type(
                        creator.get("deadline"), today
                    )
                    if reminder_type:
                        recorded += self._seed_row(
                            db, DeadlineReminder,
                            campaign_id=campaign_id,
                            creator_username=username,
                            reminder_type=reminder_type,
                        )
                        email = creator.get("email")
                        if email:
                            recorded += self._seed_row(
                                db, EmailLog,
                                recipient_email=email,
                                template_type=f"deadline_{reminder_type}",
                                campaign_id=campaign_id,
                                creator_username=username,
                            )

                # Upload follow-up within the 5-day window.
                min_videos = deliverables.get("minVideos")
                if min_videos is not None:
                    total_posted = creator.get("totalVideosPosted", 0)
                    deadline_str = creator.get("deadline")
                    if total_posted < min_videos and deadline_str:
                        try:
                            days_left = (date.fromisoformat(deadline_str) - today).days
                        except (ValueError, TypeError):
                            days_left = None
                        if days_left is not None and 0 <= days_left <= 5:
                            recorded += self._seed_row(
                                db, UploadFollowup,
                                campaign_id=campaign_id,
                                creator_username=username,
                            )
        finally:
            db.close()
        return recorded

    @staticmethod
    def _seed_row(db, model, **fields) -> int:
        """Insert a dedup row if absent. Returns 1 if inserted, else 0."""
        existing = db.query(model).filter_by(**fields).first()
        if existing:
            return 0
        db.add(model(**fields))
        try:
            db.commit()
            return 1
        except IntegrityError:
            db.rollback()
            return 0

    # ------------------------------------------------------------------
    # View Milestones
    # ------------------------------------------------------------------
    def check_milestones(self, creators: list[dict]):
        """Check if any individual post crossed a milestone threshold."""
        for creator in creators:
            self.check_milestones_for(creator)

    def check_milestones_for(self, creator: dict):
        """
        Run milestone check for each of the creator's posts. A milestone
        fires when an individual video's view count crosses 250K, 500K,
        1M, etc. — not the creator's combined total across posts.
        """
        username = creator.get("username", "")
        campaign_id = creator.get("campaign_id", "")
        videos = creator.get("videos") or []

        for video in videos:
            video_id = video.get("id") or ""
            if not video_id:
                continue
            video_views = video.get("totalViews", 0) or 0

            for threshold in MILESTONE_THRESHOLDS:
                if video_views < threshold:
                    continue
                self._record_and_notify_milestone(
                    creator=creator,
                    video=video,
                    campaign_id=campaign_id,
                    username=username,
                    video_id=video_id,
                    threshold=threshold,
                    video_views=video_views,
                )

    def _record_and_notify_milestone(
        self,
        creator: dict,
        video: dict,
        campaign_id: str,
        username: str,
        video_id: str,
        threshold: int,
        video_views: int,
    ):
        db = SessionLocal()
        try:
            existing = (
                db.query(MilestoneAlert)
                .filter_by(
                    campaign_id=campaign_id,
                    creator_username=username,
                    video_id=video_id,
                    milestone_value=threshold,
                )
                .first()
            )
            if existing:
                return

            alert = MilestoneAlert(
                campaign_id=campaign_id,
                creator_username=username,
                video_id=video_id,
                milestone_value=threshold,
            )
            db.add(alert)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return

            self._send_milestone_notification(creator, video, threshold, video_views)
        except Exception as e:
            logger.error(
                f"Error recording milestone for @{username} "
                f"video={video_id} threshold={threshold}: {e}"
            )
        finally:
            db.close()

    def _send_milestone_notification(
        self, creator: dict, video: dict, milestone: int, video_views: int
    ):
        milestone_label = _format_views(milestone)
        username = creator.get("username", "")
        campaign_name = creator.get("campaign_name", "")
        brand_name = creator.get("brand_name", "")
        video_link = _primary_video_link(video)
        first_posted = _format_upload_date(video.get("uploadDate") or "")

        admin_blocks = build_milestone_blocks(
            creator_username=username,
            campaign_name=campaign_name,
            brand_name=brand_name,
            milestone_label=milestone_label,
            first_posted=first_posted,
            video_link=video_link,
            include_brand=True,
        )
        brand_blocks = build_milestone_blocks(
            creator_username=username,
            campaign_name=campaign_name,
            brand_name=brand_name,
            milestone_label=milestone_label,
            first_posted=first_posted,
            video_link=video_link,
            include_brand=False,
        )
        text = f"Breakout video alert — @{username} hit {milestone_label} views on {campaign_name}"
        self.client.chat_postMessage(
            channel=Config.SLACK_CHANNEL_MILESTONES,
            text=text,
            blocks=admin_blocks,
        )
        post_to_brand_workspace(brand_name, text, brand_blocks)
        logger.info(
            f"Milestone alert: @{username} hit {milestone_label} views "
            f"({video_views}) on video={video.get('id')}"
        )

    # ------------------------------------------------------------------
    # Deliverables Complete
    # ------------------------------------------------------------------
    def check_deliverables_complete(self, creators: list[dict]):
        """Check if any creator's deliverables.allComplete flipped to true."""
        for creator in creators:
            self.check_deliverables_complete_for(creator)

    def check_deliverables_complete_for(self, creator: dict):
        """Run deliverables-complete check for a single creator dict."""
        deliverables = creator.get("deliverables", {}) or {}
        if deliverables.get("allComplete") is not True:
            return

        username = creator.get("username", "")
        campaign_id = creator.get("campaign_id", "")

        db = SessionLocal()
        try:
            existing = (
                db.query(DeliverableAlert)
                .filter_by(
                    campaign_id=campaign_id,
                    creator_username=username,
                )
                .first()
            )
            if existing:
                return

            alert = DeliverableAlert(
                campaign_id=campaign_id,
                creator_username=username,
            )
            db.add(alert)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return

            blocks = build_deliverable_complete_blocks(
                creator_username=username,
                campaign_name=creator.get("campaign_name", ""),
                brand_name=creator.get("brand_name", ""),
                campaign_id=campaign_id,
            )
            text = (
                f"Deliverables complete! @{username} partnering with "
                f"{creator.get('brand_name')} has completed their "
                f"deliverables and is supposed to be paid."
            )
            self.client.chat_postMessage(
                channel=Config.SLACK_CHANNEL_PAYMENTS,
                text=text,
                blocks=blocks,
            )
            # Payment alerts are admin-only: brands don't see "ready to pay"
            # messages for their own creators.
            logger.info(f"Deliverable complete alert: @{username}")
        except Exception as e:
            logger.error(f"Error checking deliverables for @{username}: {e}")
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Deadline Reminders
    # ------------------------------------------------------------------
    def check_deadline_reminders(self, creators: list[dict]):
        """Send reminders at 3 days before, 1 day before, and overdue."""
        for creator in creators:
            self.check_deadline_reminder_for(creator)

    def check_deadline_reminder_for(self, creator: dict):
        """Run deadline-reminder check for a single creator dict."""
        # Skip creators who've already finished — deliverables.allComplete
        # is set on the campaign page once views + videos are both met,
        # at which point a deadline reminder is just noise.
        deliverables = creator.get("deliverables", {}) or {}
        if deliverables.get("allComplete") is True:
            return

        deadline_str = creator.get("deadline")
        if not deadline_str:
            return

        try:
            deadline = date.fromisoformat(deadline_str)
        except ValueError:
            return

        today = date.today()
        days_left = (deadline - today).days

        if days_left < 0:
            reminder_type = "overdue"
        elif days_left <= 1:
            reminder_type = "1_day"
        elif days_left <= 3:
            reminder_type = "3_days"
        else:
            return

        username = creator.get("username", "")
        campaign_id = creator.get("campaign_id", "")
        email = creator.get("email")

        # Email dedup is independent of Slack dedup: try the email every tick
        # until it succeeds, even if the Slack message was already posted.
        email_result = None
        if email:
            from templates.email_templates import deadline_reminder_email
            template = deadline_reminder_email(
                creator_name=username,
                campaign_name=creator.get("campaign_name", ""),
                brand_name=creator.get("brand_name", ""),
                deadline=deadline_str,
                reminder_type=reminder_type,
                days_left=days_left,
            )
            email_result = self.email_service.send_followup_if_not_sent(
                to_email=email,
                template_data=template,
                template_type=f"deadline_{reminder_type}",
                campaign_id=campaign_id,
                creator_username=username,
            )

        db = SessionLocal()
        try:
            existing = (
                db.query(DeadlineReminder)
                .filter_by(
                    campaign_id=campaign_id,
                    creator_username=username,
                    reminder_type=reminder_type,
                )
                .first()
            )
            if existing:
                return

            blocks = build_deadline_reminder_blocks(
                creator_username=username,
                campaign_name=creator.get("campaign_name", ""),
                brand_name=creator.get("brand_name", ""),
                deadline=deadline_str,
                reminder_type=reminder_type,
                days_left=days_left,
            )
            text = f"Deadline reminder for @{username}: {reminder_type.replace('_', ' ')}"
            self.client.chat_postMessage(
                channel=Config.SLACK_CHANNEL_DEADLINES,
                text=text,
                blocks=blocks,
            )
            # Deadline reminders are admin-only: brands don't need to see our
            # internal nags to creators. Brand workspace only gets milestone
            # alerts, review-link drops, and post-uploaded events.

            reminder = DeadlineReminder(
                campaign_id=campaign_id,
                creator_username=username,
                reminder_type=reminder_type,
                email_sent=(email_result == EmailSendResult.SENT) if email_result else False,
            )
            db.add(reminder)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return

            logger.info(
                f"Deadline reminder ({reminder_type}): @{username}, "
                f"email_result={email_result}"
            )
        except Exception as e:
            logger.error(f"Error checking deadline for @{username}: {e}")
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Upload Follow-ups
    # ------------------------------------------------------------------
    def check_upload_followups(self, creators: list[dict]):
        """
        If a creator has totalVideosPosted < minVideos and their deadline
        is within 5 days, send a reminder.
        """
        for creator in creators:
            self.check_upload_followup_for(creator)

    def check_upload_followup_for(self, creator: dict):
        """Run upload-followup check for a single creator dict."""
        deliverables = creator.get("deliverables", {}) or {}
        min_videos = deliverables.get("minVideos")
        if min_videos is None:
            return

        total_posted = creator.get("totalVideosPosted", 0)
        if total_posted >= min_videos:
            return

        deadline_str = creator.get("deadline")
        if not deadline_str:
            return

        try:
            deadline = date.fromisoformat(deadline_str)
        except ValueError:
            return

        days_left = (deadline - date.today()).days
        if days_left > 5 or days_left < 0:
            return

        username = creator.get("username", "")
        campaign_id = creator.get("campaign_id", "")

        db = SessionLocal()
        try:
            existing = (
                db.query(UploadFollowup)
                .filter_by(
                    campaign_id=campaign_id,
                    creator_username=username,
                )
                .first()
            )
            if existing:
                return

            blocks = build_upload_followup_blocks(
                creator_username=username,
                campaign_name=creator.get("campaign_name", ""),
                brand_name=creator.get("brand_name", ""),
                videos_posted=total_posted,
                videos_required=min_videos,
                deadline=deadline_str,
                days_left=days_left,
            )
            text = (
                f"Upload reminder: @{username} has posted "
                f"{total_posted}/{min_videos} videos, "
                f"{days_left} days until deadline"
            )
            self.client.chat_postMessage(
                channel=Config.SLACK_CHANNEL_UPLOADS,
                text=text,
                blocks=blocks,
            )
            # Upload follow-ups are admin-only: this is an internal "creator
            # is behind schedule" nag, not a brand-facing event. Brands see
            # post-uploaded notifications via the post_uploaded webhook
            # handler, not via this scheduler nag.

            followup = UploadFollowup(
                campaign_id=campaign_id,
                creator_username=username,
            )
            db.add(followup)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return

            logger.info(f"Upload followup: @{username}")
        except Exception as e:
            logger.error(f"Error checking upload followup for @{username}: {e}")
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Daily Payment Summary
    # ------------------------------------------------------------------
    def send_payment_summary(self):
        """Daily summary of creators with completed deliverables not yet marked as paid."""
        creators = self.api.get_all_creators()
        completed = [
            c for c in creators
            if c.get("deliverables", {}).get("allComplete") is True
        ]

        if not completed:
            self.client.chat_postMessage(
                channel=Config.SLACK_CHANNEL_PAYMENTS,
                text=":sunrise: *Daily Payment Summary*\nNo creators with completed deliverables pending payment.",
            )
            return

        # Exclude creators already marked as paid.
        db = SessionLocal()
        try:
            paid_pairs = {
                (r.campaign_id, r.creator_username)
                for r in db.query(PaymentRecord).all()
            }
        finally:
            db.close()

        pending = [
            c for c in completed
            if (c.get("campaign_id", ""), c.get("username", "")) not in paid_pairs
        ]

        if not pending:
            self.client.chat_postMessage(
                channel=Config.SLACK_CHANNEL_PAYMENTS,
                text=":sunrise: *Daily Payment Summary*\nAll creators with completed deliverables have already been marked as paid.",
            )
            return

        blocks = build_payment_summary_blocks(pending)
        self.client.chat_postMessage(
            channel=Config.SLACK_CHANNEL_PAYMENTS,
            text=f"Daily Payment Summary: {len(pending)} creator(s) ready for payment",
            blocks=blocks,
        )
        logger.info(f"Payment summary sent: {len(pending)} creators")


def _deadline_reminder_type(deadline_str, today: date) -> str | None:
    """
    Return the current deadline reminder tier ("overdue" / "1_day" / "3_days")
    for a deadline, or None if outside the reminder window. Mirrors the tier
    logic in SchedulerService.check_deadline_reminder_for so the baseline seeds
    exactly the reminder a live check would send.
    """
    if not deadline_str:
        return None
    try:
        deadline = date.fromisoformat(deadline_str)
    except (ValueError, TypeError):
        return None
    days_left = (deadline - today).days
    if days_left < 0:
        return "overdue"
    if days_left <= 1:
        return "1_day"
    if days_left <= 3:
        return "3_days"
    return None


def _format_views(count: int) -> str:
    """Format a view count into a human-readable string (e.g. 1.5M, 500K)."""
    if count >= 1_000_000:
        val = count / 1_000_000
        return f"{val:.1f}M".replace(".0M", "M")
    elif count >= 1_000:
        val = count / 1_000
        return f"{val:.0f}K"
    return str(count)


def _primary_video_link(video: dict) -> str:
    """Pick the most useful link for a video, preferring Instagram → TikTok → YouTube."""
    links = video.get("links") or {}
    for platform in ("instagram", "tiktok", "youtube"):
        url = links.get(platform)
        if url:
            return url
    return ""

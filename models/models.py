"""
Database models for INFLUENCE Bot.
Tracks state to avoid duplicate notifications (milestones, alerts, reminders).
The ReelStats API is the source of truth for campaign data — these models
only persist notification state that the API doesn't track.
"""

from datetime import datetime, timezone

import logging

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from config import Config

logger = logging.getLogger(__name__)

Base = declarative_base()
engine = create_engine(Config.DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate_milestone_alerts_video_id()


def _migrate_milestone_alerts_video_id():
    """
    Add `video_id` column + update unique constraint on `milestone_alerts`
    when upgrading from the pre-per-post schema. Idempotent.
    """
    inspector = inspect(engine)
    if "milestone_alerts" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("milestone_alerts")}
    if "video_id" in cols:
        return

    dialect = engine.dialect.name
    logger.info("Migrating milestone_alerts: adding video_id column (dialect=%s)", dialect)

    if dialect == "sqlite":
        # SQLite can't drop a unique constraint in place — rebuild the table.
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE milestone_alerts RENAME TO milestone_alerts_old"))
            conn.execute(text(
                """
                CREATE TABLE milestone_alerts (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    campaign_id VARCHAR(255) NOT NULL,
                    creator_username VARCHAR(255) NOT NULL,
                    video_id VARCHAR(255),
                    milestone_value INTEGER NOT NULL,
                    notified_at DATETIME,
                    CONSTRAINT uq_milestone_alert
                        UNIQUE (campaign_id, creator_username, video_id, milestone_value)
                )
                """
            ))
            conn.execute(text(
                """
                INSERT INTO milestone_alerts
                    (id, campaign_id, creator_username, video_id, milestone_value, notified_at)
                SELECT id, campaign_id, creator_username, NULL, milestone_value, notified_at
                FROM milestone_alerts_old
                """
            ))
            conn.execute(text("DROP TABLE milestone_alerts_old"))
    else:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE milestone_alerts ADD COLUMN video_id VARCHAR(255)"
            ))
            try:
                conn.execute(text(
                    "ALTER TABLE milestone_alerts DROP CONSTRAINT uq_milestone_alert"
                ))
            except Exception as exc:
                logger.warning("Could not drop old uq_milestone_alert: %s", exc)
            conn.execute(text(
                "ALTER TABLE milestone_alerts ADD CONSTRAINT uq_milestone_alert "
                "UNIQUE (campaign_id, creator_username, video_id, milestone_value)"
            ))


class MilestoneAlert(Base):
    """
    Tracks which view milestones have been notified to avoid duplicates.
    Milestones are tracked per individual video (post), not per creator —
    each post that crosses 250K / 500K / 1M / ... fires its own alert.
    """
    __tablename__ = "milestone_alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(String(255), nullable=False)
    creator_username = Column(String(255), nullable=False)
    # ID of the specific video/post that hit the milestone. Nullable so legacy
    # creator-wide rows from before per-post tracking remain unique-distinct
    # from the new per-post rows.
    video_id = Column(String(255), nullable=True)
    milestone_value = Column(Integer, nullable=False)  # e.g. 250000, 500000
    notified_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "creator_username", "video_id", "milestone_value",
            name="uq_milestone_alert",
        ),
    )


class DeliverableAlert(Base):
    """Tracks which deliverable-complete alerts have been sent."""
    __tablename__ = "deliverable_alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(String(255), nullable=False)
    creator_username = Column(String(255), nullable=False)
    notified_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "creator_username",
            name="uq_deliverable_alert",
        ),
    )


class DeadlineReminder(Base):
    """Tracks which deadline reminders have been sent (3 days, 1 day, overdue)."""
    __tablename__ = "deadline_reminders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(String(255), nullable=False)
    creator_username = Column(String(255), nullable=False)
    reminder_type = Column(String(50), nullable=False)  # "3_days", "1_day", "overdue"
    notified_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Deprecated: email dedup now lives in the EmailLog table.
    email_sent = Column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "creator_username", "reminder_type",
            name="uq_deadline_reminder",
        ),
    )


class EmailLog(Base):
    """Tracks which follow-up emails have been successfully sent (dedup)."""
    __tablename__ = "email_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    recipient_email = Column(String(320), nullable=False)
    template_type = Column(String(64), nullable=False)
    campaign_id = Column(String(255), nullable=False)
    creator_username = Column(String(255), nullable=False)
    sent_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint(
            "recipient_email", "template_type",
            "campaign_id", "creator_username",
            name="uq_email_log",
        ),
    )


class UploadFollowup(Base):
    """Tracks upload follow-up reminders sent within the 5-day window."""
    __tablename__ = "upload_followups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(String(255), nullable=False)
    creator_username = Column(String(255), nullable=False)
    notified_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "creator_username",
            name="uq_upload_followup",
        ),
    )


class ReviewSubmission(Base):
    """
    One row per review_submitted webhook. Stores context needed to respond to
    Approve / Request Changes button clicks, plus the Slack message coordinates
    so thread replies can be matched back to the review.
    """
    __tablename__ = "review_submissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_slug = Column(String(255), nullable=True)
    campaign_name = Column(String(255), nullable=True)
    brand_name = Column(String(255), nullable=True)
    creator_username = Column(String(255), nullable=False)
    creator_email = Column(String(255), nullable=True)
    video_link = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)

    slack_channel = Column(String(255), nullable=True)
    slack_ts = Column(String(255), nullable=True)

    submitted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Decision state: NULL until the brand clicks a button.
    decision = Column(String(50), nullable=True)  # "approved" | "changes_requested"
    decision_feedback = Column(Text, nullable=True)
    decided_by_id = Column(String(255), nullable=True)
    decided_by_name = Column(String(255), nullable=True)
    decided_at = Column(DateTime, nullable=True)

    comments = relationship("ReviewComment", back_populates="review", cascade="all, delete-orphan")


class ReviewComment(Base):
    """Thread reply on a review message, captured so the creator can be looped in."""
    __tablename__ = "review_comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_id = Column(Integer, ForeignKey("review_submissions.id"), nullable=False)
    slack_user_id = Column(String(255), nullable=True)
    slack_user_name = Column(String(255), nullable=True)
    slack_ts = Column(String(255), nullable=True)
    text = Column(Text, nullable=False)
    emailed_to_creator = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    review = relationship("ReviewSubmission", back_populates="comments")

    __table_args__ = (
        UniqueConstraint("slack_ts", name="uq_review_comment_ts"),
    )


class SlackInstallation(Base):
    """
    Per-workspace Slack install record, populated by the OAuth callback.
    One row per (team_id, brand) install. The bot uses `bot_token` to post
    into `channel_id` (the channel the installing user picked during OAuth,
    surfaced via the `incoming-webhook` scope).
    """
    __tablename__ = "slack_installations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    brand = Column(String(255), nullable=True)  # slug/name from install link
    team_id = Column(String(255), nullable=False)
    team_name = Column(String(255), nullable=True)
    enterprise_id = Column(String(255), nullable=True)

    bot_user_id = Column(String(255), nullable=True)
    bot_token = Column(Text, nullable=False)
    scope = Column(Text, nullable=True)

    # Populated when `incoming-webhook` is in scopes.
    channel_id = Column(String(255), nullable=True)
    channel_name = Column(String(255), nullable=True)
    webhook_url = Column(Text, nullable=True)

    installed_by_user_id = Column(String(255), nullable=True)
    installed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("team_id", "brand", name="uq_slack_install_team_brand"),
    )


class PaymentRecord(Base):
    """Persistent record of 'Mark as Paid' clicks."""
    __tablename__ = "payment_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(String(255), nullable=True)
    creator_username = Column(String(255), nullable=False)
    marked_by_id = Column(String(255), nullable=True)
    marked_by_name = Column(String(255), nullable=True)
    marked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "creator_username",
            name="uq_payment_record",
        ),
    )

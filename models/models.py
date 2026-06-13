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
    BigInteger,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    Index,
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
    _migrate_chat_spaces_public_slug()
    _migrate_chat_spaces_creator_invited_at()
    _migrate_review_submissions_submit_posts_url()


def _migrate_review_submissions_submit_posts_url():
    """Add `submit_posts_url` to `review_submissions` on pre-column deploys."""
    inspector = inspect(engine)
    if "review_submissions" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("review_submissions")}
    if "submit_posts_url" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE review_submissions ADD COLUMN submit_posts_url TEXT"
        ))


def _migrate_chat_spaces_creator_invited_at():
    """Add `creator_invited_at` to `chat_spaces` on pre-column deploys."""
    inspector = inspect(engine)
    if "chat_spaces" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("chat_spaces")}
    if "creator_invited_at" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE chat_spaces ADD COLUMN creator_invited_at TIMESTAMP"
        ))


def _migrate_chat_spaces_public_slug():
    """
    Add `public_slug` to `chat_spaces` on pre-slug deploys and backfill
    every row with a random URL-safe value. Idempotent.
    """
    import secrets

    inspector = inspect(engine)
    if "chat_spaces" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("chat_spaces")}
    if "public_slug" not in cols:
        # create_all() skipped this column because the table already exists;
        # add it manually. SQLite + Postgres both accept this DDL.
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE chat_spaces ADD COLUMN public_slug VARCHAR(32)"))
            try:
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_chat_spaces_public_slug "
                    "ON chat_spaces (public_slug)"
                ))
            except Exception as exc:
                logger.warning("Could not create unique index on public_slug: %s", exc)

    # Backfill any rows still missing a slug.
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id FROM chat_spaces WHERE public_slug IS NULL")
        ).fetchall()
        for row in rows:
            for _ in range(8):
                slug = secrets.token_urlsafe(9)
                try:
                    conn.execute(
                        text("UPDATE chat_spaces SET public_slug = :s WHERE id = :i"),
                        {"s": slug, "i": row[0]},
                    )
                    break
                except Exception:
                    # Collision on the unique index — retry with a fresh slug.
                    continue


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
    # Per-creator-per-campaign URL the creator uses to submit posted
    # content URLs back to the brand. Captured at review_submitted time
    # so the approval email can include it without re-hitting the
    # ReelStats API. Sourced from
    # `creators[].submissionLinks.submitPostsUrl` in the API payload.
    submit_posts_url = Column(Text, nullable=True)

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


# ---------------------------------------------------------------------------
# Creator <-> Brand chat spaces
#
# Triggered when a brand clicks "Request Changes" on a review notification.
# One chat space per (creator, campaign, brand) — reused across review
# resubmissions and archived when the campaign ends.
# ---------------------------------------------------------------------------


class ChatSpace(Base):
    __tablename__ = "chat_spaces"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Unguessable URL slug — used in user-facing chat URLs like
    # /chat/<public_slug> so addresses aren't sequential. The integer id
    # is still used internally and on admin routes.
    public_slug = Column(String(32), nullable=True, unique=True, index=True)

    # Composite key for reuse: see services.chat_service.compute_reuse_key.
    # SHA-256 hex of "{creator_key}|{campaign_key}|{brand_key}".
    reuse_key = Column(String(64), nullable=False, index=True)

    creator_username = Column(String(255), nullable=False)
    creator_email = Column(String(320), nullable=True)
    campaign_slug = Column(String(255), nullable=True)
    campaign_name = Column(String(255), nullable=True)
    brand_name = Column(String(255), nullable=True)

    # Slack workspace + brand-install identifiers (best-effort; nullable so a
    # chat can still exist when the brand hasn't installed the bot).
    workspace_team_id = Column(String(255), nullable=True)
    brand_install_id = Column(Integer, ForeignKey("slack_installations.id"), nullable=True)

    # Latest associated review (updated each time the creator resubmits).
    latest_review_id = Column(Integer, ForeignKey("review_submissions.id"), nullable=True)

    # active | archived
    status = Column(String(20), nullable=False, default="active")

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_message_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    # Retained for backward compatibility with existing rows. Historically
    # stamped the first time we emailed the creator a magic-link invite when
    # the chat space was opened; that invite email has since been removed
    # (the creator is only emailed once the brand posts a message), so
    # nothing writes this column anymore.
    creator_invited_at = Column(DateTime, nullable=True)

    # Track the brand Slack message that hosts the "Open Chat Space" button,
    # so we can chat_update it / post follow-ups in the same channel.
    brand_slack_channel = Column(String(255), nullable=True)
    brand_slack_ts = Column(String(255), nullable=True)

    members = relationship("ChatMember", back_populates="space", cascade="all, delete-orphan")
    messages = relationship("ChatMessage", back_populates="space", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_chat_spaces_reuse_active", "reuse_key", "status"),
    )


class ChatMember(Base):
    """
    A party with access to a chat space. `party` is 'creator' | 'brand' |
    'admin'. `identifier` is creator email for creators, slack team_id for
    brand (anyone in the brand workspace channel can enter), and an admin
    token id for admins. Display name is set on first entry.
    """
    __tablename__ = "chat_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_space_id = Column(Integer, ForeignKey("chat_spaces.id"), nullable=False, index=True)
    party = Column(String(20), nullable=False)
    identifier = Column(String(320), nullable=False)
    display_name = Column(String(255), nullable=True)

    last_read_message_id = Column(Integer, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    joined_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    space = relationship("ChatSpace", back_populates="members")

    __table_args__ = (
        UniqueConstraint("chat_space_id", "party", "identifier", name="uq_chat_member"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_space_id = Column(Integer, ForeignKey("chat_spaces.id"), nullable=False, index=True)
    sender_party = Column(String(20), nullable=False)  # creator | brand | admin | system
    sender_identifier = Column(String(320), nullable=True)
    sender_display_name = Column(String(255), nullable=True)
    body = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    space = relationship("ChatSpace", back_populates="messages")
    attachments = relationship("ChatAttachment", back_populates="message", cascade="all, delete-orphan")
    reactions = relationship("ChatReaction", back_populates="message", cascade="all, delete-orphan")


class ChatAttachment(Base):
    __tablename__ = "chat_attachments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey("chat_messages.id"), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(127), nullable=False)
    size_bytes = Column(BigInteger, nullable=False, default=0)
    storage_path = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    message = relationship("ChatMessage", back_populates="attachments")


class ChatReaction(Base):
    __tablename__ = "chat_reactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey("chat_messages.id"), nullable=False, index=True)
    party = Column(String(20), nullable=False)
    identifier = Column(String(320), nullable=False)
    emoji = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    message = relationship("ChatMessage", back_populates="reactions")

    __table_args__ = (
        UniqueConstraint("message_id", "party", "identifier", "emoji", name="uq_chat_reaction"),
    )


class ChatSession(Base):
    """
    Server-side session record backing the magic-link-to-cookie flow.
    The cookie carries `session_id` and an HMAC; lookup validates against
    this row (revocable, expirable).
    """
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_space_id = Column(Integer, ForeignKey("chat_spaces.id"), nullable=False, index=True)
    party = Column(String(20), nullable=False)
    identifier = Column(String(320), nullable=False)
    display_name = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)

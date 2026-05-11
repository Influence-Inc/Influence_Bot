import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # --- ReelStats API ---
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    REELSTATS_API_URL = os.environ.get(
        "REELSTATS_API_URL", "https://campaigns.influence.technology"
    )

    # --- Slack ---
    SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
    SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
    # Fallback channel — used when a per-type channel below isn't set.
    SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")

    # --- Slack OAuth (for per-brand install links) ---
    # Create an app at https://api.slack.com/apps, enable "Distribution", then
    # copy Client ID / Client Secret here. SLACK_OAUTH_REDIRECT_URI must match
    # the redirect URL registered on the Slack app (e.g.
    # https://your-domain/slack/oauth_redirect).
    SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID")
    SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET")
    SLACK_OAUTH_REDIRECT_URI = os.environ.get("SLACK_OAUTH_REDIRECT_URI")
    # Scopes requested during install. `incoming-webhook` causes Slack to prompt
    # the installing user to pick a channel, which is the channel the bot will
    # post to for that workspace.
    SLACK_OAUTH_SCOPES = os.environ.get(
        "SLACK_OAUTH_SCOPES",
        "chat:write,channels:read,commands,incoming-webhook,users:read",
    )
    # HMAC key used to sign the `state` param in install URLs. Defaults to the
    # signing secret, but can be overridden.
    SLACK_OAUTH_STATE_SECRET = (
        os.environ.get("SLACK_OAUTH_STATE_SECRET")
        or os.environ.get("SLACK_SIGNING_SECRET")
    )

    # Per-notification-type channels. Each resolves env var → SLACK_CHANNEL_ID
    # → hardcoded channel-name default. Accepts a channel name
    # (e.g. "#content-reviews") or a channel ID (e.g. "C0XXXXXXXXX"). The bot
    # must be a member of each channel or posts fail with `not_in_channel`.
    SLACK_CHANNEL_REVIEWS = (
        os.environ.get("SLACK_CHANNEL_REVIEWS") or SLACK_CHANNEL_ID or "#content-reviews"
    )
    SLACK_CHANNEL_UPLOADS = (
        os.environ.get("SLACK_CHANNEL_UPLOADS") or SLACK_CHANNEL_ID or "#content-uploads"
    )
    SLACK_CHANNEL_PAYMENTS = (
        os.environ.get("SLACK_CHANNEL_PAYMENTS") or SLACK_CHANNEL_ID or "#payment-reminders"
    )
    SLACK_CHANNEL_MILESTONES = (
        os.environ.get("SLACK_CHANNEL_MILESTONES") or SLACK_CHANNEL_ID or "#breakout-content-alerts"
    )
    SLACK_CHANNEL_DEADLINES = (
        os.environ.get("SLACK_CHANNEL_DEADLINES") or SLACK_CHANNEL_ID or "#creator-deadlines"
    )

    # --- Email (jennifer@useinfluence.xyz) ---
    # Preferred: Resend HTTP API. Set RESEND_API_KEY and the bot will use it
    # instead of SMTP. Resend works over HTTPS so Railway's outbound port
    # filtering doesn't block it (unlike SMTP/587).
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
    RESEND_FROM = os.environ.get(
        "RESEND_FROM",
        "Jennifer - INFLUENCE <jennifer@useinfluence.xyz>",
    )

    # Legacy SMTP fallback. Used only when RESEND_API_KEY is not set.
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "jennifer@useinfluence.xyz")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "Jennifer - INFLUENCE")

    # --- Application ---
    # Host/port binding is handled by gunicorn ($PORT on Railway), not here.
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///influence_bot.db")

    # Poll interval for the safety-net fallback. Real-time notifications come
    # from ReelStats webhooks; this loop catches anything a dropped webhook
    # missed. Prefer POLL_INTERVAL_SECONDS; POLL_INTERVAL_MINUTES is legacy.
    _poll_seconds = os.environ.get("POLL_INTERVAL_SECONDS")
    if _poll_seconds is not None:
        POLL_INTERVAL_SECONDS = int(_poll_seconds)
    elif os.environ.get("POLL_INTERVAL_MINUTES") is not None:
        POLL_INTERVAL_SECONDS = int(os.environ["POLL_INTERVAL_MINUTES"]) * 60
    else:
        POLL_INTERVAL_SECONDS = 60

    # --- Testing ---
    # If set, the bot only processes the campaign with this exact name.
    # Leave empty/unset in production to process all campaigns.
    TEST_CAMPAIGN_NAME = os.environ.get("TEST_CAMPAIGN_NAME") or None

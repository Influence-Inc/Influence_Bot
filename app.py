"""
INFLUENCE Bot — Main Application Entry Point

An automated Slack bot for INFLUENCE (influencer marketing) that:
- Polls the ReelStats API every 5 minutes for campaign data
- Sends view milestone alerts (250K, 500K, 1M, ...)
- Flags creators for payment when deliverables are complete
- Sends deadline reminders (3 days, 1 day, overdue) via Slack + email
- Sends upload follow-ups when creators are behind schedule
- Posts a daily payment summary at 9 AM
- Receives webhook events from ReelStats (review_submitted, video_links_submitted)

Email: jennifer@useinfluence.xyz
ReelStats API: configured via REELSTATS_API_URL env var
"""

import atexit
import logging

from flask import Flask, request, jsonify, render_template_string
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_sdk import WebClient

from config import Config
from models.models import init_db
from bot.handlers import register_event_handlers
from bot.commands import register_commands
from bot.actions import register_actions
from services.email_service import EmailService
from services.reelstats_api import ReelStatsAPI
from services.webhook_handler import WebhookHandler
from services.scheduler_service import SchedulerService
from services.slack_authorize import authorize as slack_authorize
from services.slack_oauth import (
    InstallConfigError,
    InstallStateError,
    SlackInstallURLGenerator,
    handle_oauth_callback,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Initialize Database
# ---------------------------------------------------------------------------
init_db()

# ---------------------------------------------------------------------------
# Slack Bolt App
# ---------------------------------------------------------------------------
# Per-workspace token lookup (slack_installations table) with SLACK_BOT_TOKEN
# fallback for the home workspace. Passing `authorize=` instead of `token=`
# avoids slack-bolt's auto-OAuth path (triggered by SLACK_CLIENT_ID/SECRET in
# the env) silently dropping a static token.
bolt_app = App(
    signing_secret=Config.SLACK_SIGNING_SECRET,
    authorize=slack_authorize,
)

# Static WebClient for the home workspace, used by the scheduler and webhook
# handler to post into the internal SLACK_CHANNEL_* channels. Bolt's
# `bolt_app.client` carries no default token when `authorize=` is used.
slack_client = WebClient(token=Config.SLACK_BOT_TOKEN)

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
email_service = EmailService()
reelstats_api = ReelStatsAPI()
scheduler_service = SchedulerService(slack_client, email_service, reelstats_api)
webhook_handler = WebhookHandler(slack_client, scheduler_service)

# ---------------------------------------------------------------------------
# Register Slack Handlers
# ---------------------------------------------------------------------------
register_event_handlers(bolt_app)
register_commands(bolt_app, scheduler_service, reelstats_api)
register_actions(bolt_app)

# ---------------------------------------------------------------------------
# Flask App  (wraps Bolt for HTTP endpoints)
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)
handler = SlackRequestHandler(bolt_app)


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    """Handle all Slack events (messages, mentions, etc.)."""
    return handler.handle(request)


@flask_app.route("/slack/commands", methods=["POST"])
def slack_commands():
    """Handle slash commands."""
    return handler.handle(request)


@flask_app.route("/slack/actions", methods=["POST"])
def slack_actions():
    """Handle interactive actions (button clicks, modal submissions)."""
    return handler.handle(request)


# ---------------------------------------------------------------------------
# Slack OAuth — per-brand install links
# ---------------------------------------------------------------------------
@flask_app.route("/slack/install", methods=["GET"])
@flask_app.route("/slack/install/<brand>", methods=["GET"])
def slack_install(brand: str = None):
    """
    Generate an install URL and redirect the brand to Slack's OAuth consent
    screen. The optional `<brand>` path segment is embedded (signed) in the
    `state` param so we know which brand the installation belongs to when
    Slack calls us back.
    """
    try:
        generator = SlackInstallURLGenerator()
    except InstallConfigError as exc:
        logger.error("Slack OAuth not configured: %s", exc)
        return jsonify({"error": str(exc)}), 500

    url = generator.build_install_url(brand=brand)
    # 302 so a browser following the link lands on Slack's consent page.
    return "", 302, {"Location": url}


_INSTALL_RESULT_PAGE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>INFLUENCE Bot — {{ heading }}</title>
  <style>
    html, body {
      background: #ffffff;
      color: #1d1d1f;
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 520px;
      margin: 12vh auto;
      padding: 0 24px;
      line-height: 1.55;
    }
    .badge { font-size: 56px; line-height: 1; margin-bottom: 12px; }
    h1 { font-size: 24px; margin: 0 0 8px; color: #1d1d1f; }
    p  { margin: 8px 0; color: #1d1d1f; }
    .card {
      margin-top: 24px;
      padding: 16px 20px;
      border: 1px solid #e5e5ea;
      border-radius: 12px;
      background: #f9f9fb;
      color: #1d1d1f;
    }
    .row { display: flex; justify-content: space-between; gap: 16px; padding: 6px 0; }
    .row + .row { border-top: 1px solid rgba(0,0,0,0.06); }
    .label { color: #6b7280; }
    .value { color: #1d1d1f; font-weight: 500; }
    code {
      background: #ececf1;
      color: #1d1d1f;
      padding: 1px 6px;
      border-radius: 4px;
      font-size: 90%;
    }
    .muted { color: #6b7280; font-size: 14px; margin-top: 24px; }
  </style>
</head>
<body>
  <div class="badge">{{ badge }}</div>
  <h1>{{ heading }}</h1>
  <p>{{ message }}</p>
  {% if details %}
  <div class="card">
    {% for label, value in details %}
    <div class="row"><span class="label">{{ label }}</span><span class="value">{{ value }}</span></div>
    {% endfor %}
  </div>
  {% endif %}
  <p class="muted">You can close this tab.</p>
</body>
</html>
"""


def _render_install_page(*, badge, heading, message, details=None, status_code=200):
    html = render_template_string(
        _INSTALL_RESULT_PAGE,
        badge=badge,
        heading=heading,
        message=message,
        details=details or [],
    )
    return html, status_code, {"Content-Type": "text/html; charset=utf-8"}


@flask_app.route("/slack/oauth_redirect", methods=["GET"])
def slack_oauth_redirect():
    """OAuth callback: Slack redirects here with ?code=...&state=..."""
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")
    if error:
        return _render_install_page(
            badge="✕",
            heading="Install cancelled",
            message=f"Slack reported: {error}. No changes were made to your workspace.",
            status_code=400,
        )
    if not code or not state:
        return _render_install_page(
            badge="✕",
            heading="Something's missing",
            message="The install link is incomplete. Please open the link your INFLUENCE contact sent you again.",
            status_code=400,
        )

    try:
        install = handle_oauth_callback(code=code, state=state)
    except InstallStateError as exc:
        logger.warning("Invalid OAuth state: %s", exc)
        return _render_install_page(
            badge="✕",
            heading="Link expired",
            message="This install link is no longer valid. Please ask your INFLUENCE contact for a fresh link.",
            status_code=400,
        )
    except Exception as exc:
        logger.exception("OAuth callback failed: %s", exc)
        return _render_install_page(
            badge="✕",
            heading="Install failed",
            message="Something went wrong on our end. Please try again, or contact INFLUENCE support.",
            status_code=500,
        )

    # Slack sometimes returns the channel name with a leading "#", sometimes
    # without — normalize so we never render "##social".
    raw_channel = (install.channel_name or "").lstrip("#")
    channel_display = f"#{raw_channel}" if raw_channel else "(channel not set)"

    details = [("Workspace", install.team_name or install.team_id)]
    if raw_channel:
        details.append(("Channel", channel_display))
    if install.brand:
        details.append(("Brand", install.brand))

    return _render_install_page(
        badge="✓",
        heading="You're all set!",
        message=f"INFLUENCE Bot is now installed and will post to {channel_display}.",
        details=details,
    )


# ---------------------------------------------------------------------------
# ReelStats Webhook Endpoint
# ---------------------------------------------------------------------------
@flask_app.route("/webhook", methods=["POST"])
def reelstats_webhook():
    """
    Receive webhook events from the ReelStats server.
    Events: review_submitted, video_links_submitted
    """
    payload = request.get_json(silent=True)
    if not payload:
        logger.warning("Received webhook with no JSON payload")
        return jsonify({"error": "No payload"}), 400

    event_type = payload.get("event", "unknown")
    creator = (payload.get("creator") or {}).get("username", "?")
    campaign = (payload.get("campaign") or {}).get("name", "?")
    logger.info(
        f"Received webhook event: {event_type} "
        f"(creator=@{creator}, campaign='{campaign}')"
    )

    try:
        success = webhook_handler.handle_event(payload)
    except Exception as e:
        logger.exception(f"Unhandled error processing webhook {event_type}: {e}")
        return jsonify({"status": "error", "event": event_type}), 500

    if success:
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "failed", "event": event_type}), 500


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------
@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "bot": "INFLUENCE Bot"}), 200


# ---------------------------------------------------------------------------
# SMTP egress diagnostic — temporary. Tests outbound TCP to the configured
# (or query-string-overridden) SMTP host:port from inside the Railway
# container so we can tell whether email timeouts are network-level
# (Railway blocking SMTP) or auth/config-level.
#
# Usage:
#   GET /debug/smtp                    -> uses Config.SMTP_HOST / SMTP_PORT
#   GET /debug/smtp?port=465           -> override port
#   GET /debug/smtp?host=smtp.x&port=2525
#
# Remove once diagnosis is complete.
# ---------------------------------------------------------------------------
@flask_app.route("/debug/smtp", methods=["GET"])
def debug_smtp():
    import socket
    import time

    host = request.args.get("host") or Config.SMTP_HOST
    try:
        port = int(request.args.get("port") or Config.SMTP_PORT)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid port"}), 400

    result = {"host": host, "port": port}

    # 1. DNS lookup (IPv4 only — matches what _IPv4SMTP does in production).
    t0 = time.monotonic()
    try:
        addrinfo = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        result["dns_ms"] = int((time.monotonic() - t0) * 1000)
        result["resolved_ips"] = sorted({a[4][0] for a in addrinfo})
    except Exception as e:
        result["ok"] = False
        result["stage"] = "dns"
        result["error"] = f"{type(e).__name__}: {e}"
        return jsonify(result), 200

    # 2. TCP connect.
    t0 = time.monotonic()
    try:
        s = socket.create_connection(addrinfo[0][4], timeout=10)
        result["connect_ms"] = int((time.monotonic() - t0) * 1000)
        result["peer"] = f"{s.getpeername()[0]}:{s.getpeername()[1]}"
        s.close()
        result["ok"] = True
        result["stage"] = "connect"
    except Exception as e:
        result["ok"] = False
        result["stage"] = "connect"
        result["connect_ms"] = int((time.monotonic() - t0) * 1000)
        result["error"] = f"{type(e).__name__}: {e}"

    return jsonify(result), 200


# ---------------------------------------------------------------------------
# Startup — runs at import time under gunicorn (`app:flask_app`).
# Gunicorn must be started with --workers 1 so the in-process scheduler
# runs exactly once; multiple workers would fire every scheduled job N
# times and race on the SQLite dedup tables.
# ---------------------------------------------------------------------------
logger.info("Starting INFLUENCE Bot...")
logger.info(f"ReelStats API: {Config.REELSTATS_API_URL}")
logger.info(f"Poll interval: {Config.POLL_INTERVAL_SECONDS}s (webhook fallback)")

scheduler_service.start()
atexit.register(scheduler_service.shutdown)

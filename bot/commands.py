"""
Slack slash command handlers for INFLUENCE Bot.

Commands:
  /influence-status   — View active campaign statuses from the ReelStats API
  /influence-check    — Manually trigger all notification checks
  /influence-install  — Generate a per-brand Slack install link (admin only)
  /influence-help     — Show all available commands

Workspace scoping
-----------------
The admin home workspace (no SlackInstallation row) sees all campaigns and
can manually trigger checks. Brand workspaces (those that installed via
/slack/install) see only campaigns matching their own brand and cannot
trigger global checks.
"""

import logging
import re
from urllib.parse import urlparse

from models.models import (
    DeadlineReminder,
    DeliverableAlert,
    MilestoneAlert,
    SessionLocal,
    SlackInstallation,
    UploadFollowup,
)
from services.brand_routing import (
    brand_matches_install,
    find_install_by_team,
    find_install_for_brand_name,
)
from services.slack_oauth import InstallConfigError, SlackInstallURLGenerator

logger = logging.getLogger(__name__)


def _alert_counts() -> dict[str, int]:
    """Snapshot row counts for each dedup table (used by /influence-check)."""
    db = SessionLocal()
    try:
        return {
            "milestones": db.query(MilestoneAlert).count(),
            "deliverables": db.query(DeliverableAlert).count(),
            "deadlines": db.query(DeadlineReminder).count(),
            "uploads": db.query(UploadFollowup).count(),
        }
    finally:
        db.close()


def _public_base_url(redirect_uri):
    """Strip the path off SLACK_OAUTH_REDIRECT_URI to get the bot's public origin."""
    if not redirect_uri:
        return None
    parsed = urlparse(redirect_uri)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def register_commands(app, scheduler_service, reelstats_api):
    """Register all slash commands on the Bolt app."""

    @app.command("/influence-status")
    def handle_status(ack, respond, command):
        """Show campaign status. Brand workspaces see only their own brand."""
        ack()
        respond(
            text=":hourglass_flowing_sand: Fetching campaigns from ReelStats...",
            response_type="ephemeral",
        )

        campaigns = reelstats_api.get_campaigns()

        install = find_install_by_team(command.get("team_id"))

        if install is not None:
            before = len(campaigns)
            campaigns = [
                c for c in campaigns
                if brand_matches_install(c.get("brandName"), install)
            ]
            logger.info(
                "/influence-status scoped to brand=%s team_id=%s: %d -> %d campaigns",
                install.brand or install.team_name, command.get("team_id"),
                before, len(campaigns),
            )

        if not campaigns:
            if install is not None:
                msg = (
                    f":information_source: No active campaigns found for "
                    f"*{install.team_name or install.brand}*."
                )
            else:
                msg = ":information_source: No active campaigns found."
            respond(text=msg, response_type="ephemeral")
            return

        header = ":bar_chart: *Active Campaigns*"
        if install is not None:
            header = f":bar_chart: *Active Campaigns — {install.team_name or install.brand}*"

        lines = [header + "\n"]
        for campaign in campaigns:
            name = campaign.get("name", "Unknown")
            brand = campaign.get("brandName", "")
            creator_count = len(campaign.get("creators", []))
            lines.append(
                f"• *{name}* ({brand}) — {creator_count} creator(s)"
            )

        respond(text="\n".join(lines), response_type="ephemeral")

    @app.command("/influence-check")
    def handle_check(ack, respond, command):
        """
        Manually trigger all notification checks. Restricted to the admin
        workspace because the underlying job iterates every brand's creators
        and posts to admin channels.
        """
        ack()
        install = find_install_by_team(command.get("team_id"))
        if install is not None:
            respond(
                text=(
                    ":lock: This command is reserved for the INFLUENCE admin "
                    "workspace. You'll automatically receive notifications "
                    "for your brand's campaigns here."
                ),
                response_type="ephemeral",
            )
            return

        respond(
            text=":mag: Running all checks now (milestones, deliverables, deadlines, uploads)...",
            response_type="ephemeral",
        )

        before = _alert_counts()
        scheduler_service.run_all_checks()
        after = _alert_counts()

        deltas = {k: after[k] - before[k] for k in before}
        total_new = sum(deltas.values())

        if total_new == 0:
            respond(
                text=(
                    ":white_check_mark: All checks complete — nothing new to "
                    "notify since the last run."
                ),
                response_type="ephemeral",
            )
            return

        labels = {
            "milestones": "milestone",
            "deliverables": "deliverables-complete",
            "deadlines": "deadline reminder",
            "uploads": "upload follow-up",
        }
        breakdown = ", ".join(
            f"{deltas[k]} {labels[k]}" for k in deltas if deltas[k] > 0
        )
        respond(
            text=(
                f":white_check_mark: All checks complete. Sent *{total_new}* "
                f"new notification(s): {breakdown}."
            ),
            response_type="ephemeral",
        )

    _BRAND_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

    @app.command("/influence-install")
    def handle_install(ack, respond, command):
        """
        Generate a Slack install link for a brand. Admin-only: brand
        workspaces shouldn't be able to mint install URLs for other brands.

        Usage: /influence-install <brand-slug>
        The slug is embedded (signed) in the OAuth `state` so the callback
        attributes the install to the right brand.
        """
        ack()

        install = find_install_by_team(command.get("team_id"))
        if install is not None:
            respond(
                text=(
                    ":lock: This command is reserved for the INFLUENCE admin "
                    "workspace."
                ),
                response_type="ephemeral",
            )
            return

        tokens = (command.get("text") or "").strip().split()
        brand = tokens[0].lower() if tokens else ""
        if not brand:
            respond(
                text=(
                    ":information_source: *Usage:* `/influence-install <brand-slug>`\n"
                    "Example: `/influence-install acme` — the slug is embedded in "
                    "the install link so we can attribute the workspace to the brand."
                ),
                response_type="ephemeral",
            )
            return

        if not _BRAND_SLUG_RE.match(brand):
            respond(
                text=(
                    f":warning: `{brand}` isn't a valid brand slug. Use lowercase "
                    "letters, digits, and hyphens (max 63 chars), starting with a "
                    "letter or digit."
                ),
                response_type="ephemeral",
            )
            return

        # Prefer the bot-routed `/slack/install/<brand>` URL (derived from
        # SLACK_OAUTH_REDIRECT_URI) so the link mints a fresh signed state at
        # each click and never expires. Fall back to the direct Slack URL
        # (10-minute state lifetime) if the redirect URI isn't configured.
        try:
            generator = SlackInstallURLGenerator()
        except InstallConfigError as exc:
            logger.error("Slack OAuth not configured: %s", exc)
            respond(
                text=(
                    f":x: Slack OAuth isn't configured on the bot: {exc}. "
                    "Set `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, "
                    "`SLACK_OAUTH_REDIRECT_URI`, and `SLACK_OAUTH_STATE_SECRET`."
                ),
                response_type="ephemeral",
            )
            return

        public_base = _public_base_url(generator.redirect_uri)
        if public_base:
            url = f"{public_base}/slack/install/{brand}"
            expiry_note = ""
        else:
            url = generator.build_install_url(brand=brand)
            expiry_note = (
                "\n:hourglass: This link expires in 10 minutes — re-run the "
                "command for a fresh one if needed."
            )

        respond(
            text=(
                f":link: *Install link for `{brand}`*\n"
                f"<{url}|Open Slack consent screen> "
                "(or copy the URL below to send to the brand)\n"
                f"```{url}```"
                f"{expiry_note}"
            ),
            response_type="ephemeral",
        )

    @app.command("/influence-installs")
    def handle_installs(ack, respond, command):
        """
        Admin-only. List every brand workspace that installed the bot and
        whether it can actually receive posts (has a saved channel + token).
        Optionally pass a brand name to test which install a campaign with
        that brandName would route to — e.g. `/influence-installs Reve`.
        """
        ack()

        # Admin workspace only (a brand workspace has its own install row).
        if find_install_by_team(command.get("team_id")) is not None:
            respond(
                text=":lock: This command is reserved for the INFLUENCE admin workspace.",
                response_type="ephemeral",
            )
            return

        db = SessionLocal()
        try:
            rows = (
                db.query(SlackInstallation)
                .order_by(SlackInstallation.installed_at.desc())
                .all()
            )
            summaries = []
            for r in rows:
                usable = bool(r.bot_token and r.channel_id)
                flag = ":white_check_mark:" if usable else ":warning:"
                if r.channel_name:
                    channel = f"#{r.channel_name}"
                elif r.channel_id:
                    channel = r.channel_id
                else:
                    channel = "*no channel saved*"
                note = "" if usable else "  _(can't receive posts — re-install and pick a channel)_"
                summaries.append(
                    f"{flag} slug *`{r.brand or '—'}`* · workspace *{r.team_name or r.team_id}* "
                    f"→ {channel}{note}"
                )
        finally:
            db.close()

        if not summaries:
            respond(
                text=(
                    ":information_source: No brand workspaces have installed the bot "
                    "yet. Generate a link with `/influence-install <brand>`."
                ),
                response_type="ephemeral",
            )
            return

        lines = [f":package: *Brand installs ({len(summaries)})*", *summaries]

        # Optional routing test for a specific brandName.
        test_brand = (command.get("text") or "").strip()
        if test_brand:
            match = find_install_for_brand_name(test_brand)
            if match is None:
                lines.append(
                    f"\n:mag: A campaign with brandName *{test_brand}* matches "
                    f"*no install* → brand notification would be skipped."
                )
            elif not (match.bot_token and match.channel_id):
                lines.append(
                    f"\n:mag: A campaign with brandName *{test_brand}* matches "
                    f"*{match.team_name or match.team_id}*, but it has *no channel* "
                    f"→ notification would be skipped (re-install needed)."
                )
            else:
                lines.append(
                    f"\n:mag: A campaign with brandName *{test_brand}* → posts to "
                    f"*{match.team_name or match.team_id}* (#{match.channel_name})."
                )

        respond(text="\n".join(lines), response_type="ephemeral")

    @app.command("/influence-help")
    def handle_help(ack, respond, command):
        """Show available commands — trimmed for brand workspaces."""
        ack()

        # Brand workspaces see only what applies to them: no admin-only commands
        # and no internal (payment / deadline / upload) features.
        if find_install_by_team(command.get("team_id")) is not None:
            respond(
                text=(
                    ":robot_face: *INFLUENCE Bot*\n\n"
                    "`/influence-status` — View your active campaigns\n"
                    "`/influence-help` — Show this help message\n\n"
                    "*You'll automatically receive:*\n"
                    "- :trophy: Breakout view milestone alerts (250K, 500K, 1M, ...)\n"
                    "- :film_frames: Draft videos submitted for your review\n"
                    "- :link: Posted content links from your creators"
                ),
                response_type="ephemeral",
            )
            return

        respond(
            text=(
                ":robot_face: *INFLUENCE Bot Commands*\n\n"
                "`/influence-status` — View active campaigns from the ReelStats API\n"
                "`/influence-check` — Manually run all notification checks (admin only)\n"
                "`/influence-install <brand>` — Generate a Slack install link for a brand (admin only)\n"
                "`/influence-installs [brand]` — List brand installs / test routing (admin only)\n"
                "`/influence-help` — Show this help message\n\n"
                "*Automatic Features:*\n"
                "- :trophy: View milestone alerts (250K, 500K, 1M, ...)\n"
                "- :white_check_mark: Payment flags when deliverables are complete\n"
                "- :calendar: Deadline reminders (3 days, 1 day, overdue) via Slack + email\n"
                "- :film_frames: Upload follow-ups when creators are behind schedule\n"
                "- :sunrise: Daily payment summary at 9 AM\n"
                "- :link: Real-time webhook notifications for reviews and video links"
            ),
            response_type="ephemeral",
        )

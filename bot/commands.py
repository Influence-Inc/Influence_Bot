"""
Slack slash command handlers for INFLUENCE Bot.

Commands:
  /influence-status  — View active campaign statuses from the ReelStats API
  /influence-check   — Manually trigger all notification checks
  /influence-help    — Show all available commands

Workspace scoping
-----------------
The admin home workspace (no SlackInstallation row) sees all campaigns and
can manually trigger checks. Brand workspaces (those that installed via
/slack/install) see only campaigns matching their own brand and cannot
trigger global checks.
"""

import logging
import re

from services.brand_routing import find_install_by_team

logger = logging.getLogger(__name__)


def _normalize(value):
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


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
        brand_filter = None
        if install is not None:
            brand_filter = _normalize(install.brand) or _normalize(install.team_name)

        if brand_filter:
            before = len(campaigns)
            campaigns = [
                c for c in campaigns
                if _normalize(c.get("brandName")) == brand_filter
            ]
            logger.info(
                "/influence-status scoped to brand=%s team_id=%s: %d -> %d campaigns",
                brand_filter, command.get("team_id"), before, len(campaigns),
            )

        if not campaigns:
            if brand_filter:
                msg = (
                    f":information_source: No active campaigns found for "
                    f"*{install.team_name or install.brand}*."
                )
            else:
                msg = ":information_source: No active campaigns found."
            respond(text=msg, response_type="ephemeral")
            return

        header = ":bar_chart: *Active Campaigns*"
        if brand_filter:
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
        scheduler_service.run_all_checks()
        respond(
            text=":white_check_mark: All checks complete. Notifications sent for any new items.",
            response_type="ephemeral",
        )

    @app.command("/influence-help")
    def handle_help(ack, respond):
        """Show all available bot commands."""
        ack()
        respond(
            text=(
                ":robot_face: *INFLUENCE Bot Commands*\n\n"
                "`/influence-status` — View active campaigns from the ReelStats API\n"
                "`/influence-check` — Manually run all notification checks (admin only)\n"
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

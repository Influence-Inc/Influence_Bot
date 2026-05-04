"""
Bolt `authorize` callable for INFLUENCE Bot.

Resolves the bot token for an incoming Slack request:
1. Per-workspace OAuth installs — looked up by team_id in the
   `slack_installations` table populated by services/slack_oauth.py.
2. Home workspace — falls back to the SLACK_BOT_TOKEN env var so the
   workspace where the app was originally installed (and where scheduler
   messages post) keeps working without going through the install flow.
"""

from __future__ import annotations

import logging
from typing import Optional

from slack_bolt.authorization import AuthorizeResult

from config import Config
from models.models import SessionLocal, SlackInstallation

logger = logging.getLogger(__name__)


def authorize(
    enterprise_id: Optional[str],
    team_id: Optional[str],
    user_id: Optional[str],
    **_: object,
) -> Optional[AuthorizeResult]:
    if team_id:
        db = SessionLocal()
        try:
            install = (
                db.query(SlackInstallation)
                .filter_by(team_id=team_id)
                .order_by(SlackInstallation.installed_at.desc())
                .first()
            )
            if install and install.bot_token:
                return AuthorizeResult(
                    enterprise_id=enterprise_id,
                    team_id=install.team_id,
                    bot_token=install.bot_token,
                    bot_user_id=install.bot_user_id,
                    bot_id=install.bot_user_id,
                )
        finally:
            db.close()

    if Config.SLACK_BOT_TOKEN:
        return AuthorizeResult(
            enterprise_id=enterprise_id,
            team_id=team_id,
            bot_token=Config.SLACK_BOT_TOKEN,
        )

    logger.error(
        "No Slack token available for team_id=%s — install the app via "
        "/slack/install or set SLACK_BOT_TOKEN.",
        team_id,
    )
    return None

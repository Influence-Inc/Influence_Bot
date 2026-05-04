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
    logger.info(
        "authorize called: enterprise_id=%s team_id=%s user_id=%s",
        enterprise_id, team_id, user_id,
    )

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
                logger.info(
                    "authorize: using SlackInstallation row id=%s for team_id=%s",
                    install.id, team_id,
                )
                return AuthorizeResult(
                    enterprise_id=enterprise_id,
                    team_id=install.team_id,
                    bot_token=install.bot_token,
                    bot_user_id=install.bot_user_id,
                    bot_id=install.bot_user_id,
                )
            logger.info("authorize: no SlackInstallation row for team_id=%s", team_id)
        except Exception:
            logger.exception("authorize: SlackInstallation lookup failed")
        finally:
            db.close()

    token = Config.SLACK_BOT_TOKEN
    logger.info(
        "authorize: SLACK_BOT_TOKEN fallback — present=%s prefix=%s len=%s",
        bool(token),
        (token[:5] if token else "NONE"),
        (len(token) if token else 0),
    )
    if token:
        return AuthorizeResult(
            enterprise_id=enterprise_id,
            team_id=team_id,
            bot_token=token,
        )

    logger.error(
        "No Slack token available for team_id=%s — install the app via "
        "/slack/install or set SLACK_BOT_TOKEN.",
        team_id,
    )
    return None

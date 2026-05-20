"""
Helpers that map between Slack workspaces and brands.

Used by the scheduler to fan out notifications to a brand's own workspace
(in addition to the admin home workspace), and by slash commands to scope
results to the workspace that invoked them.

Matching strategy
-----------------
The install slug (`SlackInstallation.brand`) is set from the path segment in
the install URL (e.g. ``/slack/install/influuu``). The ReelStats API returns
brand names like ``"Influuu"`` or ``"Influuu, Inc"``. We compare them after
lowercasing and stripping non-alphanumeric characters, against either the
install slug or the workspace name Slack returned during OAuth — whichever
hits first.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from models.models import SessionLocal, SlackInstallation

logger = logging.getLogger(__name__)


def _normalize(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def find_install_by_team(team_id: Optional[str]) -> Optional[SlackInstallation]:
    """Return the most recent SlackInstallation for a Slack team_id."""
    if not team_id:
        return None
    db = SessionLocal()
    try:
        return (
            db.query(SlackInstallation)
            .filter_by(team_id=team_id)
            .order_by(SlackInstallation.installed_at.desc())
            .first()
        )
    finally:
        db.close()


def find_install_for_brand_name(brand_name: Optional[str]) -> Optional[SlackInstallation]:
    """
    Return the SlackInstallation whose `brand` slug or `team_name` matches
    `brand_name`, comparing case-insensitively after stripping non-alnum
    characters. Returns None when no install matches.
    """
    target = _normalize(brand_name)
    if not target:
        return None
    db = SessionLocal()
    try:
        for install in db.query(SlackInstallation).all():
            if _normalize(install.brand) == target or _normalize(install.team_name) == target:
                return install
        return None
    finally:
        db.close()


def install_brand_label(install: Optional[SlackInstallation]) -> str:
    """Best-effort human label for an install; used in logs."""
    if install is None:
        return "(none)"
    return install.brand or install.team_name or install.team_id or "(unknown)"


def post_to_brand_workspace(
    brand_name: Optional[str],
    text: str,
    blocks: list[dict],
) -> tuple[Optional[str], Optional[str]]:
    """
    Mirror an admin-channel notification to the brand's own workspace if
    they've installed the bot via /slack/install. No-op for brands that
    haven't installed (or whose install row has no channel/token yet).
    Failures are logged and swallowed so a broken brand install never
    blocks admin notifications.

    Returns `(channel_id, ts)` for the posted message so callers can thread
    follow-ups under it. Returns `(None, None)` on no-op or failure.
    """
    install = find_install_for_brand_name(brand_name)
    if install is None or not install.bot_token or not install.channel_id:
        return None, None
    try:
        response = WebClient(token=install.bot_token).chat_postMessage(
            channel=install.channel_id,
            text=text,
            blocks=blocks,
        )
        return response.get("channel") or install.channel_id, response.get("ts")
    except SlackApiError as e:
        err = e.response.get("error") if e.response else str(e)
        logger.warning(
            "Brand-workspace post failed: brand=%s team_id=%s channel=%s error=%s",
            install_brand_label(install), install.team_id, install.channel_id, err,
        )
    except Exception as e:
        logger.warning(
            "Brand-workspace post failed: brand=%s error=%s",
            install_brand_label(install), e,
        )
    return None, None

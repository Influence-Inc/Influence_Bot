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

import re
from typing import Optional

from models.models import SessionLocal, SlackInstallation


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

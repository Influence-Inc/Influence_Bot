"""
Helpers that map between Slack workspaces and brands.

Used by the scheduler to fan out notifications to a brand's own workspace
(in addition to the admin home workspace), and by slash commands to scope
results to the workspace that invoked them.

Matching strategy
-----------------
The install slug (`SlackInstallation.brand`) is set from the path segment in
the install URL (e.g. ``/slack/install/influuu``). The ReelStats API returns
brand names like ``"Influuu"`` or ``"Influuu, Inc"``. We compare them against
either the install slug or the workspace name Slack returned during OAuth.

Matching is intentionally forgiving so a workspace whose name carries extra
words still receives its brand's notifications:

1. **Exact match** — after lowercasing and stripping non-alphanumeric
   characters, the brand name equals the install slug or the workspace name.
2. **Token-prefix match** — the brand name forms the leading whole word(s) of
   the slug or workspace name (or vice-versa). This is what makes a campaign
   for brand ``"Reve"`` reach a workspace named ``"REVE AI"``. Matching is
   word-boundary aware, so ``"Reve"`` never matches ``"Revel"`` or
   ``"Revenue"``.

Exact matches always win over prefix matches, so a workspace named exactly for
its brand is never shadowed by a looser prefix hit on another install.
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


def _tokens(value: Optional[str]) -> list[str]:
    """Split a name into lowercase alphanumeric words (e.g. "REVE AI" -> ["reve", "ai"])."""
    if not value:
        return []
    return re.findall(r"[a-z0-9]+", value.lower())


def _token_prefix_match(a: list[str], b: list[str]) -> bool:
    """
    True when the shorter token list is a whole-token leading prefix of the
    longer one. Word-boundary aware, so ["reve"] matches ["reve", "ai"]
    ("REVE AI") but never ["revel"] or ["revenue"]. Empty inputs never match.
    """
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[: len(shorter)] == shorter


def brand_matches_install(
    brand_name: Optional[str], install: Optional[SlackInstallation]
) -> bool:
    """
    True when a campaign's `brand_name` should route to this Slack install.

    Exact normalized match on the install slug or workspace name wins; a
    forgiving token-prefix match ("Reve" -> "REVE AI" workspace) is the
    fallback. See the module docstring for the full strategy.
    """
    if install is None:
        return False
    target = _normalize(brand_name)
    if not target:
        return False
    if _normalize(install.brand) == target or _normalize(install.team_name) == target:
        return True
    brand_toks = _tokens(brand_name)
    return (
        _token_prefix_match(brand_toks, _tokens(install.brand))
        or _token_prefix_match(brand_toks, _tokens(install.team_name))
    )


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


def _install_is_usable(install: SlackInstallation) -> bool:
    """A brand install can only receive posts if it has a bot token AND a channel."""
    return bool(install.bot_token and install.channel_id)


def find_install_for_brand_name(brand_name: Optional[str]) -> Optional[SlackInstallation]:
    """
    Return the SlackInstallation whose `brand` slug or `team_name` matches
    `brand_name` (see the module docstring for the matching strategy).

    Preference order among matches:
      1. An exact normalized match beats a token-prefix match.
      2. Within each tier, an install that can actually receive posts (has a
         bot token AND a channel_id) beats one that can't — so a broken or
         stale install never shadows a working one. Ties break on the most
         recent install.
    Returns None when no install matches at all.
    """
    target = _normalize(brand_name)
    if not target:
        return None
    db = SessionLocal()
    try:
        installs = (
            db.query(SlackInstallation)
            .order_by(SlackInstallation.installed_at.desc())
            .all()
        )
        brand_toks = _tokens(brand_name)

        # Pass 1: exact normalized match, preferring a usable install.
        exact = [
            i for i in installs
            if _normalize(i.brand) == target or _normalize(i.team_name) == target
        ]
        if exact:
            return next((i for i in exact if _install_is_usable(i)), exact[0])

        # Pass 2: forgiving token-prefix fallback (e.g. brand "Reve" reaching
        # the "REVE AI" workspace), again preferring a usable install.
        prefix = [
            i for i in installs
            if _token_prefix_match(brand_toks, _tokens(i.brand))
            or _token_prefix_match(brand_toks, _tokens(i.team_name))
        ]
        if prefix:
            return next((i for i in prefix if _install_is_usable(i)), prefix[0])
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
    if install is None:
        logger.info(
            "Brand mirror skipped: no installed workspace matches brand=%r. "
            "The brand must install via /slack/install, and its workspace name "
            "or install slug must match the campaign's brand name.",
            brand_name,
        )
        return None, None
    if not _install_is_usable(install):
        missing = "bot_token" if not install.bot_token else "channel_id"
        logger.warning(
            "Brand mirror skipped: install for %s (team_id=%s) is missing %s — "
            "the brand needs to re-install the bot (with the incoming-webhook "
            "scope so a channel is captured).",
            install_brand_label(install), install.team_id, missing,
        )
        return None, None
    try:
        response = WebClient(token=install.bot_token).chat_postMessage(
            channel=install.channel_id,
            text=text,
            blocks=blocks,
        )
        logger.info(
            "Brand mirror posted: brand=%r -> %s (team_id=%s, channel=%s)",
            brand_name, install_brand_label(install),
            install.team_id, install.channel_id,
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

"""
Slack OAuth install flow for INFLUENCE Bot.

Produces per-brand install links so each brand can add the bot to their own
Slack workspace and pick the channel it posts to. The `incoming-webhook` scope
makes Slack prompt the installing user to select a channel during OAuth, and
Slack hands back the chosen channel ID + a webhook URL, which we persist.

Usage:
    generator = SlackInstallURLGenerator()
    url = generator.build_install_url(brand="acme")
    # Send `url` to the brand; when they complete OAuth, Slack redirects to
    # SLACK_OAUTH_REDIRECT_URI with a `code` + `state`, handled by
    # handle_oauth_callback().
"""

from __future__ import annotations

import hmac
import hashlib
import json
import logging
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Optional
from urllib.parse import urlencode

import requests

from config import Config
from models.models import SessionLocal, SlackInstallation

logger = logging.getLogger(__name__)

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"

# State payloads older than this are rejected (seconds).
STATE_MAX_AGE_SECONDS = 10 * 60


class InstallConfigError(RuntimeError):
    """Raised when OAuth config is missing."""


class InstallStateError(RuntimeError):
    """Raised when the `state` param fails HMAC verification."""


class InstallTargetError(RuntimeError):
    """
    Raised when the brand picked a Direct Message (instead of a channel) as
    the bot's post target during install. We require a dedicated channel so
    the whole brand team can see notifications, so this install is rejected
    and not persisted.
    """


def _is_dm_channel(channel_id: Optional[str]) -> bool:
    """
    True when a Slack conversation id refers to a Direct Message.

    Slack conversation ids are prefixed by type: ``C`` public channel,
    ``G`` private channel / group DM, ``D`` direct message. Only a 1:1 DM
    (``D``) is unambiguously a DM — a ``G`` id can be a legitimate private
    channel, so we deliberately don't reject those.
    """
    return bool(channel_id) and channel_id.startswith("D")


def _require_config() -> None:
    missing = [
        name
        for name, val in (
            ("SLACK_CLIENT_ID", Config.SLACK_CLIENT_ID),
            ("SLACK_CLIENT_SECRET", Config.SLACK_CLIENT_SECRET),
            ("SLACK_OAUTH_REDIRECT_URI", Config.SLACK_OAUTH_REDIRECT_URI),
            ("SLACK_OAUTH_STATE_SECRET", Config.SLACK_OAUTH_STATE_SECRET),
        )
        if not val
    ]
    if missing:
        raise InstallConfigError(
            "Slack OAuth is not configured; missing env vars: "
            + ", ".join(missing)
        )


def _b64url(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return urlsafe_b64decode(s + padding)


def _sign_state(payload: dict) -> str:
    """Return a tamper-proof state token: base64(payload).base64(hmac)."""
    secret = Config.SLACK_OAUTH_STATE_SECRET.encode("utf-8")
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(mac)}"


def _verify_state(state: str) -> dict:
    try:
        body, sig = state.split(".", 1)
    except ValueError as exc:
        raise InstallStateError("Malformed state token") from exc

    secret = Config.SLACK_OAUTH_STATE_SECRET.encode("utf-8")
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url(expected), sig):
        raise InstallStateError("State signature mismatch")

    try:
        payload = json.loads(_b64url_decode(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise InstallStateError("State payload is not valid JSON") from exc

    issued_at = payload.get("iat", 0)
    if time.time() - issued_at > STATE_MAX_AGE_SECONDS:
        raise InstallStateError("State token expired")

    return payload


class SlackInstallURLGenerator:
    """Builds `https://slack.com/oauth/v2/authorize?...` URLs for brands."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        scopes: Optional[str] = None,
        redirect_uri: Optional[str] = None,
    ) -> None:
        _require_config()
        self.client_id = client_id or Config.SLACK_CLIENT_ID
        self.scopes = scopes or Config.SLACK_OAUTH_SCOPES
        self.redirect_uri = redirect_uri or Config.SLACK_OAUTH_REDIRECT_URI

    def build_install_url(self, brand: Optional[str] = None) -> str:
        state_payload = {
            "brand": brand,
            "nonce": secrets.token_urlsafe(16),
            "iat": int(time.time()),
        }
        params = {
            "client_id": self.client_id,
            "scope": self.scopes,
            "redirect_uri": self.redirect_uri,
            "state": _sign_state(state_payload),
        }
        return f"{SLACK_AUTHORIZE_URL}?{urlencode(params)}"


def _exchange_code_for_token(code: str) -> dict:
    resp = requests.post(
        SLACK_OAUTH_ACCESS_URL,
        data={
            "client_id": Config.SLACK_CLIENT_ID,
            "client_secret": Config.SLACK_CLIENT_SECRET,
            "code": code,
            "redirect_uri": Config.SLACK_OAUTH_REDIRECT_URI,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack oauth.v2.access failed: {data.get('error')}")
    return data


def handle_oauth_callback(code: str, state: str) -> SlackInstallation:
    """
    Handle the Slack redirect to SLACK_OAUTH_REDIRECT_URI. Verifies `state`,
    exchanges `code` for a bot token, persists a SlackInstallation row, and
    returns it.

    Raises InstallTargetError (and persists nothing) when the brand chose a
    Direct Message instead of a channel during install — we require a
    dedicated channel so the whole brand team sees notifications.
    """
    _require_config()
    state_payload = _verify_state(state)
    brand = state_payload.get("brand")

    data = _exchange_code_for_token(code)
    team = data.get("team") or {}
    enterprise = data.get("enterprise") or {}
    incoming = data.get("incoming_webhook") or {}
    authed_user = data.get("authed_user") or {}

    # Reject DM installs before writing anything. Slack still granted the bot
    # token at this point, but we don't persist it, so the DM webhook is never
    # used — the brand must re-install and pick a channel.
    if _is_dm_channel(incoming.get("channel_id")):
        logger.info(
            "Rejecting DM install: team=%s brand=%s channel=%s (%s)",
            team.get("id"), brand,
            incoming.get("channel_id"), incoming.get("channel"),
        )
        raise InstallTargetError(
            "INFLUENCE Bot must be installed to a channel, not a Direct Message."
        )

    session = SessionLocal()
    try:
        install = (
            session.query(SlackInstallation)
            .filter_by(team_id=team.get("id"), brand=brand)
            .one_or_none()
        )
        if install is None:
            install = SlackInstallation(team_id=team.get("id"), brand=brand)
            session.add(install)

        install.team_name = team.get("name")
        install.enterprise_id = enterprise.get("id")
        install.bot_user_id = data.get("bot_user_id")
        install.bot_token = data.get("access_token")
        install.scope = data.get("scope")
        install.channel_id = incoming.get("channel_id")
        install.channel_name = incoming.get("channel")
        install.webhook_url = incoming.get("url")
        install.installed_by_user_id = authed_user.get("id")

        session.commit()
        session.refresh(install)
        logger.info(
            "Slack install complete: team=%s brand=%s channel=%s",
            install.team_id, install.brand, install.channel_name,
        )
        return install
    finally:
        session.close()

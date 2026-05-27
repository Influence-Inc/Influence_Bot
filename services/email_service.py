"""
Email service for INFLUENCE Bot.

Sends email via the Resend HTTPS API. We use Resend because Railway blocks
outbound SMTP on most plans, which made the previous Gmail SMTP integration
unreliable. Both useinfluence.xyz and influence.technology must be verified
on the Resend account that issued RESEND_API_KEY.
"""

import logging
from enum import Enum

import requests
from sqlalchemy.exc import IntegrityError

from config import Config
from models.models import SessionLocal, EmailLog

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 20


class EmailSendResult(str, Enum):
    SENT = "sent"
    ALREADY_SENT = "already_sent"
    FAILED = "failed"


class EmailService:
    def __init__(self):
        self.api_key = Config.RESEND_API_KEY
        self.from_address = Config.EMAIL_FROM_ADDRESS
        self.from_name = Config.EMAIL_FROM_NAME
        self.reply_to = Config.EMAIL_REPLY_TO

    def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        cc: str = None,
        from_email: str = None,
        from_name: str = None,
        reply_to: str = None,
    ) -> bool:
        """
        Send an email via the Resend API.

        Defaults the From header to EMAIL_FROM_ADDRESS / EMAIL_FROM_NAME
        (jennifer@useinfluence.xyz). `from_email` / `from_name` can override
        for a single send, but the override domain must also be verified on
        Resend or the API will reject the send.

        Reply-To defaults to Config.EMAIL_REPLY_TO (which itself defaults to
        the From address) so creator replies land in jennifer@useinfluence.xyz
        instead of Resend's Return-Path. Pass `reply_to` to override per send.
        """
        if not self.api_key:
            logger.error(
                "RESEND_API_KEY is not set; cannot send email to %s. "
                "Set RESEND_API_KEY in Railway.",
                to_email,
            )
            return False

        effective_from_email = from_email or self.from_address
        effective_from_name = from_name or self.from_name
        effective_reply_to = reply_to or self.reply_to

        payload = {
            "from": f"{effective_from_name} <{effective_from_email}>",
            "to": [to_email],
            "subject": subject,
            "text": body,
        }
        if cc:
            payload["cc"] = [cc]
        if effective_reply_to:
            payload["reply_to"] = effective_reply_to

        try:
            response = requests.post(
                RESEND_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=RESEND_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            logger.error(
                "Failed to reach Resend API for %s as %s: %s",
                to_email, effective_from_email, e,
            )
            return False

        if response.status_code >= 400:
            logger.error(
                "Resend rejected email to %s as %s (HTTP %s): %s",
                to_email, effective_from_email, response.status_code, response.text,
            )
            return False

        logger.info(
            "Email sent to %s as %s: %s",
            to_email, effective_from_email, subject,
        )
        return True

    def send_followup(self, to_email: str, template_data: dict) -> bool:
        """Send a follow-up email using a template dict with 'subject' and 'body'."""
        return self.send_email(to_email, template_data["subject"], template_data["body"])

    def send_approval_notification(self, to_email: str, template_data: dict) -> bool:
        """Send an approval/changes-requested email."""
        return self.send_email(to_email, template_data["subject"], template_data["body"])

    def send_followup_if_not_sent(
        self,
        to_email: str,
        template_data: dict,
        template_type: str,
        campaign_id: str,
        creator_username: str,
    ) -> EmailSendResult:
        """
        Idempotent follow-up send. Checks EmailLog first; only attempts to send
        if no row exists for (recipient, template_type, campaign, creator).
        On send failure, no row is written — the next call will retry.
        """
        db = SessionLocal()
        try:
            existing = (
                db.query(EmailLog)
                .filter_by(
                    recipient_email=to_email,
                    template_type=template_type,
                    campaign_id=campaign_id,
                    creator_username=creator_username,
                )
                .first()
            )
            if existing:
                return EmailSendResult.ALREADY_SENT

            sent = self.send_followup(to_email, template_data)
            if not sent:
                return EmailSendResult.FAILED

            log = EmailLog(
                recipient_email=to_email,
                template_type=template_type,
                campaign_id=campaign_id,
                creator_username=creator_username,
            )
            db.add(log)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            return EmailSendResult.SENT
        finally:
            db.close()

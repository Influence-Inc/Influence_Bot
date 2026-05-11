"""
Email service for INFLUENCE Bot.

Two backends:
  - Resend HTTP API (preferred, set RESEND_API_KEY) — works on Railway because
    it uses HTTPS port 443.
  - Legacy SMTP (Gmail) — used only if RESEND_API_KEY is not set. Often blocked
    by Railway's outbound port filtering, surfacing as "timed out" errors.
"""

import logging
import smtplib
import socket
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum

import requests
from sqlalchemy.exc import IntegrityError

from config import Config
from models.models import SessionLocal, EmailLog

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


class EmailSendResult(str, Enum):
    SENT = "sent"
    ALREADY_SENT = "already_sent"
    FAILED = "failed"


class _IPv4SMTP(smtplib.SMTP):
    """SMTP that resolves the host to IPv4 only.

    Railway containers expose IPv6 in DNS but have no IPv6 default route, so
    ``socket.create_connection`` fails with ``[Errno 101] Network is
    unreachable`` before ever falling back to IPv4. Restricting resolution to
    AF_INET avoids that. ``self._host`` is left as the hostname so STARTTLS
    SNI and certificate verification still work.
    """

    def _get_socket(self, host, port, timeout):
        addrinfo = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        if not addrinfo:
            raise OSError(f"No IPv4 address resolved for {host}")
        return socket.create_connection(addrinfo[0][4], timeout, self.source_address)


class EmailService:
    def __init__(self):
        self.resend_api_key = Config.RESEND_API_KEY
        self.resend_from = Config.RESEND_FROM
        self.host = Config.SMTP_HOST
        self.port = Config.SMTP_PORT
        self.username = Config.SMTP_USERNAME
        self.password = Config.SMTP_PASSWORD
        self.from_name = Config.EMAIL_FROM_NAME

    def send_email(self, to_email: str, subject: str, body: str, cc: str = None) -> bool:
        """Send an email. Uses Resend HTTP API when configured, else SMTP."""
        if self.resend_api_key:
            return self._send_via_resend(to_email, subject, body, cc)
        return self._send_via_smtp(to_email, subject, body, cc)

    def _send_via_resend(self, to_email: str, subject: str, body: str, cc: str = None) -> bool:
        payload = {
            "from": self.resend_from,
            "to": [to_email],
            "subject": subject,
            "text": body,
        }
        if cc:
            payload["cc"] = [cc]

        try:
            resp = requests.post(
                RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {self.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15,
            )
        except requests.RequestException as e:
            logger.error(f"Failed to send email to {to_email} via Resend: {e}")
            return False

        if resp.status_code >= 300:
            logger.error(
                "Resend rejected email to %s: %s %s",
                to_email, resp.status_code, resp.text[:300],
            )
            return False

        logger.info(f"Email sent to {to_email} via Resend: {subject}")
        return True

    def _send_via_smtp(self, to_email: str, subject: str, body: str, cc: str = None) -> bool:
        if not self.password:
            logger.error(
                "Neither RESEND_API_KEY nor SMTP_PASSWORD is set; cannot send "
                "email to %s. Recommended: set RESEND_API_KEY in Railway.",
                to_email,
            )
            return False

        try:
            msg = MIMEMultipart()
            msg["From"] = f"{self.from_name} <{self.username}>"
            msg["To"] = to_email
            msg["Subject"] = subject
            if cc:
                msg["Cc"] = cc

            msg.attach(MIMEText(body, "plain"))

            recipients = [to_email]
            if cc:
                recipients.append(cc)

            with _IPv4SMTP(self.host, self.port, timeout=20) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.sendmail(self.username, recipients, msg.as_string())

            logger.info(f"Email sent to {to_email}: {subject}")
            return True

        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return False

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
        Idempotent follow-up send. Checks EmailLog first; only attempts SMTP
        if no row exists for (recipient, template_type, campaign, creator).
        On SMTP failure, no row is written — the next call will retry.
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

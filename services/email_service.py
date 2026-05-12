"""
Email service for INFLUENCE Bot.
Sends professional emails from jennifer@useinfluence.xyz via SMTP.
"""

import logging
import smtplib
import socket
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum

from sqlalchemy.exc import IntegrityError

from config import Config
from models.models import SessionLocal, EmailLog

logger = logging.getLogger(__name__)


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
        self.host = Config.SMTP_HOST
        self.port = Config.SMTP_PORT
        self.username = Config.SMTP_USERNAME
        self.password = Config.SMTP_PASSWORD
        self.from_name = Config.EMAIL_FROM_NAME

    def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        cc: str = None,
        from_email: str = None,
        from_name: str = None,
    ) -> bool:
        """
        Send an email via SMTP.

        `from_email` / `from_name` override the From header for this send
        only — useful for the chat-notification flow that mails creators
        as contact@influence.technology even though the SMTP account is
        authenticated as jennifer@useinfluence.xyz. The override address
        must be configured as a verified send-as alias on the SMTP account
        or the provider will reject the send.
        """
        if not self.password:
            logger.error(
                "SMTP_PASSWORD is not set; cannot send email to %s. "
                "Set SMTP_PASSWORD in Railway (use a Gmail App Password).",
                to_email,
            )
            return False

        effective_from_email = from_email or self.username
        effective_from_name = from_name or self.from_name

        try:
            msg = MIMEMultipart()
            msg["From"] = f"{effective_from_name} <{effective_from_email}>"
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
                # Envelope sender uses the override address too. Gmail will
                # 550 here if the override isn't a verified send-as alias.
                server.sendmail(effective_from_email, recipients, msg.as_string())

            logger.info(
                "Email sent to %s as %s: %s",
                to_email, effective_from_email, subject,
            )
            return True

        except Exception as e:
            logger.error(
                "Failed to send email to %s as %s: %s",
                to_email, effective_from_email, e,
            )
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

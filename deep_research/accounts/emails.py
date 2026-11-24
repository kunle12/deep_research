"""Outbound email for account verification / password reset.

Two implementations of the same tiny protocol:

- ``ConsoleEmailSender`` — prints the email to the server log. Default and
  fallback so a self-hosted instance without SMTP can still bootstrap its
  first account (the token is visible to whoever runs the server).
- ``SmtpEmailSender`` — stdlib smtplib via ``asyncio.to_thread`` (no new
  dependency; blocking SMTP must not stall the event loop).

Credentials always come from env vars (``*_env`` config pattern, see
docs/AGENTS.md) — never from config.yaml.
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from deep_research.config import PDLAuthSMTPConfig

logger = logging.getLogger(__name__)


@dataclass
class OutgoingEmail:
    to_address: str
    subject: str
    body: str


class EmailSender(Protocol):
    async def send(self, email: OutgoingEmail) -> None: ...


class ConsoleEmailSender:
    """Log the email instead of sending it (self-hosted bootstrap fallback)."""

    async def send(self, email: OutgoingEmail) -> None:
        logger.warning(
            "SMTP not configured — account email printed to the log instead.\n"
            "To: %s\nSubject: %s\n%s",
            email.to_address,
            email.subject,
            email.body,
        )


class SmtpEmailSender:
    """SMTP via stdlib smtplib, run in a worker thread."""

    def __init__(self, cfg: PDLAuthSMTPConfig) -> None:
        self._cfg = cfg
        self._from = cfg.from_address

    async def send(self, email: OutgoingEmail) -> None:
        if not self._from:
            raise ValueError("pdl.auth.smtp.from_address must be set to send email")
        username = os.environ.get(self._cfg.username_env, "")
        password = os.environ.get(self._cfg.password_env, "")
        await asyncio.to_thread(self._send_sync, email, username, password)

    def _send_sync(self, email: OutgoingEmail, username: str, password: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._from
        msg["To"] = email.to_address
        msg["Subject"] = email.subject
        msg.set_content(email.body)

        if self._cfg.use_starttls:
            context = ssl.create_default_context()
            with smtplib.SMTP(self._cfg.host, self._cfg.port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self._cfg.host, self._cfg.port, timeout=30) as smtp:
                smtp.ehlo()
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)


def build_email_sender(cfg: PDLAuthSMTPConfig) -> EmailSender:
    """Console fallback unless an SMTP host is configured."""
    if cfg.host:
        return SmtpEmailSender(cfg)
    return ConsoleEmailSender()

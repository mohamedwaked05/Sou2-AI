"""Replaceable transactional-email boundary with a Resend implementation."""

from functools import lru_cache
from typing import Protocol
from urllib.parse import quote

import resend

from app.core.config import Settings, get_settings


class EmailDeliveryError(Exception):
    """Raised without provider details when transactional delivery fails."""


class EmailService(Protocol):
    def send_verification_email(self, recipient: str, token: str) -> None: ...

    def send_password_reset_email(self, recipient: str, token: str) -> None: ...


class ResendEmailService:
    """Transactional email adapter that contains all Resend-specific behavior."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _send(self, *, recipient: str, subject: str, html: str) -> None:
        api_key = self.settings.resend_api_key
        if api_key is None or not api_key.get_secret_value():
            raise EmailDeliveryError("Transactional email is not configured.")
        try:
            resend.api_key = api_key.get_secret_value()
            resend.Emails.send(
                {
                    "from": self.settings.resend_sender_email,
                    "to": [recipient],
                    "subject": subject,
                    "html": html,
                }
            )
        except Exception as exc:
            raise EmailDeliveryError("Transactional email delivery failed.") from exc

    def _link(self, path: str, token: str) -> str:
        base = self.settings.frontend_base_url.rstrip("/")
        clean_path = "/" + path.lstrip("/")
        return f"{base}{clean_path}?token={quote(token, safe='')}"

    def send_verification_email(self, recipient: str, token: str) -> None:
        link = self._link(self.settings.verification_link_path, token)
        self._send(
            recipient=recipient,
            subject="Verify your Sou2AI email",
            html=(
                "<p>Verify your email to use Sou2AI.</p>"
                f'<p><a href="{link}">Verify email</a></p>'
            ),
        )

    def send_password_reset_email(self, recipient: str, token: str) -> None:
        link = self._link(self.settings.password_reset_link_path, token)
        self._send(
            recipient=recipient,
            subject="Reset your Sou2AI password",
            html=(
                "<p>A password reset was requested.</p>"
                f'<p><a href="{link}">Reset password</a></p>'
            ),
        )


@lru_cache
def get_email_service() -> EmailService:
    return ResendEmailService(get_settings())

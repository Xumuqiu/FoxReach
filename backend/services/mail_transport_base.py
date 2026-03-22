from dataclasses import dataclass

from app.models import Email, EmailAccount


@dataclass(frozen=True)
class SendResult:
    message_id: str
    smtp_response: str | None = None


class MailTransport:
    def send_email(self, account: EmailAccount, email: Email, to_address: str) -> SendResult:
        raise ValueError("Mail transport not configured")

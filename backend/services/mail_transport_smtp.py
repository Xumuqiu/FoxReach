import html
import re
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import quote

from app.core.config import settings
from app.models import Email, EmailAccount
from app.services.mail_transport_base import MailTransport, SendResult


class SMTPTransport(MailTransport):
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        track_base_url: str | None = None,
        message_id_domain: str | None = None,
    ):
        self.host = host or settings.EMAIL_SMTP_HOST
        self.port = port or settings.EMAIL_SMTP_PORT
        self.username = username or settings.EMAIL_USERNAME
        self.password = password or settings.EMAIL_PASSWORD
        self.track_base_url = track_base_url or settings.TRACK_BASE_URL
        self.message_id_domain = message_id_domain or settings.MESSAGE_ID_DOMAIN

    def send_email(self, account: EmailAccount, email: Email, to_address: str) -> SendResult:
        if not self.username or not self.password:
            raise ValueError("EMAIL_USERNAME/EMAIL_PASSWORD not configured")

        msg = EmailMessage()
        display_name = (account.sender_name or account.salesperson_name or "").strip()
        from_address = self.username
        msg["From"] = formataddr((display_name, from_address)) if display_name else from_address
        if account.email_address and account.email_address.strip() and account.email_address.strip().lower() != from_address.lower():
            reply_to = account.email_address.strip()
            msg["Reply-To"] = formataddr((display_name, reply_to)) if display_name else reply_to
        msg["To"] = to_address
        msg["Subject"] = self._normalize_subject(email.subject)

        message_id = f"<{uuid.uuid4()}@{self.message_id_domain}>"
        msg["Message-ID"] = message_id
        msg["X-YourApp-Email-ID"] = str(email.id)

        clean_body = self._clean_body_for_sending(email.body or "")
        plain = self._ensure_plain_text(clean_body)
        msg.set_content(plain)

        html_body = self._ensure_html_body(clean_body, email.id)
        msg.add_alternative(html_body, subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30) as server:
            server.login(self.username, self.password)
            result = server.send_message(msg)

        smtp_response = None
        if isinstance(result, dict) and result:
            smtp_response = str(result)

        return SendResult(message_id=message_id, smtp_response=smtp_response)

    def _normalize_subject(self, subject: str) -> str:
        s = (subject or "").strip()
        s = re.sub(r"^\s*\[#\d+\]\s*", "", s)
        s = re.sub(r"^\s*subject\s*:\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\r\n?|\n", " ", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        if len(s) > 160:
            s = s[:157].rstrip() + "..."
        return s or "(no subject)"

    def _clean_body_for_sending(self, body: str) -> str:
        text = body or ""
        if re.search(r"(?i)&lt;\s*(div|p|br|html|body)\b", text):
            text = html.unescape(text)

        text = re.sub(
            r'(?is)<\s*div\b[^>]*style\s*=\s*(["\'])\s*font-family\s*:\s*arial\s*,\s*helvetica\s*,\s*sans-serif\s*;\s*font-size\s*:\s*12pt\s*;\s*color\s*:\s*#333333\s*;\s*line-height\s*:\s*1\.5\s*;\s*\1[^>]*>',
            "",
            text,
        )
        text = re.sub(
            r'(?is)<\s*div\b[^>]*style\s*=\s*(["\'])\s*font-family\s*:\s*arial\s*,\s*helvetica\s*,\s*sans-serif\s*;\s*font-size\s*:\s*12pt\s*;\s*color\s*:\s*#333333\s*;\s*line-height\s*:\s*1\.5\s*\1[^>]*>',
            "",
            text,
        )
        if "<div" not in text.lower():
            text = re.sub(r"(?is)</\s*div\s*>", "", text)
        else:
            text = re.sub(r"(?is)</\s*div\s*>\s*$", "", text)
        return text.strip()

    def _ensure_html_body(self, body: str, email_id: int) -> str:
        body = self._rewrite_click_links(body, email_id)
        pixel_url = f"{self.track_base_url}/track/open?email_id={email_id}"
        pixel = f'<img src="{html.escape(pixel_url)}" width="1" height="1" />'

        lower = body.lower()
        if "<html" in lower or "<body" in lower:
            if "</body>" in lower:
                parts = body.rsplit("</body>", 1)
                return f"{parts[0]}{pixel}</body>{parts[1]}"
            return f"{body}{pixel}"

        if re.search(r"(?i)<\s*/?\s*(p|div|span|br|a|table|tbody|thead|tr|td|th|ul|ol|li|strong|em|b|i|h[1-6])\b", body):
            return f"<html><body>{body}{pixel}</body></html>"

        escaped = html.escape(body).replace("\n", "<br/>")
        return f"<html><body>{escaped}{pixel}</body></html>"

    def _rewrite_click_links(self, body: str, email_id: int) -> str:
        track_prefix = f"{self.track_base_url}/track/click?email_id={email_id}&url="

        def repl(match: re.Match) -> str:
            quote_char = match.group(1)
            url = match.group(2)
            if url.startswith(track_prefix) or url.startswith(f"{self.track_base_url}/track/click"):
                return match.group(0)
            encoded = quote(url, safe="")
            return f'href={quote_char}{track_prefix}{encoded}{quote_char}'

        return re.sub(r'href=(["\'])(https?://[^"\']+)\1', repl, body)

    def _ensure_plain_text(self, body: str) -> str:
        raw = body or ""
        raw_lower = raw.lower()
        unescaped = html.unescape(raw)
        unescaped_lower = unescaped.lower()
        looks_like_html = any(tag in raw_lower for tag in ("<html", "<body", "<div", "<br", "<p", "<span", "<style", "<table")) or any(
            tag in unescaped_lower for tag in ("<html", "<body", "<div", "<br", "<p", "<span", "<style", "<table")
        )
        if not looks_like_html:
            return raw.strip()

        text = unescaped
        text = re.sub(r"(?is)<\s*br\s*/?\s*>", "\n", text)
        text = re.sub(r"(?is)</\s*p\s*>", "\n\n", text)
        text = re.sub(r"(?is)<\s*p\b[^>]*>", "", text)
        text = re.sub(r"(?is)</\s*div\s*>", "\n", text)
        text = re.sub(r"(?is)<\s*div\b[^>]*>", "", text)
        text = re.sub(r"(?is)</\s*li\s*>", "\n", text)
        text = re.sub(r"(?is)<\s*li\b[^>]*>", "- ", text)
        text = re.sub(r"(?is)</\s*ul\s*>", "\n", text)
        text = re.sub(r"(?is)<\s*ul\b[^>]*>", "", text)
        text = re.sub(r"(?is)</\s*ol\s*>", "\n", text)
        text = re.sub(r"(?is)<\s*ol\b[^>]*>", "", text)
        text = re.sub(r"(?is)<[^>]+>", "", text)
        text = re.sub(r"\r\n?", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

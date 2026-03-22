import email
import json
import re
from email.message import Message
from email.parser import BytesParser
from email.policy import default

import imaplib
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import EmailEvent
from app.schemas.email_automation import EmailEventIn
from app.services.email_automation_service import EmailAutomationService


class IMAPPollingService:
    def __init__(
        self,
        db: Session,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        mailbox: str = "INBOX",
    ):
        self.db = db
        self.host = host or settings.EMAIL_IMAP_HOST
        self.port = port or settings.EMAIL_IMAP_PORT
        self.username = username or settings.EMAIL_USERNAME
        self.password = password or settings.EMAIL_PASSWORD
        self.mailbox = mailbox

    def poll(self) -> int:
        if not self.username or not self.password:
            raise ValueError("EMAIL_USERNAME/EMAIL_PASSWORD not configured")

        processed = 0
        with imaplib.IMAP4_SSL(self.host, self.port) as imap:
            imap.login(self.username, self.password)
            imap.select(self.mailbox)
            typ, data = imap.search(None, "UNSEEN")
            if typ != "OK":
                return 0

            ids = (data[0] or b"").split()
            if not ids:
                return 0

            for uid in ids:
                try:
                    msg = self._fetch_message(imap, uid)
                    if msg is None:
                        self._mark_seen(imap, uid)
                        continue

                    email_id = self._match_email_id(msg)
                    if email_id is None:
                        self._mark_seen(imap, uid)
                        continue

                    event_type = "auto_replied" if self._is_auto_reply(msg) else "replied"
                    meta = json.dumps(
                        {
                            "from": msg.get("From"),
                            "subject": msg.get("Subject"),
                            "in_reply_to": msg.get("In-Reply-To"),
                            "references": msg.get("References"),
                        }
                    )

                    EmailAutomationService(self.db).record_event(
                        EmailEventIn(email_id=email_id, event_type=event_type, meta=meta)
                    )
                    self._mark_seen(imap, uid)
                    processed += 1
                except Exception:
                    try:
                        self._mark_seen(imap, uid)
                    except Exception:
                        pass
                    continue

        return processed

    def _fetch_message(self, imap: imaplib.IMAP4_SSL, uid: bytes) -> Message | None:
        typ, data = imap.fetch(uid, "(RFC822)")
        if typ != "OK" or not data:
            return None
        raw = data[0][1] if isinstance(data[0], tuple) else None
        if not raw:
            return None
        return BytesParser(policy=default).parsebytes(raw)

    def _mark_seen(self, imap: imaplib.IMAP4_SSL, uid: bytes) -> None:
        imap.store(uid, "+FLAGS", "\\Seen")

    def _match_email_id(self, msg: Message) -> int | None:
        direct = msg.get("X-YourApp-Email-ID")
        if direct and str(direct).strip().isdigit():
            return int(str(direct).strip())

        in_reply_to = msg.get("In-Reply-To")
        references = msg.get("References")
        for token in self._extract_message_ids(" ".join([in_reply_to or "", references or ""])):
            email_id = self._find_email_id_by_sent_message_id(token)
            if email_id is not None:
                return email_id

        subject = msg.get("Subject") or ""
        m = re.search(r"\[#(\d+)\]", subject)
        if m:
            return int(m.group(1))

        return None

    def _extract_message_ids(self, value: str) -> list[str]:
        return re.findall(r"<[^>]+>", value or "")

    def _find_email_id_by_sent_message_id(self, message_id: str) -> int | None:
        row = (
            self.db.query(EmailEvent.email_id)
            .filter(
                EmailEvent.event_type == "sent",
                EmailEvent.meta.isnot(None),
                EmailEvent.meta.like(f"%{message_id}%"),
            )
            .order_by(EmailEvent.id.desc())
            .first()
        )
        if row is None:
            return None
        return int(row[0])

    def _is_auto_reply(self, msg: Message) -> bool:
        auto_submitted = (msg.get("Auto-Submitted") or "").strip().lower()
        if auto_submitted and auto_submitted != "no":
            return True

        precedence = (msg.get("Precedence") or "").strip().lower()
        if precedence in {"bulk", "junk", "list"}:
            return True

        x_autoreply = (msg.get("X-Autoreply") or "").strip()
        if x_autoreply:
            return True

        subject = (msg.get("Subject") or "").strip().lower()
        keywords = [
            "auto reply",
            "autoreply",
            "out of office",
            "vacation",
            "automatic reply",
            "自动回复",
            "不在办公室",
        ]
        if any(k in subject for k in keywords):
            return True

        from_header = (msg.get("From") or "").strip().lower()
        if "mailer-daemon" in from_header or "postmaster" in from_header:
            return True

        return False

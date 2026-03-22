"""
Email automation domain service.

This service owns:
- Creating sender accounts (EmailAccount)
- Storing drafts for human approval (Email.status = pending_approval)
- Sending emails now, or scheduling for later
- Recording tracking events (sent/opened/replied) and updating the customer state machine

Important behaviors:
- Drafts must be stored before sending so sales can review and edit content.
- Sending and scheduling should write EmailEvent(sent) and trigger CustomerState updates.
"""

import html
import json
from datetime import datetime, timedelta, time
from datetime import timezone as dt_timezone

from sqlalchemy.orm import Session

import re
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models import Customer, Email, EmailAccount, EmailEvent, EmailSchedule
from app.schemas.email_automation import (
    EmailAccountCreate,
    EmailAccountOut,
    EmailComposeRequest,
    EmailEventIn,
    EmailScheduleRequest,
    EmailSendNowRequest,
)
from app.schemas.followup import FollowUpEvent, FollowUpEventType
from app.services.followup_state_service import FollowUpStateService
from app.repositories.customer_assignment_repository import CustomerAssignmentRepository
from app.services.mail_transport_base import MailTransport
from app.services.mail_transport_smtp import SMTPTransport


class EmailAutomationService:
    def __init__(self, db: Session, transport: MailTransport | None = None):
        self.db = db
        self.transport = transport or SMTPTransport()

    def _single_sender_email(self) -> str:
        if not settings.EMAIL_USERNAME:
            raise ValueError("EMAIL_USERNAME is not configured")
        return settings.EMAIL_USERNAME.strip().lower()

    def get_single_sender_account(self) -> EmailAccount:
        """
        Enforces the demo rule:
        - Only one active EmailAccount is allowed
        - Its email_address must equal EMAIL_USERNAME (SMTP login user)
        """
        sender_email = self._single_sender_email()

        accounts = self.db.query(EmailAccount).all()
        keep: EmailAccount | None = None
        for a in accounts:
            if (a.email_address or "").strip().lower() == sender_email:
                keep = a
            else:
                a.is_active = False

        if keep is None:
            keep = EmailAccount(
                salesperson_name="Sales",
                sender_name="Sales",
                email_address=sender_email,
                provider="qq" if sender_email.endswith("@qq.com") else "custom",
                is_active=True,
                time_zone="Asia/Shanghai",
                country="CN",
            )
            self.db.add(keep)
        else:
            keep.is_active = True
            if not keep.sender_name:
                keep.sender_name = keep.salesperson_name

        self.db.commit()
        self.db.refresh(keep)
        return keep

    def create_account(self, payload: EmailAccountCreate) -> EmailAccountOut:
        sender_email = self._single_sender_email()
        if payload.email_address.strip().lower() != sender_email:
            raise ValueError("Only the authorized sender email can be used")

        account = self.get_single_sender_account()
        account.salesperson_name = payload.salesperson_name
        account.sender_name = payload.sender_name or payload.salesperson_name
        account.provider = payload.provider
        account.time_zone = payload.time_zone
        account.country = payload.country
        self.db.commit()
        self.db.refresh(account)
        return EmailAccountOut.model_validate(account)

    def update_account_sender_name(self, account_id: int, sender_name: str) -> EmailAccountOut:
        account = self.get_single_sender_account()
        if account.id != account_id:
            raise ValueError("Email account not found")
        account.sender_name = sender_name
        self.db.commit()
        self.db.refresh(account)
        return EmailAccountOut.model_validate(account)

    def list_accounts(self) -> list[EmailAccountOut]:
        try:
            account = self.get_single_sender_account()
        except ValueError:
            return []
        return [EmailAccountOut.model_validate(account)]

    def compose_email(self, payload: EmailComposeRequest) -> Email:
        """
        Creates a pending-approval email draft.

        This is the bridge between AI generation and the human approval workflow:
        - AI creates subject/body (or sales edits it)
        - This endpoint stores it so it appears in the approval board
        """
        customer = self.db.query(Customer).filter(Customer.id == payload.customer_id).first()
        if customer is None:
            raise ValueError("Customer not found")
        account = self.get_single_sender_account()

        subject = self._apply_placeholders(payload.subject, customer=customer, account=account)
        body = self._apply_placeholders(payload.body, customer=customer, account=account)
        country = payload.country or customer.country
        time_zone = payload.time_zone or customer.time_zone

        email = Email(
            customer_id=payload.customer_id,
            product_id=payload.product_id,
            strategy_id=payload.strategy_id,
            account_id=account.id,
            subject=subject,
            body=body,
            status="pending_approval",
            country=country,
            time_zone=time_zone,
        )
        self.db.add(email)
        self.db.commit()
        self.db.refresh(email)
        return email

    def schedule_email(self, payload: EmailScheduleRequest) -> EmailSchedule:
        """
        Creates an EmailSchedule entry for a draft email.

        The scheduler will later pick it up and attempt to deliver it.
        """
        email = self.db.query(Email).filter(Email.id == payload.email_id).first()
        if email is None:
            raise ValueError("Email not found")
        account = self.get_single_sender_account()
        if email.account_id is None:
            email.account_id = account.id
            self.db.commit()
        if email.account_id != account.id:
            raise ValueError("Email sender account is not authorized")

        existing = (
            self.db.query(EmailSchedule)
            .filter(
                EmailSchedule.email_id == email.id,
                EmailSchedule.status.in_(["pending", "sent"]),
            )
            .first()
        )
        if existing is not None:
            return existing

        account = self.get_single_sender_account()

        base_utc = payload.earliest_utc or datetime.utcnow()

        if payload.desired_local_hour is not None and email.time_zone is not None:
            scheduled_time_utc = self._calculate_local_send_time(
                base_utc=base_utc,
                local_hour=payload.desired_local_hour,
                time_zone=email.time_zone,
            )
        else:
            scheduled_time_utc = base_utc

        schedule = EmailSchedule(
            email_id=email.id,
            account_id=email.account_id,
            customer_id=email.customer_id,
            scheduled_time_utc=scheduled_time_utc,
            preferred_local_hour=payload.desired_local_hour,
            status="pending",
            next_attempt_at=scheduled_time_utc,
        )
        email.scheduled_at = scheduled_time_utc

        self.db.add(schedule)
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def send_now(self, payload: EmailSendNowRequest) -> Email:
        """
        Sends an email immediately.
        This operation is idempotent: if the email is already sent, it returns the existing record.
        """
        email = self.db.query(Email).filter(Email.id == payload.email_id).first()
        if email is None:
            raise ValueError("Email not found")
        account = self.get_single_sender_account()
        if email.account_id is None:
            email.account_id = account.id
            self.db.commit()
        if email.account_id != account.id:
            raise ValueError("Email sender account is not authorized")

        if email.sent_at is not None or email.status in {"sent", "opened", "replied"}:
            return email

        account = self.get_single_sender_account()

        customer = self.db.query(Customer).filter(Customer.id == email.customer_id).first()
        if customer is None:
            raise ValueError("Customer not found")

        email.subject = self._apply_placeholders(email.subject, customer=customer, account=account)
        email.body = self._apply_customer_sender_names(body=email.body, customer=customer, account=account)
        self.db.commit()

        # Update state first to prevent race conditions (optimistic locking)
        email.status = "sending"
        self.db.commit()

        try:
            send_result = self.transport.send_email(account, email, to_address=customer.email)
        except Exception as e:
            email.status = "failed"
            self.db.commit()
            raise e

        now_utc = datetime.utcnow()
        email.status = "sent"
        email.sent_at = now_utc

        event = EmailEvent(
            email_id=email.id,
            event_type="sent",
            occurred_at=now_utc,
            meta=json.dumps(
                {
                    "message_id": getattr(send_result, "message_id", None),
                    "smtp_response": getattr(send_result, "smtp_response", None),
                }
            ),
        )
        self.db.add(event)

        followup_service = FollowUpStateService(self.db)
        followup_service.handle_event(
            FollowUpEvent(
                customer_id=email.customer_id,
                email_id=email.id,
                event_type=FollowUpEventType.EMAIL_SENT,
            ),
            now=now_utc,
        )

        self.db.commit()
        self.db.refresh(email)
        return email

    def record_event(self, payload: EmailEventIn) -> EmailEvent:
        """
        Records tracking events and updates state.

        - opened -> email.status becomes opened (if previously sent)
        - replied -> state machine becomes REPLIED, and we assign the customer to the account
        """
        if payload.event_type in {"opened", "replied", "auto_replied"}:
            existing = (
                self.db.query(EmailEvent)
                .filter(
                    EmailEvent.email_id == payload.email_id,
                    EmailEvent.event_type == payload.event_type,
                )
                .first()
            )
            if existing is not None:
                return existing

        event = EmailEvent(
            email_id=payload.email_id,
            event_type=payload.event_type,
            meta=payload.meta,
        )
        self.db.add(event)

        email = self.db.query(Email).filter(Email.id == payload.email_id).first()

        if payload.event_type == "opened":
            if email is not None and email.status == "sent":
                email.status = "opened"

        followup_service = FollowUpStateService(self.db)
        if email is not None and payload.event_type in {"opened", "replied", "auto_replied"}:
            if payload.event_type == "opened":
                event_type = FollowUpEventType.EMAIL_OPENED
            elif payload.event_type == "replied":
                event_type = FollowUpEventType.EMAIL_REPLIED
            else:
                event_type = FollowUpEventType.EMAIL_AUTO_REPLIED

            followup_service.handle_event(
                FollowUpEvent(
                    customer_id=email.customer_id,
                    email_id=email.id,
                    event_type=event_type,
                )
            )

        if email is not None and payload.event_type == "replied" and email.account_id is not None:
            CustomerAssignmentRepository(self.db).upsert(email.customer_id, email.account_id)

        self.db.commit()
        self.db.refresh(event)
        return event

    def due_schedules(self, now_utc: datetime) -> list[EmailSchedule]:
        return (
            self.db.query(EmailSchedule)
            .filter(
                EmailSchedule.status == "pending",
                EmailSchedule.next_attempt_at <= now_utc,
            )
            .all()
        )

    def process_due_schedules(self, now_utc: datetime | None = None) -> None:
        """
        Sends scheduled emails that are due.

        Called by the APScheduler task. On successful send, this will:
        - set email.status = sent and set sent_at
        - write EmailEvent(sent)
        - update CustomerState (EMAIL_SENT) so the follow-up cadence can start
        """
        if now_utc is None:
            now_utc = datetime.utcnow()

        schedules = self.due_schedules(now_utc)
        for schedule in schedules:
            email = self.db.query(Email).filter(Email.id == schedule.email_id).first()
            if email is None:
                schedule.status = "failed"
                continue

            account = (
                self.db.query(EmailAccount)
                .filter(EmailAccount.id == schedule.account_id)
                .first()
            )
            if account is None:
                schedule.status = "failed"
                continue

            if email.sent_at is not None or email.status in {"sent", "opened", "replied"}:
                schedule.status = "sent"
                schedule.last_attempt_at = now_utc
                continue

            try:
                customer = self.db.query(Customer).filter(Customer.id == email.customer_id).first()
                if customer is None:
                    schedule.status = "failed"
                    schedule.last_attempt_at = now_utc
                    continue
                email.subject = self._apply_placeholders(email.subject, customer=customer, account=account)
                email.body = self._apply_customer_sender_names(body=email.body, customer=customer, account=account)
                self.db.commit()
                send_result = self.transport.send_email(account, email, to_address=customer.email)
            except Exception:
                schedule.last_attempt_at = now_utc
                schedule.attempt_count += 1
                if schedule.attempt_count >= schedule.max_attempts:
                    schedule.status = "failed"
                else:
                    schedule.next_attempt_at = now_utc + timedelta(minutes=15)
                continue

            email.status = "sent"
            email.sent_at = now_utc
            schedule.status = "sent"
            schedule.last_attempt_at = now_utc

            event = EmailEvent(
                email_id=email.id,
                event_type="sent",
                occurred_at=now_utc,
                meta=json.dumps(
                    {
                        "message_id": getattr(send_result, "message_id", None),
                        "smtp_response": getattr(send_result, "smtp_response", None),
                    }
                ),
            )
            self.db.add(event)

            followup_service = FollowUpStateService(self.db)
            followup_service.handle_event(
                FollowUpEvent(
                    customer_id=email.customer_id,
                    email_id=email.id,
                    event_type=FollowUpEventType.EMAIL_SENT,
                ),
                now=now_utc,
            )

        self.db.commit()

    def _infer_customer_first_name(self, customer: Customer) -> str | None:
        if customer.first_name and customer.first_name.strip():
            return customer.first_name.strip()
        if customer.name and customer.name.strip():
            return customer.name.strip().split()[0]
        return None

    def _infer_sender_full_name(self, account: EmailAccount) -> str | None:
        value = (account.sender_name or account.salesperson_name or "").strip()
        return value or None

    def _apply_placeholders(self, text: str, customer: Customer, account: EmailAccount) -> str:
        first_name = self._infer_customer_first_name(customer)
        sender_name = self._infer_sender_full_name(account)
        if not first_name and not sender_name:
            return text

        is_html = "<html" in text.lower() or "<body" in text.lower()
        first_value: str | None
        if first_name:
            first_value = html.escape(first_name) if is_html else first_name
        else:
            if re.search(r"\[\s*first\s*_?\s*name\s*\]|\{\{\s*first\s*_?\s*name\s*\}\}|\{\s*first\s*_?\s*name\s*\}|%{1,2}\s*first\s*_?\s*name\s*%{1,2}|<\s*first\s*_?\s*name\s*>", text, flags=re.IGNORECASE):
                first_value = html.escape("there") if is_html else "there"
            else:
                first_value = None
        sender_value = html.escape(sender_name) if (is_html and sender_name) else sender_name

        result = text
        if first_value:
            result = re.sub(r"\[\s*first\s*_?\s*name\s*\]", str(first_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\{\s*first\s*_?\s*name\s*\}\}", str(first_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\s*first\s*_?\s*name\s*\}", str(first_value), result, flags=re.IGNORECASE)
            result = re.sub(r"%{1,2}\s*first\s*_?\s*name\s*%{1,2}", str(first_value), result, flags=re.IGNORECASE)
            result = re.sub(r"<\s*first\s*_?\s*name\s*>", str(first_value), result, flags=re.IGNORECASE)
        if sender_value:
            result = re.sub(r"\[\s*your\s*_?\s*name\s*\]", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\{\s*your\s*_?\s*name\s*\}\}", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\s*your\s*_?\s*name\s*\}", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"%{1,2}\s*your\s*_?\s*name\s*%{1,2}", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"<\s*your\s*_?\s*name\s*>", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\[\s*sender\s*_?\s*name\s*\]", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\{\s*sender\s*_?\s*name\s*\}\}", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\s*sender\s*_?\s*name\s*\}", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"%{1,2}\s*sender\s*_?\s*name\s*%{1,2}", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"<\s*sender\s*_?\s*name\s*>", str(sender_value), result, flags=re.IGNORECASE)

        return result

    def _apply_customer_sender_names(self, body: str, customer: Customer, account: EmailAccount) -> str:
        first_name = self._infer_customer_first_name(customer)
        sender_name = self._infer_sender_full_name(account)
        if not first_name and not sender_name:
            return body

        is_html = "<html" in body.lower() or "<body" in body.lower()
        new_body = self._apply_placeholders(body, customer=customer, account=account)

        if first_name:
            if is_html:
                if "hi " not in body.lower()[:80] and "hello " not in body.lower()[:80]:
                    greeting = f"<p>Hi {html.escape(first_name)},</p>"
                    if "<body" in body.lower():
                        new_body = re.sub(r"(<body[^>]*>)", r"\\1" + greeting, new_body, count=1, flags=re.IGNORECASE)
                    else:
                        new_body = greeting + new_body
            else:
                head = body.lstrip()[:80].lower()
                if not (head.startswith("hi ") or head.startswith("hello ") or head.startswith("dear ")):
                    new_body = f"Hi {first_name},\n\n{new_body.lstrip()}"

        if sender_name:
            if is_html:
                if sender_name.lower() not in new_body.lower():
                    signature = f"<p>Best regards,<br/>{html.escape(sender_name)}</p>"
                    if "</body>" in new_body.lower():
                        new_body = re.sub(r"</body>", signature + "</body>", new_body, count=1, flags=re.IGNORECASE)
                    else:
                        new_body = new_body + signature
            else:
                if sender_name.lower() not in new_body.lower():
                    new_body = new_body.rstrip() + f"\n\nBest regards,\n{sender_name}\n"

        return new_body

    def _calculate_local_send_time(
        self,
        base_utc: datetime,
        local_hour: int,
        time_zone: str,
    ) -> datetime:
        tz = ZoneInfo(time_zone)
        base_utc_aware = base_utc.replace(tzinfo=dt_timezone.utc) if base_utc.tzinfo is None else base_utc.astimezone(dt_timezone.utc)
        base_local = base_utc_aware.astimezone(tz)
        target_local = datetime.combine(base_local.date(), time(hour=local_hour), tzinfo=tz)
        if target_local <= base_local:
            target_local = target_local + timedelta(days=1)
        return target_local.astimezone(dt_timezone.utc).replace(tzinfo=None)

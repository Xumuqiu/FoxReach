"""
Follow-up orchestrator.

Responsibility:
- Read current CustomerState (outreach state machine)
- Decide the next follow-up stage (CONTACTED -> FOLLOWUP_1 -> FOLLOWUP_2 -> FOLLOWUP_3)
- Build a stage-specific prompt using structured CustomerBackground and internal knowledge
- Call the LLM to generate a follow-up email draft and store it as pending_approval

Important:
- This is used both by the UI (generate-next) and by the scheduler (generate-due).
- Fails fast if OPENAI_API_KEY is missing to avoid mock output.
"""

import json
import html
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.ai_client import AIClient, get_default_ai_client
from app.core.config import settings
from app.models import Customer, CustomerBackground, CustomerState, Email, EmailAccount, EmailEvent
from app.prompts.followup_prompts import build_followup_email_prompt
from app.schemas.followup import FollowUpDraftRequest, FollowUpDraftResponse, FollowUpStatus
from app.schemas.strategy_engine import StrategyEngineRequest
from app.schemas.value_content import ValueContentRequest, ValueContentType
from app.services.company_knowledge_service import CompanyKnowledgeService
from app.services.email_automation_service import EmailAutomationService
from app.services.followup_state_service import FollowUpStateService
from app.services.value_content_service import ValueContentService


class FollowUpOrchestratorService:
    def __init__(self, db: Session, ai_client: AIClient | None = None):
        self.db = db
        self.ai_client = ai_client or get_default_ai_client()
        self.knowledge_service = CompanyKnowledgeService(db)
        self.value_content_service = ValueContentService(db, ai_client=self.ai_client)
        self.state_service = FollowUpStateService(db)

    def generate_next_draft(self, request: FollowUpDraftRequest) -> FollowUpDraftResponse:
        if not (settings.LLM_API_KEY or settings.OPENAI_API_KEY):
            raise RuntimeError("LLM_API_KEY is not configured")

        state = self.state_service.get_state(request.customer_id)
        if state.status in {FollowUpStatus.REPLIED, FollowUpStatus.STOPPED}:
            raise ValueError("Customer is not eligible for follow-up")

        stage = self._next_stage_from_state(state.status, state.sequence_step)
        if stage == FollowUpStatus.STOPPED:
            raise ValueError("Customer is stopped")

        existing = (
            self.db.query(Email)
            .filter(
                Email.customer_id == request.customer_id,
                Email.status == "pending_approval",
            )
            .first()
        )
        if existing is not None:
            raise ValueError("已有待审核草稿，请先发送或删除后再生成下一封")

        customer_background = (
            self.db.query(CustomerBackground)
            .filter(CustomerBackground.customer_id == request.customer_id)
            .first()
        )

        account_id = request.account_id or self._infer_account_id(request.customer_id)
        account_id = EmailAutomationService(self.db).get_single_sender_account().id

        strategy_request = StrategyEngineRequest(
            customer_id=request.customer_id,
            product_id=request.product_id,
            language=request.language,
        )

        angle = self._select_followup_angle(customer_id=request.customer_id, stage=stage)
        previous_emails = self._previous_outreach_emails(customer_id=request.customer_id, limit=2)
        content_type = self._value_content_type_for_stage(stage)
        value_content = self.value_content_service.generate(
            ValueContentRequest(
                customer_id=request.customer_id,
                product_id=request.product_id,
                content_type=content_type,
                language=request.language,
            )
        )

        prompt = build_followup_email_prompt(
            status=stage,
            request=strategy_request,
            customer_background=customer_background,
            knowledge_service=self.knowledge_service,
            value_content=value_content,
            previous_emails=previous_emails,
            angle=angle,
        )

        value_summary = value_content.items[0].body if value_content.items else ""

        try:
            output = self.ai_client.generate(prompt)
            subject, body = self._parse_subject_body(output)
        except Exception:
            subject, body = self._fallback_email(stage=stage, customer_background=customer_background, value_summary=value_summary)

        customer = self.db.query(Customer).filter(Customer.id == request.customer_id).first()
        account = self.db.query(EmailAccount).filter(EmailAccount.id == account_id).first() if account_id else None
        if customer is not None and account is not None:
            subject = self._apply_names_to_text(text=subject, customer=customer, account=account)
            body = self._apply_names_to_body(body=body, customer=customer, account=account)

        email = Email(
            customer_id=request.customer_id,
            product_id=request.product_id,
            strategy_id=None,
            account_id=account_id,
            subject=subject,
            body=body,
            status="pending_approval",
            country=customer.country if customer is not None else None,
            time_zone=customer.time_zone if customer is not None else None,
        )
        self.db.add(email)
        self.db.commit()
        self.db.refresh(email)
        self.db.add(
            EmailEvent(
                email_id=email.id,
                event_type="draft_generated",
                meta=json.dumps({"stage": stage.value, "angle": angle}),
            )
        )
        self.db.commit()

        return FollowUpDraftResponse(customer_id=request.customer_id, email_id=email.id, stage=stage)

    def _value_content_type_for_stage(self, stage: FollowUpStatus) -> ValueContentType:
        if stage == FollowUpStatus.FOLLOWUP_1:
            return ValueContentType.product_trend_report
        if stage == FollowUpStatus.FOLLOWUP_2:
            return ValueContentType.market_analysis
        if stage == FollowUpStatus.FOLLOWUP_3:
            return ValueContentType.design_inspiration
        return ValueContentType.industry_insights

    def _previous_outreach_emails(self, customer_id: int, limit: int = 2) -> list[tuple[str, str]]:
        rows = (
            self.db.query(Email.subject, Email.body)
            .filter(
                Email.customer_id == customer_id,
                Email.status.in_(["sent", "opened", "replied"]),
            )
            .order_by(Email.id.desc())
            .limit(limit)
            .all()
        )
        result: list[tuple[str, str]] = []
        for subj, body in rows:
            s = (subj or "")[:200]
            b = (body or "")[:1500]
            result.append((s, b))
        return result

    def _select_followup_angle(self, customer_id: int, stage: FollowUpStatus) -> str:
        options: list[str]
        if stage == FollowUpStatus.FOLLOWUP_1:
            options = [
                "Lightly reference last email; add one concrete value point (MOQ/lead time/customization/certifications).",
                "Lightly reference last email; add one relevant success case highlight.",
                "Lightly reference last email; add one product/market insight tied to the customer's context.",
            ]
        elif stage == FollowUpStatus.FOLLOWUP_2:
            options = [
                "Busy-friendly follow-up with a low-friction CTA (two options).",
                "Busy-friendly follow-up with a yes/no question and a concrete suggestion.",
                "Busy-friendly follow-up offering a quick sample/quote idea with a single question.",
            ]
        elif stage == FollowUpStatus.FOLLOWUP_3:
            options = [
                "Exit-style follow-up: confirm relevance and offer to pause outreach.",
                "Exit-style follow-up: ask if there's a better contact for this topic.",
                "Exit-style follow-up: confirm priority and propose closing the loop politely.",
            ]
        else:
            options = ["Value-first outreach with a soft CTA."]

        used: set[str] = set()
        rows = (
            self.db.query(EmailEvent.meta)
            .join(Email, Email.id == EmailEvent.email_id)
            .filter(
                Email.customer_id == customer_id,
                EmailEvent.event_type == "draft_generated",
            )
            .order_by(EmailEvent.id.desc())
            .limit(20)
            .all()
        )
        for (meta,) in rows:
            try:
                data = json.loads(meta) if meta else {}
            except Exception:
                data = {}
            if data.get("stage") == stage.value and data.get("angle"):
                used.add(str(data["angle"]))

        for opt in options:
            if opt not in used:
                return opt
        return options[0]

    def _infer_customer_first_name(self, customer: Customer) -> str | None:
        if customer.first_name and customer.first_name.strip():
            return customer.first_name.strip()
        if customer.name and customer.name.strip():
            return customer.name.strip().split()[0]
        return None

    def _infer_sender_full_name(self, account: EmailAccount) -> str | None:
        value = (account.sender_name or account.salesperson_name or "").strip()
        return value or None

    def _apply_names_to_text(self, text: str, customer: Customer, account: EmailAccount) -> str:
        first_name = self._infer_customer_first_name(customer)
        sender_name = self._infer_sender_full_name(account)
        if not first_name and not sender_name:
            return text

        is_html = "<html" in text.lower() or "<body" in text.lower()
        first_value = html.escape(first_name) if (is_html and first_name) else first_name
        sender_value = html.escape(sender_name) if (is_html and sender_name) else sender_name

        result = text
        if first_value:
            result = re.sub(r"\[\s*first\s*_?\s*name\s*\]", str(first_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\{\s*first\s*_?\s*name\s*\}\}", str(first_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\s*first\s*_?\s*name\s*\}", str(first_value), result, flags=re.IGNORECASE)
        if sender_value:
            result = re.sub(r"\[\s*your\s*_?\s*name\s*\]", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\{\s*your\s*_?\s*name\s*\}\}", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\s*your\s*_?\s*name\s*\}", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\[\s*sender\s*_?\s*name\s*\]", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\{\s*sender\s*_?\s*name\s*\}\}", str(sender_value), result, flags=re.IGNORECASE)
            result = re.sub(r"\{\s*sender\s*_?\s*name\s*\}", str(sender_value), result, flags=re.IGNORECASE)

        return result

    def _apply_names_to_body(self, body: str, customer: Customer, account: EmailAccount) -> str:
        first_name = self._infer_customer_first_name(customer)
        sender_name = self._infer_sender_full_name(account)
        if not first_name and not sender_name:
            return body

        is_html = "<html" in body.lower() or "<body" in body.lower()
        first_value = html.escape(first_name) if (is_html and first_name) else first_name
        sender_value = html.escape(sender_name) if (is_html and sender_name) else sender_name

        new_body = self._apply_names_to_text(text=body, customer=customer, account=account)

        if is_html:
            return new_body

        new_body = new_body.lstrip()
        if first_name:
            head = new_body[:80].lower()
            if not (head.startswith("hi ") or head.startswith("hello ") or head.startswith("dear ")):
                new_body = f"Hi {first_name},\n\n{new_body}"

        if sender_name and sender_name.lower() not in new_body.lower():
            new_body = new_body.rstrip() + f"\n\nBest regards,\n{sender_name}\n"

        return new_body

    def generate_due_drafts(self, now: datetime | None = None) -> list[FollowUpDraftResponse]:
        if now is None:
            now = datetime.utcnow()

        # Find customers who are in an active follow-up state (CONTACTED, FOLLOWUP_1, FOLLOWUP_2)
        # We also include EMAIL_OPENED, but typically we want to follow up even if they didn't open
        target_statuses = [
            FollowUpStatus.CONTACTED.value,
            FollowUpStatus.FOLLOWUP_1.value,
            FollowUpStatus.FOLLOWUP_2.value,
            FollowUpStatus.EMAIL_OPENED.value,
        ]
        
        states = self.db.query(CustomerState).filter(CustomerState.status.in_(target_statuses)).all()
        results: list[FollowUpDraftResponse] = []

        for st in states:
            state = self.state_service.get_state(st.customer_id)
            
            # Skip if already replied or stopped
            if state.status in (FollowUpStatus.REPLIED, FollowUpStatus.STOPPED):
                continue

            delay = self.state_service.next_followup_delay(state)
            if delay is None:
                continue

            # Base the delay on the last contact time
            last_contact = state.last_contacted_at
            if last_contact is None:
                continue

            due_at = last_contact + delay
            if due_at > now:
                continue

            existing = (
                self.db.query(Email)
                .filter(
                    Email.customer_id == st.customer_id,
                    Email.status == "pending_approval",
                )
                .first()
            )
            if existing is not None:
                continue

            results.append(
                self.generate_next_draft(
                    FollowUpDraftRequest(customer_id=st.customer_id)
                )
            )

        return results

    def _latest_opened_at(self, customer_id: int) -> datetime | None:
        row = (
            self.db.query(EmailEvent.occurred_at)
            .join(Email, Email.id == EmailEvent.email_id)
            .filter(Email.customer_id == customer_id, EmailEvent.event_type == "opened")
            .order_by(EmailEvent.occurred_at.desc())
            .first()
        )
        if row is None:
            return None
        return row[0]

    def _infer_account_id(self, customer_id: int) -> int | None:
        row = (
            self.db.query(Email.account_id)
            .filter(Email.customer_id == customer_id, Email.account_id.isnot(None))
            .order_by(Email.id.desc())
            .first()
        )
        if row is None:
            return None
        return row[0]

    def _next_stage_from_state(self, status: FollowUpStatus, sequence_step: int) -> FollowUpStatus:
        if sequence_step <= 0:
            return FollowUpStatus.CONTACTED
        if sequence_step == 1:
            return FollowUpStatus.FOLLOWUP_1
        if sequence_step == 2:
            return FollowUpStatus.FOLLOWUP_2
        if sequence_step == 3:
            return FollowUpStatus.FOLLOWUP_3
        return FollowUpStatus.STOPPED

    def _parse_subject_body(self, output: str) -> tuple[str, str]:
        subject = "Follow-up"
        body = output
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        for idx, line in enumerate(lines):
            lower = line.lower()
            if lower.startswith("subject:"):
                subject = line.split(":", 1)[1].strip()
                body = "\n".join(lines[idx + 1 :]).strip()
                break
        return subject, body

    def _fallback_email(
        self,
        stage: FollowUpStatus,
        customer_background: CustomerBackground | None,
        value_summary: str,
    ) -> tuple[str, str]:
        company = customer_background.company_name if customer_background else "your company"
        if stage == FollowUpStatus.FOLLOWUP_1:
            subject = f"Quick follow-up on a value idea for {company}"
        elif stage == FollowUpStatus.FOLLOWUP_2:
            subject = f"One more idea that may help {company}"
        elif stage == FollowUpStatus.FOLLOWUP_3:
            subject = f"Checking in — is this relevant for {company}?"
        else:
            subject = f"Following up"

        body_lines: list[str] = []
        body_lines.append("Hi there,")
        body_lines.append("")
        body_lines.append(value_summary)
        body_lines.append("")
        if stage == FollowUpStatus.FOLLOWUP_3:
            body_lines.append("If now isn’t the right time, no worries—just let me know and I’ll stop reaching out.")
        else:
            body_lines.append("If this is interesting, happy to share a few ideas—would a quick chat be helpful?")
        body_lines.append("")
        body_lines.append("Best regards,")
        body_lines.append("Sales Team")

        return subject, "\n".join(body_lines)

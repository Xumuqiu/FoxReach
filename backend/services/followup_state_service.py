"""
Follow-up state machine service.

This service implements the outreach lifecycle for each customer:
- NEW_LEAD -> CONTACTED -> (FOLLOWUP_1 -> FOLLOWUP_2 -> FOLLOWUP_3) -> STOPPED
- EMAIL_OPENED / REPLIED are event-driven states that can interrupt the sequence.

Key fields:
- sequence_step: how many times we have sent an email to this customer
- last_contacted_at: timestamp of last send (used to determine due follow-ups)
- next_action: a simple label to guide UI/automation

This module is intentionally deterministic: given the same inputs, it produces the same state.
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import CustomerState
from app.repositories.customer_state_repository import CustomerStateRepository
from app.schemas.followup import (
    CustomerFollowUpState,
    FollowUpEvent,
    FollowUpEventType,
    FollowUpStatus,
)


class FollowUpStateService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = CustomerStateRepository(db)

    def get_state(self, customer_id: int) -> CustomerFollowUpState:
        state = self.repository.get_or_create(customer_id)
        return CustomerFollowUpState(
            customer_id=state.customer_id,
            status=FollowUpStatus(state.status),
            sequence_step=state.sequence_step,
            last_contacted_at=state.last_contacted_at,
            next_action=state.next_action,
        )

    def handle_event(self, event: FollowUpEvent, now: datetime | None = None) -> CustomerFollowUpState:
        if now is None:
            now = datetime.utcnow()

        state = self.repository.get_or_create(event.customer_id)

        if event.event_type == FollowUpEventType.EMAIL_SENT:
            self._on_email_sent(state, now)
        elif event.event_type == FollowUpEventType.EMAIL_OPENED:
            self._on_email_opened(state)
        elif event.event_type == FollowUpEventType.EMAIL_REPLIED:
            self._on_email_replied(state)
        elif event.event_type == FollowUpEventType.EMAIL_AUTO_REPLIED:
            self._on_email_auto_replied(state, now)

        self.repository.save(state)

        return CustomerFollowUpState(
            customer_id=state.customer_id,
            status=FollowUpStatus(state.status),
            sequence_step=state.sequence_step,
            last_contacted_at=state.last_contacted_at,
            next_action=state.next_action,
        )

    def _on_email_sent(self, state: CustomerState, now: datetime) -> None:
        state.sequence_step += 1
        state.last_contacted_at = now

        if state.sequence_step == 1:
            state.status = FollowUpStatus.CONTACTED.value
            state.next_action = "WAIT_OPEN_OR_REPLY"
        elif state.sequence_step == 2:
            state.status = FollowUpStatus.FOLLOWUP_1.value
            state.next_action = "WAIT_OPEN_OR_REPLY"
        elif state.sequence_step == 3:
            state.status = FollowUpStatus.FOLLOWUP_2.value
            state.next_action = "WAIT_OPEN_OR_REPLY"
        elif state.sequence_step == 4:
            state.status = FollowUpStatus.FOLLOWUP_3.value
            state.next_action = "WAIT_OPEN_OR_REPLY"
        else:
            state.status = FollowUpStatus.STOPPED.value
            state.next_action = None

    def _on_email_opened(self, state: CustomerState) -> None:
        if state.status in {
            FollowUpStatus.CONTACTED.value,
            FollowUpStatus.FOLLOWUP_1.value,
            FollowUpStatus.FOLLOWUP_2.value,
        }:
            state.status = FollowUpStatus.EMAIL_OPENED.value
            state.next_action = "SCHEDULE_NEXT_FOLLOWUP"

    def _on_email_replied(self, state: CustomerState) -> None:
        state.status = FollowUpStatus.REPLIED.value
        state.next_action = None

    def _on_email_auto_replied(self, state: CustomerState, now: datetime) -> None:
        current = CustomerFollowUpState(
            customer_id=state.customer_id,
            status=FollowUpStatus(state.status),
            sequence_step=state.sequence_step,
            last_contacted_at=state.last_contacted_at,
            next_action=state.next_action,
        )
        delay = self.next_followup_delay(current)
        if delay is None:
            return
        state.last_contacted_at = now - delay + timedelta(days=3)
        if state.next_action is None:
            state.next_action = "WAIT_OPEN_OR_REPLY"

    def next_followup_delay(self, state: CustomerFollowUpState) -> timedelta | None:
        # If replied or stopped, no more follow-ups
        if state.status in (FollowUpStatus.REPLIED, FollowUpStatus.STOPPED):
            return None
            
        # 1-3-7 Logic based on sequence step
        # Step 1 (Contacted) -> Wait 1 day -> Send FollowUp 1
        if state.sequence_step == 1:
            return timedelta(days=1)
        # Step 2 (FollowUp 1) -> Wait 3 days -> Send FollowUp 2
        if state.sequence_step == 2:
            return timedelta(days=3)
        # Step 3 (FollowUp 2) -> Wait 7 days -> Send FollowUp 3
        if state.sequence_step == 3:
            return timedelta(days=7)

        return None

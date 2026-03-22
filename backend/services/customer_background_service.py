"""
Customer background service.

This service manages the structured background fields that sales fills in.
These fields are the primary input for AI strategy and email generation.

The front-end uses this via:
- GET  /customers/{id}/background
- PUT  /customers/{id}/background
"""

from sqlalchemy.orm import Session

from app.repositories.customer_background_repository import CustomerBackgroundRepository
from app.schemas.customer_background import (
    CustomerBackgroundCreate,
    CustomerBackgroundOut,
    CustomerBackgroundUpdate,
)


class CustomerBackgroundService:
    def __init__(self, db: Session):
        self.repository = CustomerBackgroundRepository(db)

    def upsert_for_customer(
        self,
        customer_id: int,
        payload: CustomerBackgroundCreate | CustomerBackgroundUpdate,
    ) -> CustomerBackgroundOut:
        data = payload.dict(exclude_unset=True)
        instance = self.repository.create_or_update(customer_id, data)
        return CustomerBackgroundOut.model_validate(instance)

    def get_for_customer(self, customer_id: int) -> CustomerBackgroundOut | None:
        instance = self.repository.get_by_customer_id(customer_id)
        if instance is None:
            return None
        return CustomerBackgroundOut.model_validate(instance)

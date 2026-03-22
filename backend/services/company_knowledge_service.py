"""
Company knowledge service.

This service exposes the seller-side "knowledge base" stored in the DB:
- Product matrix (what we can make)
- Company capabilities (MOQ, lead time, certifications, customization)
- Success cases (proof points)

AI prompts consume this data to make outputs more grounded and less generic.
"""

from sqlalchemy.orm import Session

from app.repositories.company_capability_repository import CompanyCapabilityRepository
from app.repositories.product_matrix_repository import ProductMatrixRepository
from app.repositories.success_case_repository import SuccessCaseRepository
from app.schemas.company_capability import (
    CompanyCapabilityCreate,
    CompanyCapabilityOut,
)
from app.schemas.product_matrix import ProductMatrixCreate, ProductMatrixOut
from app.schemas.success_case import SuccessCaseCreate, SuccessCaseOut


class CompanyKnowledgeService:
    def __init__(self, db: Session):
        self.product_matrix_repository = ProductMatrixRepository(db)
        self.company_capability_repository = CompanyCapabilityRepository(db)
        self.success_case_repository = SuccessCaseRepository(db)

    def list_product_matrix(self) -> list[ProductMatrixOut]:
        items = self.product_matrix_repository.list()
        return [ProductMatrixOut.model_validate(item) for item in items]

    def add_product_matrix(self, payload: ProductMatrixCreate) -> ProductMatrixOut:
        instance = self.product_matrix_repository.create(payload.dict(exclude_unset=True))
        return ProductMatrixOut.model_validate(instance)

    def list_company_capabilities(self) -> list[CompanyCapabilityOut]:
        items = self.company_capability_repository.list()
        return [CompanyCapabilityOut.model_validate(item) for item in items]

    def add_company_capability(
        self,
        payload: CompanyCapabilityCreate,
    ) -> CompanyCapabilityOut:
        instance = self.company_capability_repository.create(payload.dict(exclude_unset=True))
        return CompanyCapabilityOut.model_validate(instance)

    def list_success_cases(self) -> list[SuccessCaseOut]:
        items = self.success_case_repository.list()
        return [SuccessCaseOut.model_validate(item) for item in items]

    def add_success_case(self, payload: SuccessCaseCreate) -> SuccessCaseOut:
        instance = self.success_case_repository.create(payload.dict(exclude_unset=True))
        return SuccessCaseOut.model_validate(instance)

    def delete_product_matrix(self, item_id: int) -> bool:
        return self.product_matrix_repository.delete_by_id(item_id)

    def delete_company_capability(self, item_id: int) -> bool:
        return self.company_capability_repository.delete_by_id(item_id)

    def delete_success_case(self, item_id: int) -> bool:
        return self.success_case_repository.delete_by_id(item_id)

"""
Value content service.

Purpose:
- Generate short reusable "value blocks" that can be inserted into cold emails,
  such as industry insights, product trend summaries, or market analysis.

Inputs:
- Structured CustomerBackground from DB
- Internal company knowledge (product matrix, capabilities, success cases)

Outputs:
- A ValueContentResponse with one or more blocks that downstream prompts can reuse
  to keep emails concrete and less generic.
"""

from sqlalchemy.orm import Session

from app.core.ai_client import AIClient, get_default_ai_client
from app.models import CustomerBackground, Product
from app.prompts.value_content_prompts import build_value_content_prompt
from app.schemas.value_content import (
    ValueContentItem,
    ValueContentRequest,
    ValueContentResponse,
)
from app.services.company_knowledge_service import CompanyKnowledgeService


class ValueContentService:
    def __init__(self, db: Session, ai_client: AIClient | None = None):
        self.db = db
        self.ai_client = ai_client or get_default_ai_client()
        self.knowledge_service = CompanyKnowledgeService(db)

    def _load_customer_background(self, customer_id: int) -> CustomerBackground | None:
        return (
            self.db.query(CustomerBackground)
            .filter(CustomerBackground.customer_id == customer_id)
            .first()
        )

    def _load_product_name(self, product_id: int | None) -> str | None:
        if product_id is None:
            return None
        product = self.db.query(Product).filter(Product.id == product_id).first()
        if product is None:
            return None
        return product.name

    def generate(self, request: ValueContentRequest) -> ValueContentResponse:
        customer_background = self._load_customer_background(request.customer_id)

        prompt = build_value_content_prompt(
            request=request,
            customer_background=customer_background,
            knowledge_service=self.knowledge_service,
        )

        try:
            raw_output = self.ai_client.generate(prompt)
        except Exception:
            raw_output = self._build_fallback_output(request, customer_background)

        items = [
            ValueContentItem(
                title=f"Value content for customer {request.customer_id}",
                summary="AI-generated value content based on structured customer and company data.",
                body=raw_output,
            )
        ]

        return ValueContentResponse(
            content_type=request.content_type,
            customer_id=request.customer_id,
            product_id=request.product_id,
            items=items,
        )

    def _build_fallback_output(
        self,
        request: ValueContentRequest,
        customer_background: CustomerBackground | None,
    ) -> str:
        product_name = self._load_product_name(request.product_id)
        parts: list[str] = []

        parts.append("AI client is not configured; this is a structured fallback text.")

        if request.content_type.value == "industry_insights":
            parts.append(
                "Share 2–3 high-level industry insights relevant to the customer's "
                "main market and product categories."
            )
        elif request.content_type.value == "product_trend_report":
            parts.append(
                "Outline key product trends, including design and material changes, "
                "that are relevant for the coming season."
            )
        elif request.content_type.value == "design_inspiration":
            parts.append(
                "Describe several design directions (colors, structures, finishing) "
                "that could inspire the customer's next collection."
            )
        elif request.content_type.value == "market_analysis":
            parts.append(
                "Provide a concise market analysis: demand drivers, pricing tiers, and "
                "differentiation opportunities."
            )

        if customer_background is not None:
            parts.append("")
            parts.append("Customer context:")
            parts.append(f"- Company name: {customer_background.company_name}")
            if customer_background.main_market:
                parts.append(f"- Main market: {customer_background.main_market}")
            if customer_background.target_customer_profile:
                parts.append(
                    f"- Target customer profile: {customer_background.target_customer_profile}"
                )

        if product_name:
            parts.append("")
            parts.append(f"Focus on product: {product_name}")

        return "\n".join(parts)

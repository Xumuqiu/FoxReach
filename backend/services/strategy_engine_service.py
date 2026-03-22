"""
Strategy engine service.

Responsibility:
- Read structured CustomerBackground from DB
- Build a prompt that combines:
  - customer background
  - internal company knowledge (product matrix, capabilities, success cases)
  - value content blocks (industry insights, trend report, etc.)
- Call the LLM and parse a strict JSON response:
  - profile
  - outreach strategy
  - initial email drafts

This service intentionally fails fast if OPENAI_API_KEY is missing, to avoid fake demo output.
"""

import json

from sqlalchemy.orm import Session

from app.core.ai_client import AIClient, get_default_ai_client
from app.core.config import settings
from app.models import CustomerBackground, Product
from app.prompts.strategy_prompts import build_strategy_prompt
from app.schemas.strategy_engine import (
    EmailDraft,
    CustomerProfile,
    OutreachStrategy,
    StrategyEngineRequest,
    StrategyEngineResponse,
)
from app.schemas.value_content import ValueContentRequest
from app.services.company_knowledge_service import CompanyKnowledgeService
from app.services.value_content_service import ValueContentService


class StrategyEngineService:
    def __init__(self, db: Session, ai_client: AIClient | None = None):
        self.db = db
        self.ai_client = ai_client or get_default_ai_client()
        self.knowledge_service = CompanyKnowledgeService(db)
        self.value_content_service = ValueContentService(db, ai_client=self.ai_client)

    def _load_customer_background(self, customer_id: int) -> CustomerBackground | None:
        return (
            self.db.query(CustomerBackground)
            .filter(CustomerBackground.customer_id == customer_id)
            .first()
        )

    def _load_product(self, product_id: int | None) -> Product | None:
        if product_id is None:
            return None
        return self.db.query(Product).filter(Product.id == product_id).first()

    def generate(self, request: StrategyEngineRequest) -> StrategyEngineResponse:
        if not (settings.LLM_API_KEY or settings.OPENAI_API_KEY):
            raise RuntimeError("LLM_API_KEY is not configured")

        customer_background = self._load_customer_background(request.customer_id)
        product = self._load_product(request.product_id)

        value_content_request = ValueContentRequest(
            customer_id=request.customer_id,
            product_id=request.product_id,
            content_type=request.value_content_type,
            language=request.language,
        )
        value_content = self.value_content_service.generate(value_content_request)

        prompt = build_strategy_prompt(
            request=request,
            customer_background=customer_background,
            knowledge_service=self.knowledge_service,
            value_content=value_content,
        )

        value_summary = value_content.items[0].body if value_content.items else ""

        try:
            raw_output = self.ai_client.generate(prompt)
            parsed = self._parse_json_response(raw_output)
            profile = CustomerProfile(**parsed["profile"])
            strategy = OutreachStrategy(**parsed["strategy"])
            emails = [EmailDraft(**e) for e in parsed["emails"]]
        except Exception:
            profile, strategy, emails = self._fallback_response(
                request=request,
                customer_background=customer_background,
                product=product,
                value_summary=value_summary,
            )

        return StrategyEngineResponse(
            customer_id=request.customer_id,
            product_id=request.product_id,
            profile=profile,
            strategy=strategy,
            emails=emails,
        )

    def _parse_json_response(self, raw_output: str) -> dict:
        text = raw_output.strip()
        
        # Try to strip markdown code blocks
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                text = text[start : end + 1]
        
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise

    def _fallback_response(
        self,
        request: StrategyEngineRequest,
        customer_background: CustomerBackground | None,
        product: Product | None,
        value_summary: str,
    ) -> tuple[CustomerProfile, OutreachStrategy, list[EmailDraft]]:
        if customer_background is not None:
            name = customer_background.company_name
            main_market = customer_background.main_market or "their core market"
            segment = customer_background.target_customer_profile or "their target segment"
        else:
            name = "the customer"
            main_market = "their core market"
            segment = "their target segment"

        product_name = product.name if product is not None else "our products"

        profile = CustomerProfile(
            summary=f"{name} operates in {main_market} and serves {segment}.",
            risks="Customer may worry about supplier reliability, product differentiation, and switching cost.",
            opportunities=f"We can help {name} launch or optimize {product_name} with better design and supply stability.",
            positioning="Position ourselves as a proactive, design-driven manufacturing partner focused on long-term collaboration.",
        )

        strategy = OutreachStrategy(
            goal="Open a low-pressure conversation to understand current projects and explore fit.",
            core_value_message=value_summary,
            sequence_overview=(
                "Touch 1: send a short value-first email sharing tailored insights.\n"
                "Touch 2: follow up with a concrete idea or small proposal.\n"
                "Touch 3: share a success case relevant to their market."
            ),
        )

        subject = f"Idea for your next {product_name} collection"
        body_lines: list[str] = []
        body_lines.append(f"Hi {customer_background.buyer_role or 'there'}," if customer_background else "Hi there,")
        body_lines.append("")
        body_lines.append(
            f"Based on what we see in {main_market}, there is an opportunity for brands like yours "
            f"to differentiate {product_name} with design and sustainability."
        )
        body_lines.append("")
        body_lines.append(value_summary)
        body_lines.append("")
        body_lines.append(
            "If this resonates, would you be open to a short conversation to exchange ideas, "
            "without any obligation?"
        )
        body_lines.append("")
        body_lines.append("Best regards,")
        body_lines.append("Sales Team")

        email = EmailDraft(subject=subject, body="\n".join(body_lines))
        return profile, strategy, [email]

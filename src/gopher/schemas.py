"""核心对象（§5.3）。金额使用 Decimal，时间戳必须带时区并统一存 UTC。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .clock import ensure_aware

SourceType = Literal["exchange", "filing", "official", "vendor", "manual", "news", "derived"]
Reliability = Literal["verified", "provisional", "disputed"]
Action = Literal["BUY", "SELL", "HOLD", "NO_TRADE"]
OrderStatus = Literal["PENDING", "ACTIVE", "EXPIRED", "CANCELLED", "FILLED", "ELIGIBLE_UNVERIFIED"]


class _Aware(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def _utc(cls, v: Any) -> Any:
        if isinstance(v, datetime):
            return ensure_aware(v)
        return v


class Evidence(_Aware):
    evidence_id: str
    source_url: str
    retrieved_at: datetime
    published_at: datetime | None = None
    available_at: datetime  # 决策时首次可得时间
    source_type: SourceType
    reliability: Reliability
    content_hash: str
    title: str = ""
    summary: str = ""
    claim_type: Literal["fact", "rumor", "forecast", "opinion", "data"] = "data"
    tickers: list[str] = Field(default_factory=list)


class Decision(_Aware):
    decision_id: str
    competition_id: str
    decided_at: datetime
    data_cutoff: datetime
    action: Action
    ticker: str | None = None
    thesis: str
    evidence_ids: list[str]
    invalidation_conditions: list[str]
    approval_id: str | None = None

    @model_validator(mode="after")
    def _cutoff(self) -> "Decision":
        if self.data_cutoff > self.decided_at:
            raise ValueError("data_cutoff 不得晚于 decided_at")
        return self


class OrderIntent(_Aware):
    order_id: str
    decision_id: str
    created_at: datetime
    effective_from: datetime
    expires_at: datetime
    ticker: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(gt=0)
    order_type: Literal["LIMIT", "MARKET"]
    limit_price: Decimal | None = None
    approval_id: str | None = None
    status: OrderStatus = "PENDING"

    @model_validator(mode="after")
    def _check(self) -> "OrderIntent":
        if self.order_type == "LIMIT" and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("限价单必须有正的 limit_price")
        if self.effective_from < self.created_at:
            raise ValueError("effective_from 不得早于 created_at（禁止回填）")
        if self.expires_at <= self.effective_from:
            raise ValueError("expires_at 必须晚于 effective_from")
        return self


class FillEvent(_Aware):
    fill_id: str
    order_id: str
    filled_at: datetime
    price: Decimal
    quantity: Decimal
    fee: Decimal = Decimal("0")
    source_evidence_id: str
    execution_model: str


class Approval(_Aware):
    approval_id: str
    meeting_id: str | None = None
    granted_at: datetime
    sides: list[Literal["BUY", "SELL"]]
    budget_usd: Decimal | None = None
    ticker_caps_usd: dict[str, Decimal] = Field(default_factory=dict)
    ticker_qty_caps: dict[str, Decimal] = Field(default_factory=dict)
    valid_from: datetime
    valid_until: datetime | None = None  # None = 有效期未登记 → 不可用于新订单
    execution_rules: str = ""
    note: str = ""


# ---------- Agent 结构化输出 ----------

class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["bull", "base", "bear"]
    probability: float = Field(ge=0, le=1)
    return_pct: float
    rationale: str


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["BUY", "SELL"]
    ticker: str
    budget_usd: float = Field(gt=0, description="BUY 的美元金额；SELL 的近似卖出金额")
    thesis: str
    evidence_ids: list[str]
    scenarios: list[Scenario]
    invalidation_conditions: list[str]
    horizon_days: int = Field(ge=1, le=120)
    confidence: Literal["low", "medium", "high"]


class HoldCounterfactual(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rationale: str
    cash_opportunity_cost_note: str


class DecisionProposal(BaseModel):
    """Strategist 输出（§10 DecisionProposal JSON Schema）。"""
    model_config = ConfigDict(extra="forbid")
    market_state: str
    candidates: list[Candidate]
    hold_counterfactual: HoldCounterfactual
    no_trade_reason: str | None = None
    key_risks: list[str]


class Objection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str
    challenge: str
    evidence_gap: str


class CandidateReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    action: Literal["BUY", "SELL"]
    objections: list[Objection]
    would_flip_if: str
    verdict: Literal["PROCEED", "PROCEED_WITH_CAUTION", "VETO_RECOMMENDED"]


class RedTeamReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviews: list[CandidateReview]
    portfolio_level_concerns: list[str]


class NewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    title: str
    published_at: str | None = Field(None, description="ISO-8601，未知则为 null")
    claim_type: Literal["fact", "rumor", "forecast", "opinion"]
    summary: str
    tickers: list[str]


class NewsDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[NewsItem]

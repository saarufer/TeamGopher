"""Risk Manager（§3.8）：确定性硬约束、会议触发与压力测试。不由 LLM 决定。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .config import Settings
from .ledger import ZERO, LedgerState, q2
from .valuation import Valuation


@dataclass
class RiskAssessment:
    ticker: str
    action: str
    notional: Decimal
    pre_weight: Decimal
    post_weight: Decimal
    pre_cash_weight: Decimal
    post_cash_weight: Decimal
    pre_beta: Decimal
    post_beta: Decimal
    stress_pre: dict[str, Decimal]
    stress_post: dict[str, Decimal]
    meeting_triggers: list[str] = field(default_factory=list)
    hard_blocks: list[str] = field(default_factory=list)
    covering_approval_id: str | None = None
    approval_notes: list[str] = field(default_factory=list)

    @property
    def meeting_required(self) -> bool:
        return bool(self.meeting_triggers)

    @property
    def blocked(self) -> bool:
        return bool(self.hard_blocks)


def portfolio_beta(weights: dict[str, Decimal], settings: Settings) -> Decimal:
    return sum((w * settings.beta(t) for t, w in weights.items()), ZERO)


def stress(nav: Decimal, weights: dict[str, Decimal], settings: Settings) -> dict[str, Decimal]:
    """市场情景 ±2/5/10%：各资产按（假设）beta 映射。"""
    out = {}
    for s in settings.risk.get("stress_scenarios", ["-0.10", "-0.05", "-0.02", "0.02", "0.05", "0.10"]):
        m = Decimal(str(s))
        out[f"{m * 100:+.0f}%"] = q2(sum((nav * w * settings.beta(t) * m for t, w in weights.items()), ZERO))
    return out


def find_covering_approval(state: LedgerState, ticker: str, side: str, notional: Decimal, quantity: Decimal | None,
                           at: datetime) -> tuple[str | None, list[str]]:
    notes = []
    for ap_id, st in state.approvals.items():
        ap = st.approval
        if side not in ap.sides:
            continue
        caps_usd, caps_qty = ap.ticker_caps_usd, ap.ticker_qty_caps
        if (caps_usd or caps_qty) and ticker not in caps_usd and ticker not in caps_qty:
            continue
        if ap.valid_until is None:
            notes.append(f"{ap_id}: 有效期未登记，不能用于新订单")
            continue
        if not (ap.valid_from <= at <= ap.valid_until):
            notes.append(f"{ap_id}: 不在有效期 {ap.valid_from:%Y-%m-%d}–{ap.valid_until:%Y-%m-%d}")
            continue
        if side == "BUY":
            remaining = state.approval_remaining(ap_id)
            if remaining is not None and notional > remaining:
                notes.append(f"{ap_id}: 剩余预算 ${q2(remaining)} < ${q2(notional)}")
                continue
        if ticker in caps_usd:
            used = st.used_by_ticker_usd.get(ticker, ZERO)
            if notional > caps_usd[ticker] - used:
                notes.append(f"{ap_id}: {ticker} 上限剩余 ${q2(caps_usd[ticker] - used)} 不足")
                continue
        if ticker in caps_qty and quantity is not None:
            if quantity > caps_qty[ticker] - st.used_qty.get(ticker, ZERO):
                notes.append(f"{ap_id}: {ticker} 数量上限不足")
                continue
        return ap_id, notes
    return None, notes


def assess(state: LedgerState, val: Valuation, ticker: str, action: str, notional: Decimal,
           settings: Settings, at: datetime, price: Decimal | None = None) -> RiskAssessment:
    nav = val.nav or ZERO
    weights = val.weights()
    pre_w = weights.get(ticker, ZERO)
    pre_cash = val.cash_weight or ZERO
    signed = notional if action == "BUY" else -notional
    blocks: list[str] = []
    triggers: list[str] = []

    if nav <= 0:
        blocks.append("无正式/暂估 NAV，无法评估风险")
        nav = Decimal("1")
    if action == "BUY" and notional > state.cash - state.reserved_cash():
        blocks.append(f"可用现金不足（现金 ${q2(state.cash)}，已占用 ${q2(state.reserved_cash())}）；禁止杠杆")
    if action == "SELL":
        held_val = next((p.value for p in val.positions if p.ticker == ticker), None) or ZERO
        if notional > held_val * Decimal("1.001"):
            blocks.append(f"卖出金额超过持仓市值 ${q2(held_val)}；禁止做空")

    post = dict(weights)
    post[ticker] = pre_w + signed / nav
    post_cash = pre_cash - signed / nav
    single_thr = settings.risk_decimal("single_stock_meeting_weight", "0.15")
    etf_thr = settings.risk_decimal("max_single_etf_weight", "0.35")
    cash_thr = settings.risk_decimal("cash_change_meeting_pp", "0.15")
    is_etf = ticker in settings.etfs
    if not is_etf and post[ticker] >= single_thr:
        triggers.append(f"单一股票 {ticker} 拟配置 {post[ticker]:.1%} ≥ {single_thr:.0%} NAV")
    if is_etf and post[ticker] >= etf_thr:
        triggers.append(f"单只 ETF {ticker} 拟配置 {post[ticker]:.1%} ≥ {etf_thr:.0%} NAV")
    if abs(post_cash - pre_cash) >= cash_thr:
        triggers.append(f"现金比例变化 {abs(post_cash - pre_cash) * 100:.1f}pp ≥ {cash_thr * 100:.0f}pp")

    qty = (notional / price) if price else None
    ap_id, notes = find_covering_approval(state, ticker, action, notional, qty, at)
    if ap_id is None:
        triggers.append("无覆盖该标的/金额/日期的有效授权（新增配置需地鼠会议）")

    return RiskAssessment(
        ticker, action, notional, pre_w, post[ticker], pre_cash, post_cash,
        portfolio_beta(weights, settings), portfolio_beta(post, settings),
        stress(nav, weights, settings), stress(nav, post, settings), triggers, blocks, ap_id, notes,
    )

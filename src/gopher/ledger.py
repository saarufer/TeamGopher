"""账本重放（§8.1）：现金与持仓只能通过按顺序重放不可变事件得到。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from .clock import ensure_aware
from .event_store import Event
from .schemas import Approval, OrderIntent

CENT = Decimal("0.01")
ZERO = Decimal("0")
OPEN_ORDER_STATES = {"PENDING", "ACTIVE", "ELIGIBLE_UNVERIFIED"}


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class OrderState:
    intent: OrderIntent
    status: str = "PENDING"
    history: list[tuple[datetime, str, str]] = field(default_factory=list)
    fill_ids: list[str] = field(default_factory=list)


@dataclass
class ApprovalState:
    approval: Approval
    used_usd: Decimal = ZERO
    used_qty: dict[str, Decimal] = field(default_factory=dict)
    used_by_ticker_usd: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class Checkpoint:
    seq: int
    occurred_at: datetime
    registered: Decimal
    replayed: Decimal

    @property
    def diff(self) -> Decimal:
        return self.replayed - self.registered


@dataclass
class LedgerState:
    initial_cash: Decimal
    cash: Decimal                       # 登记账本现金（含登记现金变化与追加调整）
    cash_derived: Decimal               # 由逐笔 数量×价格 推导的现金
    positions: dict[str, Decimal] = field(default_factory=dict)
    avg_cost: dict[str, Decimal] = field(default_factory=dict)
    realized_pnl: dict[str, Decimal] = field(default_factory=dict)
    fills: list[dict] = field(default_factory=list)
    adjustments: list[dict] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    approvals: dict[str, ApprovalState] = field(default_factory=dict)
    rejected_approvals: dict[str, dict] = field(default_factory=dict)
    meetings: dict[str, dict] = field(default_factory=dict)
    orders: dict[str, OrderState] = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)
    audit_flags: dict[str, dict] = field(default_factory=dict)
    corrections: list[dict] = field(default_factory=list)
    valuations: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    last_seq: int = 0

    @property
    def open_flags(self) -> dict[str, dict]:
        return {k: v for k, v in self.audit_flags.items() if v.get("status") == "OPEN"}

    def held(self) -> dict[str, Decimal]:
        return {t: q for t, q in self.positions.items() if q != 0}

    def reserved_cash(self) -> Decimal:
        """未完成买单占用的现金（按限价，市价单按 reference_price）。"""
        total = ZERO
        for o in self.orders.values():
            if o.status in OPEN_ORDER_STATES and o.intent.side == "BUY":
                px = o.intent.limit_price or ZERO
                total += o.intent.quantity * px
        return total

    def approval_remaining(self, approval_id: str) -> Decimal | None:
        st = self.approvals.get(approval_id)
        if not st or st.approval.budget_usd is None:
            return None
        reserved = sum(
            (o.intent.quantity * (o.intent.limit_price or ZERO)
             for o in self.orders.values()
             if o.intent.approval_id == approval_id and o.status in OPEN_ORDER_STATES and o.intent.side == "BUY"),
            ZERO,
        )
        return st.approval.budget_usd - st.used_usd - reserved


def _d(x) -> Decimal:
    return Decimal(str(x))


def replay(events: list[Event], initial_cash: Decimal, as_of: datetime | None = None) -> LedgerState:
    st = LedgerState(initial_cash=initial_cash, cash=initial_cash, cash_derived=initial_cash)
    cutoff = ensure_aware(as_of) if as_of else None
    for ev in sorted(events, key=lambda e: e.seq):
        if cutoff and ev.occurred_at > cutoff:
            continue
        st.last_seq = ev.seq
        p = ev.payload
        t = ev.type
        if t == "FillRecorded":
            _apply_fill(st, ev)
        elif t == "CashAdjustmentRecorded":
            amt = _d(p["amount"])
            st.cash += amt
            st.cash_derived += amt
            st.adjustments.append({**p, "seq": ev.seq, "occurred_at": ev.occurred_at})
        elif t == "CashCheckpointRecorded":
            st.checkpoints.append(Checkpoint(ev.seq, ev.occurred_at, _d(p["registered_cash"]), st.cash))
        elif t == "ApprovalGranted":
            ap = Approval.model_validate({k: v for k, v in p.items() if k in Approval.model_fields})
            st.approvals[ap.approval_id] = ApprovalState(ap)
            if ap.meeting_id and ap.meeting_id in st.meetings:
                st.meetings[ap.meeting_id]["status"] = "APPROVED"
        elif t == "ApprovalRejected":
            st.rejected_approvals[p["meeting_id"]] = p
            if p["meeting_id"] in st.meetings:
                st.meetings[p["meeting_id"]]["status"] = "REJECTED"
        elif t == "MeetingCreated":
            st.meetings[p["meeting_id"]] = {**p, "status": "OPEN", "created_at": ev.occurred_at}
        elif t == "DecisionRecorded":
            st.decisions.append({**p, "seq": ev.seq})
        elif t == "OrderProposed":
            intent = OrderIntent.model_validate({k: v for k, v in p.items() if k in OrderIntent.model_fields})
            st.orders[intent.order_id] = OrderState(intent, "PENDING", [(ev.occurred_at, "PENDING", "")])
        elif t in ("OrderActivated", "OrderEligibleUnverified", "OrderExpired", "OrderCancelled"):
            new = {"OrderActivated": "ACTIVE", "OrderEligibleUnverified": "ELIGIBLE_UNVERIFIED",
                   "OrderExpired": "EXPIRED", "OrderCancelled": "CANCELLED"}[t]
            o = st.orders.get(p["order_id"])
            if not o:
                st.issues.append(f"seq {ev.seq}: {t} 指向未知订单 {p['order_id']}")
                continue
            if o.status in ("FILLED", "EXPIRED", "CANCELLED"):
                st.issues.append(f"seq {ev.seq}: 订单 {o.intent.order_id} 已终结({o.status})，不得变为 {new}")
                continue
            o.status = new
            o.history.append((ev.occurred_at, new, p.get("reason", "")))
        elif t == "AuditFlagRaised":
            st.audit_flags[p["flag_id"]] = {**p, "status": "OPEN", "raised_at": ev.occurred_at}
        elif t == "AuditResolved":
            if p["flag_id"] in st.audit_flags:
                st.audit_flags[p["flag_id"]]["status"] = "RESOLVED"
                st.audit_flags[p["flag_id"]]["resolution"] = p.get("resolution", "")
        elif t == "CorrectionAppended":
            st.corrections.append({**p, "seq": ev.seq})
        elif t == "ValuationPublished":
            st.valuations.append({**p, "seq": ev.seq})
    return st


def _apply_fill(st: LedgerState, ev: Event) -> None:
    p = ev.payload
    ticker, side = p["ticker"], p["side"]
    qty, price = _d(p["quantity"]), _d(p["price"])
    fee = _d(p.get("fee", "0"))
    signed = qty if side == "BUY" else -qty
    pos = st.positions.get(ticker, ZERO)
    if side == "SELL" and qty > pos:
        st.issues.append(f"seq {ev.seq}: 卖出 {ticker} {qty} 超过持仓 {pos}（禁止做空）")
    notional = qty * price
    derived_delta = -signed * price - fee
    registered_delta = _d(p["registered_cash_delta"]) if p.get("registered_cash_delta") is not None else q2(derived_delta)

    # 平均成本与已实现盈亏
    if side == "BUY":
        cost = st.avg_cost.get(ticker, ZERO) * pos + notional + fee
        st.avg_cost[ticker] = cost / (pos + qty) if pos + qty else ZERO
    else:
        st.realized_pnl[ticker] = st.realized_pnl.get(ticker, ZERO) + (price - st.avg_cost.get(ticker, ZERO)) * qty - fee

    st.positions[ticker] = pos + signed
    st.cash += registered_delta
    st.cash_derived += derived_delta
    if st.cash < 0:
        st.issues.append(f"seq {ev.seq}: 登记现金为负 {st.cash}（禁止杠杆）")

    order_id = p.get("order_id")
    if order_id and order_id in st.orders:
        o = st.orders[order_id]
        o.status = "FILLED"
        o.fill_ids.append(p["fill_id"])
        o.history.append((ev.occurred_at, "FILLED", p.get("execution_model", "")))

    ap_id = p.get("approval_id")
    if ap_id:
        ap = st.approvals.get(ap_id)
        if not ap:
            st.issues.append(f"seq {ev.seq}: 成交引用了未登记的审批 {ap_id}")
        else:
            if side == "BUY":
                ap.used_usd += -registered_delta
            ap.used_by_ticker_usd[ticker] = ap.used_by_ticker_usd.get(ticker, ZERO) + abs(registered_delta)
            ap.used_qty[ticker] = ap.used_qty.get(ticker, ZERO) + qty

    st.fills.append({**p, "seq": ev.seq, "occurred_at": ev.occurred_at,
                     "derived_cash_delta": derived_delta, "registered_cash_delta": registered_delta})

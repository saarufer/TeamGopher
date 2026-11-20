"""Auditor（§3.11）：独立只读，重算现金/仓位/NAV，比对来源与权限。审计失败时阻止正式净值。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..event_store import Event, EventStore
from ..ledger import ZERO, LedgerState, q2


@dataclass
class AuditReport:
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    reconciliation: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.blocking


def audit(store: EventStore, events: list[Event], state: LedgerState) -> AuditReport:
    rep = AuditReport()
    rep.blocking += [f"哈希链: {x}" for x in store.verify(events)]
    rep.blocking += [f"账本: {x}" for x in state.issues]

    for cp in state.checkpoints:
        if cp.diff != 0:
            rep.blocking.append(f"现金检查点 seq {cp.seq}（{cp.occurred_at:%Y-%m-%d}）重放 {cp.replayed} ≠ 登记 {cp.registered}")

    diff = state.cash - state.cash_derived
    rep.reconciliation = {
        "registered_cash": q2(state.cash),
        "derived_cash": state.cash_derived,
        "difference": diff,
        "adjustments": [{"amount": a["amount"], "reason_code": a.get("reason_code"), "reason": a.get("reason")}
                        for a in state.adjustments],
        "per_fill": [{"fill_id": f["fill_id"], "ticker": f["ticker"], "side": f["side"],
                      "registered": f["registered_cash_delta"], "derived": f["derived_cash_delta"],
                      "diff": Decimal(str(f["registered_cash_delta"])) - f["derived_cash_delta"]}
                     for f in state.fills
                     if Decimal(str(f["registered_cash_delta"])) != f["derived_cash_delta"]],
    }
    if abs(diff) >= Decimal("1.00"):
        rep.blocking.append(f"登记现金与逐笔推导现金相差 ${q2(diff)}，超过 $1 容差")
    elif diff != 0:
        rep.info.append(f"登记现金与逐笔推导现金相差 ${diff:.6f}（小数份额截断/舍入；见 reconciliation）")
    for a in state.adjustments:
        rep.info.append(f"追加调整 {a.get('reason_code')}: {a['amount']} — {a.get('reason')}")

    for f in state.fills:
        if f.get("fill_confidence") == "provisional":
            rep.warnings.append(f"{f['occurred_at']:%m/%d} {f['ticker']} 为推定成交（{f.get('evidence_grade')} 级，provisional）")
        if not f.get("approval_id"):
            if f.get("historical"):
                rep.info.append(f"{f['occurred_at']:%m/%d} {f['ticker']} 历史成交无结构化审批记录（初始建仓）")
            else:
                rep.blocking.append(f"{f['fill_id']} 缺少授权")
        elif f["approval_id"] in state.approvals:
            ap = state.approvals[f["approval_id"]].approval
            if ap.valid_until and not (ap.valid_from <= f["occurred_at"] <= ap.valid_until):
                rep.warnings.append(f"{f['fill_id']} 成交时间不在授权有效期内")

    for ap_id, st in state.approvals.items():
        ap = st.approval
        if ap.budget_usd is not None and st.used_usd > ap.budget_usd:
            rep.blocking.append(f"授权 {ap_id} 超额使用：${q2(st.used_usd)} > ${ap.budget_usd}")
        for t, cap in ap.ticker_caps_usd.items():
            if st.used_by_ticker_usd.get(t, ZERO) > cap:
                rep.blocking.append(f"授权 {ap_id} 的 {t} 超过上限")

    for fid, fl in state.open_flags.items():
        sev = fl.get("severity", "warning")
        msg = f"[{fl.get('code')}] {fl.get('ticker', '')} {fl.get('detail', '')}".strip()
        (rep.blocking if sev == "blocking" else rep.warnings if sev == "warning" else rep.info).append(msg)

    for t, q in state.positions.items():
        if q < 0:
            rep.blocking.append(f"{t} 持仓为负")
    return rep

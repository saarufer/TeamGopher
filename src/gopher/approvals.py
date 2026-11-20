"""地鼠会议与用户授权（§4）。批准必须是结构化记录：额度、标的、有效期、执行规则。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from .clock import ensure_aware, now_utc
from .event_store import EventStore
from .ledger import replay


class ApprovalError(ValueError):
    pass


def create_meeting(store: EventStore, *, candidates: list[dict], triggers: list[str], summary: str,
                   at: datetime | None = None, competition_id: str | None = None, extra: dict | None = None) -> str:
    meeting_id = f"meeting_{(at or now_utc()):%Y%m%d}_{uuid.uuid4().hex[:6]}"
    store.append("MeetingCreated", {"meeting_id": meeting_id, "candidates": candidates, "triggers": triggers,
                                    "summary": summary, **(extra or {})},
                 occurred_at=at, source="orchestrator", competition_id=competition_id)
    return meeting_id


def approve(store: EventStore, initial_cash: Decimal, meeting_id: str, *, tickers_caps_usd: dict[str, Decimal],
            sides: list[str], valid_until: datetime, execution_rules: str, budget_usd: Decimal | None = None,
            candidate_index: int | None = None, valid_from: datetime | None = None, at: datetime | None = None,
            ticker_qty_caps: dict[str, Decimal] | None = None, competition_id: str | None = None) -> str:
    at = ensure_aware(at) if at else now_utc()
    st = replay(store.load(), initial_cash)
    m = st.meetings.get(meeting_id)
    if not m:
        raise ApprovalError(f"会议不存在: {meeting_id}")
    if m["status"] != "OPEN":
        raise ApprovalError(f"会议状态为 {m['status']}，不能再次批准")
    cands = m.get("candidates", [])
    if len(cands) > 1 and candidate_index is None:
        raise ApprovalError("会议有多个方案；“支持”不构成唯一批准，请用 --candidate 指定方案编号")
    if candidate_index is not None and not (0 <= candidate_index < len(cands)):
        raise ApprovalError(f"方案编号超出范围 0..{len(cands) - 1}")
    if not tickers_caps_usd and not ticker_qty_caps:
        raise ApprovalError("必须明确标的与额度")
    if valid_until is None or ensure_aware(valid_until) <= at:
        raise ApprovalError("必须给出晚于当前时间的有效期 valid_until")
    caps = {k.upper(): Decimal(str(v)) for k, v in tickers_caps_usd.items()}
    if any(v <= 0 for v in caps.values()):
        raise ApprovalError("额度必须为正")
    budget = Decimal(str(budget_usd)) if budget_usd is not None else (sum(caps.values()) if caps else None)
    approval_id = f"appr_{at:%Y%m%d}_{uuid.uuid4().hex[:6]}"
    store.append("ApprovalGranted", {
        "approval_id": approval_id, "meeting_id": meeting_id, "granted_at": at, "sides": sides,
        "budget_usd": budget, "ticker_caps_usd": caps,
        "ticker_qty_caps": {k.upper(): Decimal(str(v)) for k, v in (ticker_qty_caps or {}).items()},
        "valid_from": ensure_aware(valid_from) if valid_from else at, "valid_until": ensure_aware(valid_until),
        "execution_rules": execution_rules,
        "note": f"批准方案 #{candidate_index}" if candidate_index is not None else "",
    }, occurred_at=at, source="user", competition_id=competition_id)
    return approval_id


def reject(store: EventStore, initial_cash: Decimal, meeting_id: str, reason: str, at: datetime | None = None,
           competition_id: str | None = None) -> None:
    st = replay(store.load(), initial_cash)
    m = st.meetings.get(meeting_id)
    if not m:
        raise ApprovalError(f"会议不存在: {meeting_id}")
    if m["status"] != "OPEN":
        raise ApprovalError(f"会议状态为 {m['status']}")
    store.append("ApprovalRejected", {"meeting_id": meeting_id, "reason": reason},
                 occurred_at=at, source="user", competition_id=competition_id)

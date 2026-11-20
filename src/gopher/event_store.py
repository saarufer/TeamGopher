"""不可变事件日志（§5.4）：仅追加 JSONL + SHA-256 哈希链。

哈希链只提供篡改可检测性，不等于外部公证；请定期将快照备份到独立位置。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict

from .clock import ensure_aware, now_utc, parse_dt

GENESIS_HASH = "0" * 64

EVENT_TYPES = {
    "CompetitionCreated",
    "MeetingCreated",
    "DecisionRecorded",
    "OrderProposed",
    "ApprovalGranted",
    "ApprovalRejected",
    "OrderActivated",
    "OrderEligibleUnverified",
    "OrderExpired",
    "OrderCancelled",
    "FillRecorded",
    "CashAdjustmentRecorded",
    "CashCheckpointRecorded",
    "CorporateActionRecorded",
    "CorrectionAppended",
    "ValuationPublished",
    "AuditFlagRaised",
    "AuditResolved",
}


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return ensure_aware(obj).isoformat()
    if isinstance(obj, BaseModel):
        return to_jsonable(obj.model_dump())
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    return obj


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int
    event_id: str
    type: str
    occurred_at: datetime
    recorded_at: datetime
    source: str
    competition_id: str | None
    payload: dict[str, Any]
    prev_hash: str
    event_hash: str

    def body(self) -> dict:
        d = to_jsonable(self.model_dump())
        d.pop("event_hash")
        return d

    def compute_hash(self) -> str:
        return sha256(canonical(self.body()))


class TamperError(RuntimeError):
    pass


class EventStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[Event]:
        if not self.path.exists():
            return []
        events = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(Event.model_validate_json(line))
        return events

    def verify(self, events: list[Event] | None = None) -> list[str]:
        """返回全部完整性问题；空列表表示哈希链完整。"""
        issues: list[str] = []
        try:
            events = self.load() if events is None else events
        except Exception as e:  # 被篡改为非法 JSON 等
            return [f"日志无法解析: {e}"]
        prev = GENESIS_HASH
        for i, ev in enumerate(events, start=1):
            if ev.seq != i:
                issues.append(f"seq 不连续: 期望 {i}, 实际 {ev.seq}")
            if ev.prev_hash != prev:
                issues.append(f"seq {ev.seq} prev_hash 断链")
            if ev.compute_hash() != ev.event_hash:
                issues.append(f"seq {ev.seq} ({ev.type}) 事件哈希不匹配：内容被修改")
            if ev.type not in EVENT_TYPES:
                issues.append(f"seq {ev.seq} 未知事件类型 {ev.type}")
            prev = ev.event_hash
        return issues

    def append(
        self,
        type: str,
        payload: dict[str, Any],
        *,
        occurred_at: datetime | str | None = None,
        source: str = "system",
        competition_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> Event:
        return self.append_many([(type, payload, occurred_at, source, competition_id)], recorded_at=recorded_at)[0]

    def append_many(self, items: Iterable[tuple], *, recorded_at: datetime | None = None) -> list[Event]:
        issues = self.verify()
        if issues:
            raise TamperError("拒绝在不完整的日志上追加: " + "; ".join(issues))
        existing = self.load()
        prev = existing[-1].event_hash if existing else GENESIS_HASH
        seq = len(existing)
        rec = ensure_aware(recorded_at) if recorded_at else now_utc()
        out: list[Event] = []
        for type_, payload, occurred_at, source, competition_id in items:
            if type_ not in EVENT_TYPES:
                raise ValueError(f"未知事件类型: {type_}")
            seq += 1
            occ = parse_dt(occurred_at) if occurred_at is not None else rec
            draft = {
                "seq": seq,
                "event_id": f"evt_{uuid.uuid4().hex[:16]}",
                "type": type_,
                "occurred_at": occ,
                "recorded_at": rec,
                "source": source,
                "competition_id": competition_id,
                "payload": to_jsonable(payload),
                "prev_hash": prev,
                "event_hash": "",
            }
            ev = Event.model_validate(draft)
            ev = ev.model_copy(update={"event_hash": ev.compute_hash()})
            out.append(ev)
            prev = ev.event_hash
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for ev in out:
                f.write(canonical(to_jsonable(ev.model_dump())) + "\n")
        return out

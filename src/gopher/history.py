"""历史交易迁移（§6）：登记模拟账本 → 不可变事件。不得用新行情覆盖原登记成交价。"""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import yaml

from .clock import ET, parse_dt
from .config import Settings
from .event_store import EventStore

ORIGINAL_COMPETITION = "gopher_challenge_2026_10"


def _ts(date_et: str, time_et: str) -> str:
    return f"{date_et}T{time_et}:00"


def build_history_items(settings: Settings) -> list[tuple]:
    imports = settings.data_dir / "imports"
    comp = settings.competition
    items: list[tuple] = []
    items.append((
        "CompetitionCreated",
        {
            "competitions": comp.get("competitions", []),
            "initial_cash": comp.get("initial_cash"),
            "benchmark": comp.get("benchmark"),
            "rules": comp.get("rules"),
            "note": "原比赛截止时间待从原始规则/日志独立确认；延长赛独立计量",
        },
        "2026-10-03T00:00:00",
        "history_import",
        ORIGINAL_COMPETITION,
    ))

    approvals = yaml.safe_load((imports / "approvals_2026.yaml").read_text(encoding="utf-8")) or []
    by_time: list[tuple] = []
    for a in approvals:
        if a.get("meeting_id"):
            by_time.append((parse_dt(a["granted_at"]), (
                "MeetingCreated",
                {"meeting_id": a["meeting_id"], "historical": True, "summary": a.get("execution_rules", ""),
                 "triggers": ["历史重大变化协商原则"], "candidates": []},
                a["granted_at"], "history_import", ORIGINAL_COMPETITION)))
        by_time.append((parse_dt(a["granted_at"]), (
            "ApprovalGranted", {**a, "granted_by": "user", "historical": True},
            a["granted_at"], "history_import", ORIGINAL_COMPETITION)))

    with (imports / "history_2026.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = _ts(row["date_et"], row["time_et"])
            flags = [x for x in (row.get("flags") or "").split(";") if x]
            kind = row["kind"]
            if kind == "FILL":
                payload = {
                    "fill_id": f"hist_fill_{int(row['seq']):03d}",
                    "order_id": None,
                    "ticker": row["ticker"],
                    "side": row["side"],
                    "quantity": row["quantity"],
                    "price": row["price"],
                    "fee": "0",
                    "registered_cash_delta": row["registered_cash_delta"],
                    "approval_id": row["approval_id"] or None,
                    "execution_model": row["execution_model"],
                    "fill_confidence": row["fill_confidence"],
                    "evidence_grade": "C" if row["fill_confidence"] == "provisional" else "REGISTERED",
                    "source_evidence_id": "hist_conversation_log",
                    "flags": flags,
                    "note": row["note"],
                    "historical": True,
                }
                by_time.append((parse_dt(ts), ("FillRecorded", payload, ts, "history_import", ORIGINAL_COMPETITION)))
                for flag in flags:
                    by_time.append((parse_dt(ts), (
                        "AuditFlagRaised",
                        {"flag_id": f"{flag}:{payload['fill_id']}", "code": flag, "fill_id": payload["fill_id"],
                         "ticker": row["ticker"], "severity": "warning", "detail": row["note"]},
                        ts, "history_import", ORIGINAL_COMPETITION)))
            elif kind == "CASH_ADJUSTMENT":
                by_time.append((parse_dt(ts), (
                    "CashAdjustmentRecorded",
                    {"adjustment_id": f"hist_adj_{int(row['seq']):03d}", "amount": row["registered_cash_delta"],
                     "reason_code": flags[0] if flags else "ADJUSTMENT", "reason": row["note"],
                     "source": "历史现金账本对账"},
                    ts, "history_import", ORIGINAL_COMPETITION)))
                by_time.append((parse_dt(ts), (
                    "AuditFlagRaised",
                    {"flag_id": f"ROUNDING_RECONCILIATION:hist_adj_{int(row['seq']):03d}",
                     "code": "ROUNDING_RECONCILIATION", "severity": "info",
                     "detail": row["note"], "amount": row["registered_cash_delta"]},
                    ts, "history_import", ORIGINAL_COMPETITION)))
            elif kind == "CHECKPOINT":
                by_time.append((parse_dt(ts), (
                    "CashCheckpointRecorded",
                    {"registered_cash": row["registered_cash_delta"], "note": row["note"]},
                    ts, "history_import", ORIGINAL_COMPETITION)))
    # 稳定排序：同一时刻保持文件顺序（审批先于成交）
    by_time.sort(key=lambda x: x[0])
    items.extend(i for _, i in by_time)
    return items


def import_history(settings: Settings, store: EventStore | None = None) -> int:
    store = store or EventStore(settings.events_path)
    if store.load():
        raise RuntimeError(f"事件日志已存在（{store.path}），历史迁移只能执行一次；不允许覆盖。")
    return len(store.append_many(build_history_items(settings)))


REGISTERED_SNAPSHOT_20261119 = {
    "positions": {
        "SPY": Decimal("12.850990"), "QQQ": Decimal("5.259560"), "NVDA": Decimal("14.456835"),
        "XLE": Decimal("46.153846"), "XLI": Decimal("23"), "XLF": Decimal("43"), "XLV": Decimal("37"),
    },
    "cash": Decimal("66010.76"),
    # 历史报告快照，尚待独立行情源与逐笔流水审计
    "reported_nav": Decimal("96190.98"),
    "reported_spy_nav": Decimal("93013.62"),
    "reported_active_usd": Decimal("3177.36"),
}

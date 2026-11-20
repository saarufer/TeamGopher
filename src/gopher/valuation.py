"""组合估值、SPY 基准与绩效（§8）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from .clock import last_completed_session, market_close
from .config import Settings
from .event_store import Event
from .ledger import ZERO, LedgerState, q2, replay
from .market_data import MarketStore, PointInTimeView


@dataclass
class PositionValue:
    ticker: str
    quantity: Decimal
    price: Decimal | None
    value: Decimal | None
    weight: Decimal | None
    source: str | None
    price_date: date | None
    conflict: bool
    stale: bool
    candidates: dict[str, Decimal] = field(default_factory=dict)
    avg_cost: Decimal | None = None
    unrealized_pnl: Decimal | None = None


@dataclass
class Valuation:
    as_of: datetime
    session_date: date
    cash: Decimal
    positions: list[PositionValue]
    nav: Decimal | None
    cash_weight: Decimal | None
    official: bool
    reasons: list[str]
    benchmark_close: Decimal | None = None
    benchmark_nav: Decimal | None = None

    def weights(self) -> dict[str, Decimal]:
        return {p.ticker: p.weight for p in self.positions if p.weight is not None}


def value_portfolio(state: LedgerState, view: PointInTimeView, settings: Settings,
                    session_date: date | None = None, audit_ok: bool = True) -> Valuation:
    delay = int(settings.data_sources.get("daily_bar_available_delay_minutes", 15))
    sd = session_date or last_completed_session(view.as_of, delay)
    thr = settings.risk_decimal("price_conflict_threshold", "0.005")
    reasons: list[str] = []
    rows: list[PositionValue] = []
    total = state.cash
    missing = False
    for t, qty in sorted(state.held().items()):
        q = view.close(t, sd, thr)
        stale = False
        if q is None:
            q = view.latest_close(t, thr)
            stale = q is not None
        if q is None:
            missing = True
            reasons.append(f"{t} 无可得收盘价")
            rows.append(PositionValue(t, qty, None, None, None, None, None, False, False))
            continue
        if stale:
            reasons.append(f"{t} 使用 {q.session_date} 的过期收盘价")
        if q.conflict:
            reasons.append(f"{t} 收盘价来源冲突 {dict((k, str(v)) for k, v in q.candidates.items())}")
        val = qty * q.price
        total += val
        avg = state.avg_cost.get(t)
        rows.append(PositionValue(t, qty, q.price, val, None, q.source, q.session_date, q.conflict, stale,
                                  q.candidates, avg, (q.price - avg) * qty if avg is not None else None))
    nav = None if missing else total
    if nav:
        for r in rows:
            r.weight = r.value / nav if r.value is not None else None
    adj = view.adjustment_types([r.ticker for r in rows] + [settings.benchmark_ticker])
    if len(adj) > 1:
        reasons.append(f"价格口径混用: {sorted(adj)}")
    if not audit_ok:
        reasons.append("审计未通过")
    bq = view.close(settings.benchmark_ticker, sd, thr)
    bnav = None
    if bq:
        bnav = settings.initial_cash / settings.benchmark_base_close * bq.price
    else:
        reasons.append(f"{settings.benchmark_ticker} {sd} 收盘价不可得")
    official = not reasons and nav is not None
    return Valuation(view.as_of, sd, state.cash, rows, nav, (state.cash / nav) if nav else None,
                     official, reasons, bq.price if bq else None, bnav)


@dataclass
class Performance:
    nav: Decimal
    benchmark_nav: Decimal
    absolute_return: Decimal
    benchmark_return: Decimal
    active_return_pp: Decimal
    active_pnl_usd: Decimal


def performance(val: Valuation, settings: Settings) -> Performance | None:
    if val.nav is None or val.benchmark_close is None:
        return None
    init = settings.initial_cash
    abs_r = val.nav / init - 1
    bench_r = val.benchmark_close / settings.benchmark_base_close - 1
    return Performance(val.nav, val.benchmark_nav, abs_r, bench_r, 100 * (abs_r - bench_r),
                       val.nav - val.benchmark_nav)


@dataclass
class SeriesPoint:
    d: date
    nav: Decimal
    benchmark_nav: Decimal
    cash_weight: Decimal


def nav_series(events: list[Event], store: MarketStore, settings: Settings, as_of: datetime) -> list[SeriesPoint]:
    """逐交易日收盘重放账本并估值（只用当时可得的收盘价）。"""
    view = store.view(as_of)
    base = date.fromisoformat(str(settings.competition.get("benchmark", {}).get("base_date", "2026-10-02")))
    dates = [d for d in view.daily_dates(settings.benchmark_ticker) if d >= base]
    out: list[SeriesPoint] = []
    for d in dates:
        cutoff = market_close(d)
        st = replay(events, settings.initial_cash, as_of=cutoff)
        v = value_portfolio(st, view, settings, d)
        if v.nav is None or v.benchmark_nav is None:
            continue
        out.append(SeriesPoint(d, v.nav, v.benchmark_nav, v.cash_weight or ZERO))
    return out


def max_drawdown(values: list[Decimal]) -> Decimal:
    peak, mdd = None, ZERO
    for v in values:
        peak = v if peak is None or v > peak else peak
        if peak:
            mdd = min(mdd, v / peak - 1)
    return mdd


def attribution(series: list[SeriesPoint], state: LedgerState, val: Valuation) -> dict:
    """简化归因：逐资产盈亏 + 现金效应估计。反事实只是风险比较，不计入已实现利润（§8.3）。"""
    per_asset = {}
    for t in sorted(set(state.realized_pnl) | {p.ticker for p in val.positions}):
        realized = state.realized_pnl.get(t, ZERO)
        unreal = next((p.unrealized_pnl for p in val.positions if p.ticker == t), None) or ZERO
        per_asset[t] = {"realized": q2(realized), "unrealized": q2(unreal), "total": q2(realized + unreal)}
    cash_effect = ZERO
    for prev, cur in zip(series, series[1:]):
        r_b = cur.benchmark_nav / prev.benchmark_nav - 1
        # 现金权重 w 的部分没有承担基准收益：相对基准的贡献约为 -w * r_b（以当日 NAV 计）
        cash_effect += -prev.cash_weight * r_b * prev.nav
    return {"per_asset": per_asset, "cash_effect_usd_est": q2(cash_effect),
            "note": "现金效应为近似估计；逐资产盈亏按平均成本法；均为登记成交口径"}

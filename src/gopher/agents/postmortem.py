"""复盘校准 Agent（新增，对应 §15 方向 C / D 与 §8.3）。

1. 给到期的决策和事件预测打分：方向命中率、Brier 分数、预期与实际收益误差。
2. HOLD 的机会成本：当时的现金如果放在 SPY，horizon 期内会多赚 / 少亏多少。
3. 反事实：满仓 SPY、同等现金比例的 SPY（敞口对齐）、等权行业、逐个“当天不交易”的分支。
4. 敞口拆分：超额 = 敞口效应（低 beta / 高现金）+ 敞口之外（选股、择时、执行）。

全部是确定性计算；反事实只用于比较，不计入账本，也不能说成已经赚到的钱。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from ..clock import ET, last_completed_session, market_close, parse_dt
from ..config import Settings
from ..event_store import Event
from ..ledger import ZERO, replay
from ..market_data import PointInTimeView
from ..valuation import SeriesPoint

SECTOR_BASKET = ["XLK", "XLV", "XLF", "XLI", "XLE", "XLP", "XLU"]


@dataclass
class Scored:
    kind: str                 # decision | forecast | hold
    ref: str
    ticker: str
    action: str
    start: date
    end: date | None
    expected_pct: float | None
    p_up: float | None
    realized_pct: float | None
    brier: float | None
    hit: bool | None
    note: str = ""


@dataclass
class Postmortem:
    as_of: datetime
    scored: list[Scored] = field(default_factory=list)
    pending: int = 0
    counterfactuals: list[dict] = field(default_factory=list)
    decomposition: dict | None = None
    lessons: list[str] = field(default_factory=list)

    def summary(self, kind: str) -> dict:
        xs = [s for s in self.scored if s.kind == kind and s.realized_pct is not None]
        br = [s.brier for s in xs if s.brier is not None]
        hits = [s.hit for s in xs if s.hit is not None]
        err = [abs(s.realized_pct - s.expected_pct) for s in xs if s.expected_pct is not None]
        return {"n": len(xs), "hit_rate": (sum(hits) / len(hits)) if hits else None,
                "brier": (sum(br) / len(br)) if br else None, "mean_abs_err_pct": (sum(err) / len(err)) if err else None}

    def context(self) -> dict:
        """给 Strategist 的精简复盘。"""
        return {"decision_scores": self.summary("decision"), "forecast_scores": self.summary("forecast"),
                "hold_opportunity": self.summary("hold"), "pending": self.pending,
                "exposure_decomposition": self.decomposition, "lessons": self.lessons,
                "brier_baseline_note": "Brier 0.25 = 永远报 50% 的基线；低于 0.25 才说明概率有信息量"}


def _p_up(scen: list[dict]) -> float:
    return sum(s["probability"] for s in scen if s["return_pct"] > 0) + 0.5 * sum(
        s["probability"] for s in scen if s["return_pct"] == 0)


def _window(view: PointInTimeView, ticker: str, start: date, horizon: int) -> tuple[date, Decimal, date | None, Decimal | None]:
    dates = view.daily_dates(ticker)
    prior = [d for d in dates if d <= start]
    if not prior:
        return start, None, None, None
    s = prior[-1]
    i = dates.index(s)
    if i + horizon >= len(dates):
        return s, view.close(ticker, s).price, None, None
    e = dates[i + horizon]
    return s, view.close(ticker, s).price, e, view.close(ticker, e).price


def _score(kind, ref, ticker, action, scen, start, horizon, view) -> Scored | None:
    s, ps, e, pe = _window(view, ticker, start, horizon)
    if ps is None:
        return None
    exp = sum(x["probability"] * x["return_pct"] for x in scen) if scen else None
    pu = _p_up(scen) if scen else None
    if pe is None:
        return Scored(kind, ref, ticker, action, s, None, exp, pu, None, None, None, "未到期")
    r = float(pe / ps - 1) * 100
    y = 1.0 if r > 0 else 0.0
    brier = (pu - y) ** 2 if pu is not None else None
    hit = None if not exp else (exp > 0) == (r > 0)
    return Scored(kind, ref, ticker, action, s, e, exp, pu, r, brier, hit)


def run(settings: Settings, events: list[Event], view: PointInTimeView, as_of: datetime,
        series: list[SeriesPoint]) -> Postmortem:
    pm = Postmortem(as_of)
    # 决策/预测按其 data_cutoff 判断是否“当时已存在”，而不是按写入时间（回放过去日期时两者不同）
    st = replay(events, settings.initial_cash)
    st.decisions = [d for d in st.decisions if parse_dt(d["data_cutoff"]) <= as_of]
    st.forecasts = [f for f in st.forecasts if parse_dt(f["data_cutoff"]) <= as_of]
    delay = int(settings.data_sources.get("daily_bar_available_delay_minutes", 15))
    default_h = int(settings.agents.get("horizon_days", 10))
    bench = settings.benchmark_ticker

    for d in st.decisions:
        cutoff = parse_dt(d["data_cutoff"])
        start = last_completed_session(cutoff, delay)
        h = int(d.get("horizon_days") or default_h)
        if d.get("ticker"):
            sc = _score("decision", d["decision_id"], d["ticker"], d["action"], d.get("scenarios") or [], start, h, view)
        else:
            sc = _score("hold", d["decision_id"], bench, d["action"], [], start, h, view)
            if sc and sc.realized_pct is not None:
                cash = replay(events, settings.initial_cash, as_of=cutoff).cash
                usd = float(cash) * sc.realized_pct / 100
                sc.note = (f"现金 ${float(cash):,.0f} 若放在 SPY：{'少赚' if usd > 0 else '少亏'} ${abs(usd):,.0f}")
        if sc:
            pm.scored.append(sc)

    for f in st.forecasts:
        ev_day = parse_dt(f["scheduled_at"]).astimezone(ET).date()
        start = last_completed_session(parse_dt(f["data_cutoff"]), delay)
        s, ps, _, _ = _window(view, f["ticker"], start, 0)
        dates = [x for x in view.daily_dates(f["ticker"]) if x >= ev_day]
        h = int(f["horizon_days"])
        if ps is None:
            continue
        if len(dates) <= h - 1 or not dates:
            pm.scored.append(Scored("forecast", f["forecast_id"], f["ticker"], f["event_name"], s, None,
                                    f["expected_return_pct"], f["p_up"], None, None, None, "未到期"))
            continue
        e = dates[h - 1]
        r = float(view.close(f["ticker"], e).price / ps - 1) * 100
        exp = float(f["expected_return_pct"])
        pm.scored.append(Scored("forecast", f["forecast_id"], f["ticker"], f["event_name"], s, e, exp, f["p_up"], r,
                                (f["p_up"] - (1.0 if r > 0 else 0.0)) ** 2, None if not exp else (exp > 0) == (r > 0)))
    pm.pending = sum(1 for s in pm.scored if s.realized_pct is None)

    _counterfactuals(pm, settings, events, view, series)
    _lessons(pm)
    return pm


def _replay_skip(settings: Settings, events: list[Event], skip_day: date, view: PointInTimeView, d_end: date) -> tuple[Decimal | None, list[str]]:
    """去掉 skip_day 当天的全部成交；之后因此无法执行的卖出也一并跳过。"""
    cash, pos, skipped = settings.initial_cash, defaultdict(lambda: ZERO), []
    for ev in sorted(events, key=lambda e: e.seq):
        if ev.occurred_at > market_close(d_end):
            continue
        p = ev.payload
        if ev.type == "CashAdjustmentRecorded":
            cash += Decimal(p["amount"])
        elif ev.type == "FillRecorded":
            day = ev.occurred_at.astimezone(ET).date()
            qty = Decimal(p["quantity"])
            if day == skip_day:
                skipped.append(f"{p['side']} {p['ticker']}")
                continue
            if p["side"] == "SELL" and qty > pos[p["ticker"]] + Decimal("1e-9"):
                skipped.append(f"{day:%m/%d} {p['side']} {p['ticker']}（连带）")
                continue
            pos[p["ticker"]] += qty if p["side"] == "BUY" else -qty
            cash += Decimal(p["registered_cash_delta"])
    total = cash
    for t, q in pos.items():
        if q:
            c = view.close(t, d_end)
            if not c:
                return None, skipped
            total += q * c.price
    return total, skipped


def _counterfactuals(pm: Postmortem, settings: Settings, events: list[Event], view: PointInTimeView,
                     series: list[SeriesPoint]) -> None:
    if len(series) < 2:
        return
    d0, d_end = series[0].d, series[-1].d
    actual, spy = float(series[-1].nav), float(series[-1].benchmark_nav)
    init = float(settings.initial_cash)
    rows = [{"name": "实际组合", "nav": actual, "note": "登记成交口径"},
            {"name": "满仓 SPY（基准）", "nav": spy, "note": "10/02 收盘等额买入"}]

    # 敞口对齐：每天保持实际的现金比例，其余放 SPY（每日再平衡）
    nav = init
    for prev, cur in zip(series, series[1:]):
        r = float(cur.benchmark_nav / prev.benchmark_nav) - 1
        nav *= 1 + (1 - float(prev.cash_weight)) * r
    rows.append({"name": "同等现金比例的 SPY", "nav": nav, "note": "每天沿用实际现金比例，其余全放 SPY"})

    basket = [t for t in SECTOR_BASKET if view.close(t, d0) and view.close(t, d_end)]
    if basket:
        ew = sum(init / len(basket) * float(view.close(t, d_end).price / view.close(t, d0).price) for t in basket)
        rows.append({"name": "等权行业 ETF", "nav": ew, "note": f"{d0:%m/%d} 收盘等权买入 {'/'.join(basket)} 并持有"})

    trade_days = sorted({e.occurred_at.astimezone(ET).date() for e in events if e.type == "FillRecorded"
                         and e.occurred_at.astimezone(ET).date() <= d_end})
    for day in trade_days:
        v, skipped = _replay_skip(settings, events, day, view, d_end)
        if v is not None:
            rows.append({"name": f"{day:%m/%d} 不交易", "nav": float(v),
                         "note": "跳过：" + "、".join(skipped), "delta_vs_actual": actual - float(v)})
    for r in rows:
        r["vs_actual"] = r["nav"] - actual
    pm.counterfactuals = rows

    matched = nav
    pm.decomposition = {
        "period": f"{d0:%Y-%m-%d} → {d_end:%Y-%m-%d}",
        "excess_vs_spy": actual - spy,
        "exposure_effect": matched - spy,
        "beyond_exposure": actual - matched,
        "avg_cash_weight": sum(float(p.cash_weight) for p in series) / len(series),
    }


def _lessons(pm: Postmortem) -> None:
    d = pm.decomposition
    if d:
        pm.lessons.append(
            f"{d['period']} 相对 SPY {d['excess_vs_spy']:+,.0f}：其中敞口效应（平均现金 {d['avg_cash_weight']:.0%}）"
            f"{d['exposure_effect']:+,.0f}，敞口之外（选股/择时/执行）{d['beyond_exposure']:+,.0f}")
        if abs(d["exposure_effect"]) > abs(d["beyond_exposure"]) * 2:
            pm.lessons.append("超额主要来自仓位高低，不是选股；后续判断要先回答“该持有多少风险”")
    dec, hold = pm.summary("decision"), pm.summary("hold")
    if dec["brier"] is not None:
        pm.lessons.append(f"已到期决策 {dec['n']} 个，方向命中率 {dec['hit_rate']:.0%}，Brier {dec['brier']:.3f}"
                          + ("（不比 50% 基线好）" if dec["brier"] >= 0.25 else ""))
    if hold["n"]:
        pm.lessons.append(f"已到期 HOLD {hold['n']} 次；见机会成本明细")
    for r in pm.counterfactuals:
        if "delta_vs_actual" in r and abs(r["delta_vs_actual"]) >= 500:
            pm.lessons.append(f"{r['name']}：实际比该分支 {r['delta_vs_actual']:+,.0f}（反事实，非已实现利润）")

"""事件预期 Agent（新增，对应 §3.6 与 §15 方向 D）。

- 事件日历：CPI、FOMC、非农、财报等，登记为 EventScheduled（只含公布时间与事前共识）。
- 事件前预测：在公布之前生成 bull/base/bear 情景树与“有条件交易方案”，作为 ForecastRecorded 冻结进账本。
- 实际值只能在公布时间之后登记（EventActualRecorded），预测时永远看不到。
- 复盘校准 Agent 之后按真实价格给这些预测打分。
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..clock import ET, ensure_aware, parse_dt
from ..event_store import EventStore
from ..ledger import LedgerState
from ..schemas import Scenario
from .llm import LLM
from .rotation import TickerFeatures


class EventForecast(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    horizon_days: int = Field(ge=1, le=20, description="事件后多少个交易日评估")
    scenarios: list[Scenario]
    priced_in: str = Field(description="市场目前大概已经反映了什么预期")
    conditional_plan: str = Field(description="事件前的有条件方案，例如：若数据高于共识且 SPY 跌破 X，则……；没有优势就写明 NO_TRADE 理由")
    invalidation_conditions: list[str]
    confidence: Literal["low", "medium", "high"]


class ExpectationError(ValueError):
    pass


def event_key(name: str, scheduled_at: datetime) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()[:24] or "EVENT"
    return f"{slug}_{ensure_aware(scheduled_at).astimezone(ET):%Y%m%dT%H%M}"  # ID 里用美东时间，便于对照


def schedule(store: EventStore, *, name: str, scheduled_at: datetime, tickers: list[str], category: str = "macro",
             consensus: str = "", source_url: str = "", at: datetime | None = None,
             competition_id: str | None = None) -> str:
    scheduled_at = ensure_aware(scheduled_at)
    key = event_key(name, scheduled_at)
    store.append("EventScheduled", {
        "event_key": key, "name": name, "scheduled_at": scheduled_at, "tickers": [t.upper() for t in tickers],
        "category": category, "consensus_before_release": consensus, "source_url": source_url,
    }, occurred_at=at, source="user", competition_id=competition_id)
    return key


def record_actual(store: EventStore, state: LedgerState, key: str, actual: str, at: datetime,
                  competition_id: str | None = None) -> None:
    ev = state.scheduled_events.get(key)
    if not ev:
        raise ExpectationError(f"事件不存在: {key}")
    if ensure_aware(at) < parse_dt(ev["scheduled_at"]):
        raise ExpectationError("还没到公布时间，不能登记实际值（防未来信息）")
    store.append("EventActualRecorded", {"event_key": key, "actual_after_release": actual},
                 occurred_at=at, source="user", competition_id=competition_id)


def pending(state: LedgerState, cutoff: datetime, lookahead_days: int = 7,
            all_forecasts: list[dict] | None = None) -> list[tuple[dict, str]]:
    """公布时间在 (cutoff, cutoff + lookahead] 内、且该标的还没有冻结预测的 (事件, 标的)。

    all_forecasts：账本里全部已冻结的预测（不按 as_of 过滤），避免回放过去日期时重复生成。
    """
    done = {(f["event_key"], f["ticker"]) for f in (all_forecasts if all_forecasts is not None else state.forecasts)}
    out = []
    for ev in state.scheduled_events.values():
        at = parse_dt(ev["scheduled_at"])
        if cutoff < at <= cutoff + timedelta(days=lookahead_days):
            for t in ev["tickers"] or ["SPY"]:
                if (ev["event_key"], t) not in done:
                    out.append((ev, t))
    return out


def upcoming(state: LedgerState, cutoff: datetime, lookahead_days: int = 14) -> list[dict]:
    return sorted((e for e in state.scheduled_events.values()
                   if cutoff < parse_dt(e["scheduled_at"]) <= cutoff + timedelta(days=lookahead_days)),
                  key=lambda e: e["scheduled_at"])


def p_up(scenarios: list[Scenario]) -> float:
    return sum(s.probability for s in scenarios if s.return_pct > 0) + 0.5 * sum(
        s.probability for s in scenarios if s.return_pct == 0)


def offline_forecast(ev: dict, ticker: str, f: TickerFeatures | None, horizon: int = 3) -> EventForecast:
    vol = (f.vol20_ann if f and f.vol20_ann else 0.20)
    sig = vol * math.sqrt(horizon / 252) * 100 * 1.25  # 事件窗口波动放大 25%
    return EventForecast(
        ticker=ticker, horizon_days=horizon,
        scenarios=[Scenario(name="bull", probability=0.30, return_pct=round(sig, 2), rationale="结果好于共识"),
                   Scenario(name="base", probability=0.40, return_pct=0.0, rationale="符合共识，影响有限"),
                   Scenario(name="bear", probability=0.30, return_pct=round(-sig, 2), rationale="结果差于共识")],
        priced_in=f"规则基线不判断预期差；共识：{ev.get('consensus_before_release') or '未登记'}",
        conditional_plan="规则基线无可识别预期差 → NO_TRADE；事件后按复盘结果校准",
        invalidation_conditions=["事件推迟或口径变更"], confidence="low")


def llm_forecast(llm: LLM, system: str, ev: dict, ticker: str, context: dict) -> EventForecast:
    safe_ev = {k: v for k, v in ev.items() if k in
               ("event_key", "name", "scheduled_at", "tickers", "category", "consensus_before_release", "source_url")}
    user = ("请在事件公布前给出预测（JSON）。你看不到实际值，也不能假设它。\n\n"
            + json.dumps({"event": safe_ev, "ticker": ticker, **context}, ensure_ascii=False, indent=1, default=str))
    out = llm.structured(system, user, EventForecast)
    return out.model_copy(update={"ticker": ticker})


def to_payload(ev: dict, fc: EventForecast, *, made_at: datetime, cutoff: datetime, engine: str) -> dict:
    if cutoff >= parse_dt(ev["scheduled_at"]):
        raise ExpectationError("事件已经公布，不能再生成事前预测")
    psum = sum(s.probability for s in fc.scenarios)
    if abs(psum - 1) > 0.01 or sorted(s.name for s in fc.scenarios) != ["base", "bear", "bull"]:
        raise ExpectationError(f"情景必须是 bull/base/bear 且概率之和为 1（实际 {psum:.3f}）")
    return {
        "forecast_id": f"fc_{uuid.uuid4().hex[:10]}", "event_key": ev["event_key"], "event_name": ev["name"],
        "scheduled_at": ev["scheduled_at"], "ticker": fc.ticker, "horizon_days": fc.horizon_days,
        "made_at": made_at, "data_cutoff": cutoff, "scenarios": [s.model_dump() for s in fc.scenarios],
        "p_up": round(p_up(fc.scenarios), 4),
        "expected_return_pct": round(sum(s.probability * s.return_pct for s in fc.scenarios), 4),
        "priced_in": fc.priced_in, "conditional_plan": fc.conditional_plan,
        "invalidation_conditions": fc.invalidation_conditions, "confidence": fc.confidence, "engine": engine,
    }

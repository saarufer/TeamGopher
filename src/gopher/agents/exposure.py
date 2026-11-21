"""仓位管理 Agent（新增）：给现金比例设目标区间，防止“默认持有现金”的惯性。

规则（确定性）：
- 目标现金区间由市场状态决定（config/risk_limits.yaml → exposure_policy）。
- 现金超出区间连续超过 max_days_out_of_band 个交易日，或连续多次 HOLD，
  就给 Strategist 下“必须提出再部署 / 减仓方案”的硬要求；Strategist 没做到时，本 Agent 自己补一个候选。
- 它只提议；是否执行仍然要走反方质询、风控和地鼠会议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..config import Settings
from ..schemas import Candidate, Scenario
from .regime import Regime
from .rotation import TickerFeatures, rank

DEFAULT_BANDS = {
    "risk_on": ("0.20", "0.50"),
    "neutral": ("0.35", "0.65"),
    "risk_off": ("0.55", "0.85"),
    "unknown": ("0.35", "0.75"),
}


@dataclass
class ExposureAssessment:
    regime: str
    cash_weight: float
    band: tuple[float, float]
    status: str                    # IN_BAND | TOO_MUCH_CASH | TOO_LITTLE_CASH | UNKNOWN
    gap_usd: float                 # 回到区间边界需要调动的金额
    days_out_of_band: int
    consecutive_holds: int
    mandate: str | None            # REDEPLOY | DE_RISK | None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _bands(settings: Settings) -> dict[str, tuple[float, float]]:
    cfg = (settings.risk.get("exposure_policy") or {}).get("bands") or {}
    out = {}
    for k, (lo, hi) in DEFAULT_BANDS.items():
        b = cfg.get(k) or {}
        out[k] = (float(b.get("cash_min", lo)), float(b.get("cash_max", hi)))
    return out


def assess(settings: Settings, regime: Regime, nav: Decimal | None, cash_weight: Decimal | None,
           cash_weight_history: list[float], decisions: list[dict],
           last_fill_seq: int = 0) -> ExposureAssessment:
    pol = settings.risk.get("exposure_policy") or {}
    max_days = int(pol.get("max_days_out_of_band", 3))
    max_holds = int(pol.get("max_consecutive_holds", 5))
    lo, hi = _bands(settings)[regime.label]
    if nav is None or cash_weight is None:
        return ExposureAssessment(regime.label, 0.0, (lo, hi), "UNKNOWN", 0.0, 0, 0, None, ["没有净值，无法评估仓位"])
    cw, navf = float(cash_weight), float(nav)
    if cw > hi:
        status, gap = "TOO_MUCH_CASH", (cw - hi) * navf
    elif cw < lo:
        status, gap = "TOO_LITTLE_CASH", (lo - cw) * navf
    else:
        status, gap = "IN_BAND", 0.0

    days = 0
    for w in reversed(cash_weight_history):
        if (status == "TOO_MUCH_CASH" and w > hi) or (status == "TOO_LITTLE_CASH" and w < lo):
            days += 1
        else:
            break

    # 每次运行都会记录一条 HOLD/NO_TRADE 对照决策；上次成交之后有几条，就是“连续几次运行没有动作”
    holds = sum(1 for d in decisions if d.get("ticker") is None and d["seq"] > last_fill_seq)

    notes = [f"市场状态 {regime.label} → 目标现金 {lo:.0%}–{hi:.0%}，当前 {cw:.1%}"]
    mandate = None
    if status != "IN_BAND":
        notes.append(f"已连续 {days} 个交易日在区间外（阈值 {max_days}），回到边界需调动约 ${gap:,.0f}")
        if days >= max_days or holds >= max_holds:
            mandate = "REDEPLOY" if status == "TOO_MUCH_CASH" else "DE_RISK"
    if holds:
        notes.append(f"上次成交之后已经运行 {holds} 次，都没有形成交易（阈值 {max_holds}）")
    return ExposureAssessment(regime.label, cw, (lo, hi), status, gap, days, holds, mandate, notes)


def complies(mandate: str | None, candidates: list[Candidate]) -> bool:
    if mandate == "REDEPLOY":
        return any(c.action == "BUY" for c in candidates)
    if mandate == "DE_RISK":
        return any(c.action == "SELL" for c in candidates)
    return True


def propose(ex: ExposureAssessment, settings: Settings, features: list[TickerFeatures], weights: dict[str, float],
            holdings: dict[str, float], evidence_ids: list[str], horizon: int) -> Candidate | None:
    """仓位管理 Agent 自己的补位候选：分步再部署到宽基/最强行业，或减掉 beta 最高的持仓。"""
    if not ex.mandate:
        return None
    step = float((settings.risk.get("exposure_policy") or {}).get("rebalance_step_usd", "5000"))
    amt = round(min(step, ex.gap_usd), 2)
    if amt < 500:
        return None
    by_t = {f.ticker: f for f in features}

    def sc(f: TickerFeatures | None) -> list[Scenario]:
        vol = (f.vol20_ann if f and f.vol20_ann else 0.20)
        sig = vol * (horizon / 252) ** 0.5 * 100
        return [Scenario(name="bull", probability=0.30, return_pct=round(sig, 2), rationale="+1σ"),
                Scenario(name="base", probability=0.45, return_pct=0.0, rationale="无方向假设：本候选的目的是回到目标仓位"),
                Scenario(name="bear", probability=0.25, return_pct=round(-sig, 2), rationale="-1σ")]

    if ex.mandate == "REDEPLOY":
        pick = next((f.ticker for f in rank(features)
                     if f.ticker in settings.etfs and f.above_sma20 and (f.rel_spy.get(20) or 0) > 0
                     and weights.get(f.ticker, 0) < 0.10), None)
        ticker = pick or ("RSP" if "RSP" in by_t else "SPY")
        thesis = (f"现金 {ex.cash_weight:.0%} 已连续 {ex.days_out_of_band} 天高于目标上限 {ex.band[1]:.0%}；"
                  f"分步再部署 ${amt:,.0f} 到 {ticker}，降低现金拖累。目的在于回到目标仓位，不代表看涨判断")
        return Candidate(action="BUY", ticker=ticker, budget_usd=amt, thesis=thesis, evidence_ids=evidence_ids,
                         scenarios=sc(by_t.get(ticker)), invalidation_conditions=[
                             "市场状态转为 risk_off（目标区间上移）", "SPY 收盘跌破 60 日低点"],
                         horizon_days=horizon, confidence="low")
    beta = {t: float(settings.beta(t)) for t in holdings}
    t = max(holdings, key=lambda k: (beta[k], weights.get(k, 0)))
    value = holdings[t] * (by_t[t].last_close if t in by_t else 0)
    amt = round(min(amt, value * 0.5), 2)
    if amt < 500:
        return None
    return Candidate(action="SELL", ticker=t, budget_usd=amt,
                     thesis=f"现金 {ex.cash_weight:.0%} 低于目标下限 {ex.band[0]:.0%}；减持 beta 最高的 {t} ${amt:,.0f}",
                     evidence_ids=evidence_ids, scenarios=sc(by_t.get(t)),
                     invalidation_conditions=["市场状态转为 risk_on（目标区间下移）"], horizon_days=horizon,
                     confidence="low")

"""Portfolio Strategist（§3.7）：LLM 或离线规则基线提出候选；代码负责校验与期望收益计算。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from ..schemas import Candidate, DecisionProposal, HoldCounterfactual, Scenario
from .llm import LLM
from .rotation import TickerFeatures, rank


@dataclass
class ValidatedCandidate:
    candidate: Candidate
    expected_return_pct: float          # 标的自身期望收益 E[R]（%）
    valid: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def edge_pct(self) -> float:
        """动作的期望优势：BUY 为 E[R]，SELL 为 -E[R]（避免的损失）。"""
        return self.expected_return_pct if self.candidate.action == "BUY" else -self.expected_return_pct


@dataclass
class StrategistResult:
    proposal: DecisionProposal
    candidates: list[ValidatedCandidate]
    engine: str
    warnings: list[str] = field(default_factory=list)


def expected_return(scenarios: list[Scenario]) -> float:
    return sum(s.probability * s.return_pct for s in scenarios)


def validate(proposal: DecisionProposal, evidence_ids: set[str], tradable: set[str], available_cash: float,
             max_candidates: int) -> list[ValidatedCandidate]:
    out = []
    for c in proposal.candidates[:max_candidates]:
        w: list[str] = []
        valid = True
        unknown = [e for e in c.evidence_ids if e not in evidence_ids]
        if unknown:
            w.append(f"引用了不存在/不可得的证据 {unknown}（已剔除）")
            c = c.model_copy(update={"evidence_ids": [e for e in c.evidence_ids if e in evidence_ids]})
        if not c.evidence_ids:
            valid = False
            w.append("没有任何可追溯证据")
        names = sorted(s.name for s in c.scenarios)
        if names != ["base", "bear", "bull"]:
            valid = False
            w.append(f"情景必须恰为 bull/base/bear，实际 {names}")
        psum = sum(s.probability for s in c.scenarios)
        if abs(psum - 1) > 0.01:
            valid = False
            w.append(f"情景概率之和 {psum:.3f} ≠ 1")
        if c.ticker.upper() not in tradable:
            valid = False
            w.append(f"{c.ticker} 不在研究覆盖范围或当前持仓内")
        if c.action == "BUY" and c.budget_usd > available_cash:
            valid = False
            w.append(f"预算 ${c.budget_usd:,.0f} 超过可用现金 ${available_cash:,.0f}")
        if not c.invalidation_conditions:
            valid = False
            w.append("缺少失效条件")
        out.append(ValidatedCandidate(c.model_copy(update={"ticker": c.ticker.upper()}), expected_return(c.scenarios), valid, w))
    return out


def llm_propose(llm: LLM, system: str, context: dict, model: str | None = None) -> DecisionProposal:
    user = ("以下是截至 data_cutoff 的全部可用输入（JSON）。请输出 DecisionProposal。\n\n"
            + json.dumps(context, ensure_ascii=False, indent=1, default=str))
    return llm.structured(system, user, DecisionProposal, model=model)


def rule_based_propose(features: list[TickerFeatures], holdings: dict[str, float], weights: dict[str, float],
                       cash: float, cash_weight: float, evidence_ids: list[str], budget: float,
                       horizon: int, max_candidates: int, benchmark: str = "SPY") -> DecisionProposal:
    """离线规则基线：相对强度 + 趋势 + 防追涨。不是投资建议，仅作为可复现的对照组。"""
    ranked = rank(features)
    by_t = {f.ticker: f for f in features}
    cands: list[Candidate] = []

    def scenarios(f: TickerFeatures) -> list[Scenario]:
        """标的自身收益情景（与动作方向无关）。"""
        vol = f.vol20_ann or 0.20
        sig = vol * math.sqrt(horizon / 252) * 100
        drift = (f.rel_spy.get(20) or 0) * 100 * horizon / 20 * 0.25  # 相对强度延续，收缩 75%
        return [
            Scenario(name="bull", probability=0.30, return_pct=round(sig, 2), rationale="+1σ 波动区间"),
            Scenario(name="base", probability=0.45, return_pct=round(drift, 2), rationale="20 日相对强度收缩 75% 的延续"),
            Scenario(name="bear", probability=0.25, return_pct=round(-1.2 * sig, 2), rationale="-1.2σ，含反转风险"),
        ]

    for f in ranked:
        if len(cands) >= max_candidates - 1:
            break
        if f.ticker == benchmark or weights.get(f.ticker, 0) >= 0.10:
            continue
        r10, r20, r1 = f.rel_spy.get(10), f.rel_spy.get(20), f.ret.get(1)
        daily_sig = (f.vol20_ann or 0.2) / math.sqrt(252)
        if r10 is None or r20 is None or r10 <= 0 or r20 <= 0 or not f.above_sma20:
            continue
        if r1 is not None and r1 > 1.5 * daily_sig:
            continue  # 防追涨：当日涨幅过大不追
        amt = min(budget, cash * 0.9)
        if amt < 500:
            break
        cands.append(Candidate(
            action="BUY", ticker=f.ticker, budget_usd=round(amt, 2),
            thesis=f"{f.ticker} 10/20 日相对 SPY 分别 {r10:+.1%}/{r20:+.1%}，位于 20 日均线上方；以小额试探降低现金拖累",
            evidence_ids=list(evidence_ids), scenarios=scenarios(f),
            invalidation_conditions=[f"收盘跌破 20 日均线（当前偏离 {f.sma20_gap:+.1%}）",
                                     "10 日相对 SPY 转负", "行情源冲突或数据缺失"],
            horizon_days=horizon, confidence="low"))

    for t, w in sorted(weights.items(), key=lambda kv: kv[1]):
        f = by_t.get(t)
        if len(cands) >= max_candidates or not f or t == benchmark:
            continue
        r20 = f.rel_spy.get(20)
        if r20 is not None and r20 < -0.03 and f.above_sma20 is False:
            value = holdings.get(t, 0) * f.last_close
            cands.append(Candidate(
                action="SELL", ticker=t, budget_usd=round(value / 2, 2),
                thesis=f"{t} 20 日相对 SPY {r20:+.1%} 且低于 20 日均线；减半以控制弱势敞口",
                evidence_ids=list(evidence_ids), scenarios=scenarios(f),
                invalidation_conditions=["重新站上 20 日均线", "20 日相对强度转正"],
                horizon_days=horizon, confidence="low"))

    bench = by_t.get(benchmark)
    b20 = (bench.ret.get(20) if bench else None) or 0
    return DecisionProposal(
        market_state=(f"{benchmark} 20 日收益 {b20:+.1%}；相对强度前列："
                      + "、".join(f"{f.ticker}({(f.rel_spy.get(20) or 0):+.1%})" for f in ranked[:4])),
        candidates=cands,
        hold_counterfactual=HoldCounterfactual(
            rationale="不交易：保持当前现金与持仓结构",
            cash_opportunity_cost_note=(f"现金权重 {cash_weight:.0%}；若 SPY 未来 {horizon} 日重复过去 20 日节奏，"
                                        f"现金部分相对基准约 {-cash_weight * b20 * horizon / 20:+.2%}")),
        no_trade_reason=None if cands else "没有同时满足相对强度、趋势与防追涨条件的标的",
        key_risks=["规则基线未使用新闻/基本面", "动量可能在事件后快速反转", "假设 beta 不是估计值"],
    )

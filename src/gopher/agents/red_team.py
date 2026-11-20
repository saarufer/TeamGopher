"""Red Team（§3.9）：对每笔建议至少三项质疑；可输出 VETO_RECOMMENDED，但不能修改仓位。"""

from __future__ import annotations

import json
import math

from ..schemas import CandidateReview, Objection, RedTeamReview
from .llm import LLM
from .rotation import TickerFeatures
from .strategist import ValidatedCandidate

TECH_CLUSTER = {"QQQ", "XLK", "NVDA", "AMD", "AVGO", "MSFT", "GOOGL", "AMZN", "META", "PLTR"}


def llm_review(llm: LLM, system: str, context: dict, model: str | None = None) -> RedTeamReview:
    user = "请对以下提案进行独立质询（JSON）：\n\n" + json.dumps(context, ensure_ascii=False, indent=1, default=str)
    review = llm.structured(system, user, RedTeamReview, model=model)
    for r in review.reviews:
        if len(r.objections) < 3:
            r.verdict = "PROCEED_WITH_CAUTION" if r.verdict == "PROCEED" else r.verdict
    return review


def rule_based_review(cands: list[ValidatedCandidate], features: dict[str, TickerFeatures],
                      holdings: dict[str, float], evidence_reliability: dict[str, str],
                      min_edge_pct: float) -> RedTeamReview:
    reviews = []
    held_tech = sorted(t for t in holdings if t in TECH_CLUSTER and holdings[t] > 0)
    for vc in cands:
        c = vc.candidate
        f = features.get(c.ticker)
        rel = {evidence_reliability.get(e, "unknown") for e in c.evidence_ids}
        o1 = Objection(
            question="哪个关键事实若错误会推翻交易？",
            challenge=("论据完全来自派生价格特征；若行情源有误、存在未处理的公司行为或口径混用，信号即失效"
                       if rel <= {"provisional", "unknown"} else "核心证据可靠性需复核"),
            evidence_gap="缺少基本面/事件层面的独立证据" if all(e.startswith("ev_rot") or e.startswith("ev_pf") for e in c.evidence_ids) else "—")
        priced = "无法判断"
        if f and f.vol20_ann and f.ret.get(20) is not None:
            sig20 = f.vol20_ann * math.sqrt(20 / 252)
            z = f.ret[20] / sig20 if sig20 else 0
            priced = f"20 日涨幅约 {z:+.1f}σ" + ("，大概率已被价格反映" if abs(z) > 2 else "")
        o2 = Objection(question="这个优势是否已被市场价格反映？", challenge=priced,
                       evidence_gap="没有市场预期（共识/估值）数据对照")
        chase = bool(f and f.ret.get(1) is not None and f.vol20_ann and
                     f.ret[1] > 1.0 * f.vol20_ann / math.sqrt(252))
        overlap = c.ticker in TECH_CLUSTER and held_tech
        o3 = Objection(
            question="是否只是因为昨日涨跌、当前持仓或用户观点而形成确认偏误？",
            challenge="；".join(x for x in [
                "最近一日上涨超过 1σ，存在追涨嫌疑" if chase else "",
                f"与现有科技敞口重叠：{', '.join(held_tech)}" if overlap else "",
                "已持有该标的，可能是确认偏误" if holdings.get(c.ticker, 0) > 0 and c.action == "BUY" else "",
            ] if x) or "未发现明显的追涨或持仓确认偏误",
            evidence_gap="规则基线没有样本外校准")
        objs = [o1, o2, o3]
        if not vc.valid:
            verdict = "VETO_RECOMMENDED"
        elif vc.edge_pct < min_edge_pct or (chase and vc.edge_pct < 2 * min_edge_pct):
            verdict = "VETO_RECOMMENDED"
        elif chase or overlap or "已被价格反映" in priced:
            verdict = "PROCEED_WITH_CAUTION"
        else:
            verdict = "PROCEED"
        reviews.append(CandidateReview(
            ticker=c.ticker, action=c.action, objections=objs,
            would_flip_if=f"期望优势 {vc.edge_pct:+.2f}% 低于门槛 {min_edge_pct:.2f}%，或 {c.invalidation_conditions[0] if c.invalidation_conditions else '失效条件触发'}",
            verdict=verdict))
    concerns = []
    if held_tech:
        concerns.append(f"QQQ 与个股科技仓位重叠：{', '.join(held_tech)}")
    return RedTeamReview(reviews=reviews, portfolio_level_concerns=concerns)

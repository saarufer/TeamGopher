"""每日流程（§9）：普通 Python 状态机编排多 Agent。

数据 → 审计 → 估值 → 行业轮动 → (新闻) → Strategist → Red Team → Risk → 会议门控 → 记录 → 日报
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .agents import auditor, exposure, expectations, postmortem, red_team, regime, rotation, strategist
from .agents.llm import LLM, LLMError, make_llm
from .agents.news import gather_news
from .approvals import create_meeting
from .clock import ensure_aware, now_utc, parse_dt
from .config import Settings
from .event_store import EventStore, canonical, sha256, to_jsonable
from .evidence import EvidenceStore
from .ledger import replay
from .market_data import MarketStore
from .risk import assess, portfolio_beta, stress
from .schemas import DecisionProposal, RedTeamReview
from .valuation import Performance, Valuation, attribution, max_drawdown, nav_series, performance, value_portfolio


@dataclass
class RunResult:
    as_of: datetime
    competition_id: str
    engine: str
    mode: str
    valuation: Valuation
    performance: Performance | None
    audit: auditor.AuditReport
    features_ranked: list
    proposal: DecisionProposal
    review: RedTeamReview
    gated: list[dict]
    meeting_id: str | None
    orders: list
    recorded: bool = True
    warnings: list[str] = field(default_factory=list)
    regime: regime.Regime | None = None
    exposure: exposure.ExposureAssessment | None = None
    new_forecasts: list[dict] = field(default_factory=list)
    upcoming_events: list[dict] = field(default_factory=list)
    open_forecasts: list[dict] = field(default_factory=list)
    postmortem: postmortem.Postmortem | None = None
    portfolio_beta: Decimal | None = None
    stress: dict | None = None
    max_drawdown: Decimal | None = None
    attribution: dict | None = None
    report_path: Path | None = None
    report_hash: str | None = None


def _load_prompt(settings: Settings, name: str, **fmt) -> str:
    text = (settings.prompts_dir / f"{name}.md").read_text(encoding="utf-8")
    for k, v in fmt.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def run_daily(settings: Settings, *, as_of: datetime | None = None, engine: str | None = None,
              web: bool = False, record: bool = True, llm: LLM | None = None) -> RunResult:
    now = now_utc()
    cutoff = ensure_aware(as_of) if as_of else now
    warnings: list[str] = []
    backtest = cutoff < now - timedelta(hours=1)
    comp = settings.active_competition_id
    acfg = settings.agents
    mode = acfg.get("mode", "shadow")

    store = EventStore(settings.events_path)
    events = store.load()
    if not events:
        raise RuntimeError("事件日志为空：请先运行 `gopher init` 导入历史流水")
    state = replay(events, settings.initial_cash, as_of=cutoff)
    market = MarketStore(settings.bars_path, settings.data_sources.get("priority"))
    view = market.view(cutoff)
    evidence = EvidenceStore(settings.evidence_path)

    # 1. 审计 + 估值
    audit_rep = auditor.audit(store, events, state)
    val = value_portfolio(state, view, settings, audit_ok=audit_rep.passed)
    perf = performance(val, settings)
    weights = {t: float(w) for t, w in val.weights().items()}
    holdings = {t: float(q) for t, q in state.held().items()}

    # 2. 行业轮动（确定性）
    universe = list(dict.fromkeys([*acfg.get("research_universe", []), *holdings.keys()]))
    feats = rotation.compute(view, universe, settings.benchmark_ticker)
    ranked = rotation.rank(feats)
    by_t = {f.ticker: f for f in feats}
    if not feats:
        warnings.append("没有可用行情：请先运行 `gopher fetch`")

    # 2b. 市场状态（新增，确定性）
    reg = regime.assess(view, settings.benchmark_ticker)

    # 2c. 净值序列 → 仓位管理（新增）与复盘校准（新增）
    series = nav_series(events, market, settings, cutoff) if val.nav else []
    last_fill_seq = max((f["seq"] for f in state.fills), default=0)
    expo = exposure.assess(settings, reg, val.nav, val.cash_weight, [float(p.cash_weight) for p in series],
                           state.decisions, last_fill_seq)
    pm = postmortem.run(settings, events, view, cutoff, series) if series else None

    # 3. 证据快照（派生证据，available_at = data_cutoff）
    tag = f"{cutoff:%Y%m%dT%H%M}_{uuid.uuid4().hex[:4]}"
    ev_rot = evidence.add(evidence_id=f"ev_rot_{tag}", source_url="derived://rotation", retrieved_at=now,
                          available_at=cutoff, source_type="derived", reliability="provisional",
                          content=[f.as_dict() for f in feats], title="行业轮动特征快照",
                          summary="1/5/10/20 日收益、相对 SPY、波动率与趋势", tickers=[f.ticker for f in feats],
                          persist=record)
    ev_pf = evidence.add(evidence_id=f"ev_pf_{tag}", source_url="derived://portfolio", retrieved_at=now,
                         available_at=cutoff, source_type="derived", reliability="provisional",
                         content={"cash": state.cash, "positions": state.held(), "nav": val.nav},
                         title="组合快照", summary="重放账本得到的现金与持仓", persist=record)

    # 4. LLM / 离线引擎选择
    engine = engine or acfg.get("llm", {}).get("provider", "anthropic")
    if engine != "offline" and llm is None:
        try:
            llm = make_llm({**acfg, "llm": {**acfg.get("llm", {}), "provider": engine}})
        except LLMError as e:
            warnings.append(f"LLM 不可用，退回离线规则基线：{e}")
            llm = None
    engine_name = f"{engine}:{acfg.get('llm', {}).get('model')}" if llm else "offline-rules"

    if web and llm and not record:
        warnings.append("dry-run 不执行联网新闻检索（检索结果会写入证据库）")
    elif web and llm:
        if backtest:
            warnings.append("回测模式禁用联网新闻：无法保证检索结果在 data_cutoff 前可得")
        else:
            try:
                _, ws = gather_news(llm, _load_prompt(settings, "news"), universe, now, evidence)
                warnings += ws
            except LLMError as e:
                warnings.append(f"新闻检索失败：{e}")

    # 4b. 事件预期（新增）：对即将公布的事件冻结事前预测
    new_fcs: list[dict] = []
    lookahead = int(acfg.get("event_lookahead_days", 7))
    all_fcs = replay(events, settings.initial_cash).forecasts
    for ev_, tk in expectations.pending(state, cutoff, lookahead, all_fcs):
        try:
            if llm:
                fc = expectations.llm_forecast(llm, _load_prompt(settings, "expectations"), ev_, tk, {
                    "data_cutoff": cutoff, "features": by_t[tk].as_dict() if tk in by_t else None,
                    "market_regime": reg.as_dict()})
            else:
                fc = expectations.offline_forecast(ev_, tk, by_t.get(tk))
            new_fcs.append(expectations.to_payload(ev_, fc, made_at=now, cutoff=cutoff, engine=engine_name))
        except (LLMError, expectations.ExpectationError) as e:
            warnings.append(f"事件预期 {ev_['event_key']}/{tk} 失败：{e}")
    if record and new_fcs:
        store.append_many([("ForecastRecorded", f, now, "expectations", comp) for f in new_fcs])
    upcoming = expectations.upcoming(state, cutoff)
    open_fcs = [f for f in [*all_fcs, *new_fcs]
                if parse_dt(f["data_cutoff"]) <= cutoff and parse_dt(f["scheduled_at"]) > cutoff]

    visible = [ev_rot, ev_pf] + [e for e in evidence.visible(cutoff)
                                 if e.source_type != "derived" and (cutoff - e.available_at).days <= 7]
    ev_ids = {e.evidence_id for e in visible}
    reliab = {e.evidence_id: e.reliability for e in visible}

    # 5. Strategist
    max_c = int(acfg.get("max_candidates", 3))
    budget = float(acfg.get("default_trade_budget_usd", 3000))
    horizon = int(acfg.get("horizon_days", 10))
    avail_cash = float(state.cash - state.reserved_cash())
    approvals_ctx = [{
        "approval_id": k, "sides": s.approval.sides, "budget_usd": s.approval.budget_usd,
        "used_usd": s.used_usd, "remaining_usd": state.approval_remaining(k),
        "ticker_caps_usd": s.approval.ticker_caps_usd, "valid_until": s.approval.valid_until,
    } for k, s in state.approvals.items()]
    context = {
        "data_cutoff": cutoff.isoformat(), "competition_id": comp,
        "portfolio": {"cash": state.cash, "available_cash": avail_cash, "positions": holdings, "weights": weights,
                      "nav": val.nav, "valuation_official": val.official, "cash_weight": val.cash_weight},
        "performance_vs_spy": to_jsonable(perf.__dict__) if perf else None,
        "rotation_features": [f.as_dict() for f in ranked],
        "approvals": approvals_ctx,
        "evidence": [{"evidence_id": e.evidence_id, "source_type": e.source_type, "reliability": e.reliability,
                      "available_at": e.available_at, "title": e.title, "summary": e.summary,
                      "claim_type": e.claim_type, "url": e.source_url, "tickers": e.tickers} for e in visible],
        "market_regime": reg.as_dict(),
        "exposure_policy": {**expo.as_dict(), "instruction": (
            "必须至少提出一个 BUY 候选，把现金拉回目标区间" if expo.mandate == "REDEPLOY" else
            "必须至少提出一个 SELL 候选，把现金拉回目标区间" if expo.mandate == "DE_RISK" else "现金在目标区间内，无强制要求")},
        "upcoming_events": [{k: e[k] for k in ("event_key", "name", "scheduled_at", "tickers", "consensus_before_release")}
                            for e in upcoming],
        "frozen_event_forecasts": [{k: f[k] for k in ("event_key", "ticker", "p_up", "expected_return_pct",
                                                      "conditional_plan", "horizon_days")} for f in open_fcs],
        "postmortem": pm.context() if pm else None,
        "constraints": {"long_only": True, "no_leverage": True, "no_options": True, "max_candidates": max_c,
                        "default_trade_budget_usd": budget, "horizon_days": horizon},
    }
    proposal = None
    if llm:
        try:
            proposal = strategist.llm_propose(llm, _load_prompt(settings, "strategist", max_candidates=max_c), context)
        except LLMError as e:
            warnings.append(f"Strategist LLM 失败，退回规则基线：{e}")
            engine_name = "offline-rules (fallback)"
    if proposal is None:
        proposal = strategist.rule_based_propose(
            feats, holdings, weights, avail_cash, float(val.cash_weight or 0),
            [ev_rot.evidence_id, ev_pf.evidence_id], budget, horizon, max_c, settings.benchmark_ticker)
    tradable = set(universe) | set(holdings)
    vcs = strategist.validate(proposal, ev_ids, tradable, avail_cash, max_c)

    # 5b. 仓位管理 Agent 补位：有强制要求而 Strategist 没做到时，由它自己提出候选
    if expo.mandate and not exposure.complies(expo.mandate, [v.candidate for v in vcs if v.valid]):
        extra = exposure.propose(expo, settings, feats, weights, holdings, [ev_rot.evidence_id, ev_pf.evidence_id], horizon)
        if extra:
            tmp = proposal.model_copy(update={"candidates": [extra]})
            add = strategist.validate(tmp, ev_ids, tradable | {extra.ticker}, avail_cash, 1)
            for v in add:
                v.origin = "exposure"
            vcs += add
            warnings.append(f"Strategist 未满足仓位管理要求（{expo.mandate}），已由仓位管理 Agent 补充候选 {extra.ticker}")

    # 6. Red Team
    min_edge = float(settings.risk_decimal("min_expected_edge_pct", "0.30"))
    review = None
    if llm and vcs:
        try:
            review = red_team.llm_review(
                llm, _load_prompt(settings, "red_team"),
                {"proposal": proposal.model_dump(),
                 "validated": [{"ticker": v.candidate.ticker, "expected_return_pct": v.expected_return_pct, "edge_pct": v.edge_pct,
                                "valid": v.valid, "warnings": v.warnings} for v in vcs],
                 "portfolio": context["portfolio"], "rotation_features": context["rotation_features"],
                 "evidence": context["evidence"]},
                model=acfg.get("llm", {}).get("red_team_model"))
        except LLMError as e:
            warnings.append(f"Red Team LLM 失败，退回规则反方：{e}")
    if review is None:
        review = red_team.rule_based_review(vcs, by_t, holdings, reliab, min_edge)
    rv_by = {(r.ticker.upper(), r.action): r for r in review.reviews}

    # 7. Risk + 门控
    gated = []
    for vc in vcs:
        c = vc.candidate
        f = by_t.get(c.ticker)
        price = Decimal(str(f.last_close)) if f else None
        rk = assess(state, val, c.ticker, c.action, Decimal(str(c.budget_usd)), settings, cutoff, price)
        rv = rv_by.get((c.ticker, c.action))
        if not vc.valid or rk.blocked:
            gate = "BLOCKED"
        elif vc.edge_pct < min_edge and vc.origin != "exposure":
            gate = "NO_EDGE"
        elif rk.meeting_required or (rv and rv.verdict == "VETO_RECOMMENDED"):
            gate = "MEETING_REQUIRED"
        else:
            gate = "AUTHORIZED_BY_EXISTING_APPROVAL"
        gated.append({"candidate": c, "validated": vc, "risk": rk, "review": rv, "gate": gate, "origin": vc.origin})

    # 8. 记录（不可变）
    meeting_id = None
    if record:
        items = []
        for g in gated:
            c, vc, rk = g["candidate"], g["validated"], g["risk"]
            items.append(("DecisionRecorded", {
                "decision_id": f"dec_{tag}_{c.ticker}_{c.action}", "decided_at": now, "data_cutoff": cutoff,
                "action": c.action, "ticker": c.ticker, "thesis": c.thesis, "evidence_ids": c.evidence_ids,
                "invalidation_conditions": c.invalidation_conditions, "scenarios": [s.model_dump() for s in c.scenarios],
                "expected_return_pct": vc.expected_return_pct, "edge_pct": vc.edge_pct, "gate": g["gate"], "engine": engine_name,
                "red_team_verdict": g["review"].verdict if g["review"] else None,
                "meeting_triggers": rk.meeting_triggers, "hard_blocks": rk.hard_blocks,
                "approval_id": rk.covering_approval_id, "horizon_days": c.horizon_days, "origin": vc.origin,
                "market_regime": reg.label,
            }, now, "orchestrator", comp))
        items.append(("DecisionRecorded", {
            "decision_id": f"dec_{tag}_HOLD", "decided_at": now, "data_cutoff": cutoff,
            "action": "HOLD" if gated else "NO_TRADE", "ticker": None,
            "thesis": proposal.hold_counterfactual.rationale,
            "cash_opportunity_cost_note": proposal.hold_counterfactual.cash_opportunity_cost_note,
            "no_trade_reason": proposal.no_trade_reason, "evidence_ids": [ev_pf.evidence_id],
            "invalidation_conditions": [], "engine": engine_name, "horizon_days": horizon,
            "market_regime": reg.label, "cash_weight": val.cash_weight, "exposure_status": expo.status,
        }, now, "orchestrator", comp))
        store.append_many(items)
        need = [g for g in gated if g["gate"] == "MEETING_REQUIRED"]
        if need:
            meeting_id = create_meeting(
                store, at=now, competition_id=comp,
                summary=f"{len(need)} 项提案需要授权（{engine_name}）",
                triggers=sorted({t for g in need for t in g["risk"].meeting_triggers}
                                | ({"Red Team 建议否决"} if any(g["review"] and g["review"].verdict == "VETO_RECOMMENDED" for g in need) else set())),
                candidates=[{
                    "index": i, "origin": g["origin"], "action": g["candidate"].action, "ticker": g["candidate"].ticker,
                    "budget_usd": g["candidate"].budget_usd, "thesis": g["candidate"].thesis,
                    "expected_return_pct": g["validated"].expected_return_pct, "edge_pct": g["validated"].edge_pct,
                    "scenarios": [s.model_dump() for s in g["candidate"].scenarios],
                    "invalidation_conditions": g["candidate"].invalidation_conditions,
                    "red_team": g["review"].model_dump() if g["review"] else None,
                    "risk": {"post_weight": g["risk"].post_weight, "post_cash_weight": g["risk"].post_cash_weight,
                             "triggers": g["risk"].meeting_triggers, "stress_post": g["risk"].stress_post},
                    "decision_id": f"dec_{tag}_{g['candidate'].ticker}_{g['candidate'].action}",
                } for i, g in enumerate(need)],
                extra={"do_nothing": proposal.hold_counterfactual.model_dump(), "data_cutoff": cutoff})
        if val.official and not backtest:
            store.append("ValuationPublished", {
                "session_date": val.session_date.isoformat(), "nav": val.nav, "cash": val.cash,
                "benchmark_nav": val.benchmark_nav, "official": True,
            }, occurred_at=now, source="orchestrator", competition_id=comp)

    # 9. 绩效序列 / 风险
    mdd = max_drawdown([s.nav for s in series]) if series else None
    attr = attribution(series, state, val) if val.nav else None
    w_dec = val.weights()
    res = RunResult(
        as_of=cutoff, competition_id=comp, engine=engine_name, mode=mode, valuation=val, performance=perf,
        audit=audit_rep, features_ranked=ranked, proposal=proposal, review=review, gated=gated,
        meeting_id=meeting_id, recorded=record, orders=list(replay(store.load(), settings.initial_cash).orders.values()),
        warnings=warnings, portfolio_beta=portfolio_beta(w_dec, settings) if val.nav else None,
        stress=stress(val.nav, w_dec, settings) if val.nav else None, max_drawdown=mdd, attribution=attr,
        regime=reg, exposure=expo, new_forecasts=new_fcs, upcoming_events=upcoming, open_forecasts=open_fcs,
        postmortem=pm,
    )

    # 10. 日报
    from .reporting import render_daily
    md = render_daily(res)
    res.report_hash = sha256(md)
    if record:
        out_dir = settings.reports_dir / f"{val.session_date:%Y-%m-%d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"daily_report_{cutoff:%H%M}.md"
        path.write_text(md + f"\n<!-- report_hash: {res.report_hash} -->\n", encoding="utf-8")
        res.report_path = path
    return res

"""Reporter（§3.12）：确定性中文日报。不把提案写成已成交，不把反事实写成已实现利润。"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from .clock import fmt_dual, parse_dt
from .ledger import q2

if TYPE_CHECKING:
    from .orchestrator import RunResult


def _usd(x) -> str:
    return "—" if x is None else f"${q2(Decimal(str(x))):,}"


def _pct(x, digits=2) -> str:
    return "—" if x is None else f"{float(x) * 100:+.{digits}f}%"


def render_daily(r: "RunResult") -> str:
    v, p, a = r.valuation, r.performance, r.audit
    L: list[str] = []
    status = "正式收盘估值" if v.official else "暂估（未通过正式发布条件）"
    L.append(f"# 地鼠团队日报 · {v.session_date:%Y-%m-%d}")
    L.append("")
    L.append(f"- 检查时点：{fmt_dual(r.as_of)}（数据截止 data_cutoff = 检查时点）")
    L.append(f"- 估值日：{v.session_date}（{status}）")
    L.append(f"- 比赛口径：`{r.competition_id}`；基准 SPY 价格收益（起点 2026-10-02 收盘 $769.64，不含分红）")
    L.append(f"- 研究引擎：{r.engine}；运行模式：{r.mode}")
    if v.reasons:
        L.append(f"- 估值说明：{'；'.join(v.reasons)}")
    L.append("")

    L.append("## 组合与 SPY")
    L.append("")
    if p:
        L.append("| 指标 | 数值 |")
        L.append("|---|---:|")
        L.append(f"| 组合 NAV | {_usd(p.nav)} |")
        L.append(f"| SPY 基准 NAV | {_usd(p.benchmark_nav)} |")
        L.append(f"| 绝对收益 | {_pct(p.absolute_return)} |")
        L.append(f"| SPY 收益 | {_pct(p.benchmark_return)} |")
        L.append(f"| 相对 SPY | {float(p.active_return_pp):+.2f}pp / {_usd(p.active_pnl_usd)} |")
        L.append(f"| 现金 / 权重 | {_usd(v.cash)} / {_pct(v.cash_weight, 1).lstrip('+')} |")
        if r.max_drawdown is not None:
            L.append(f"| 最大回撤（可得序列） | {_pct(r.max_drawdown)} |")
    else:
        L.append(f"> 缺少行情，无法计算 NAV。登记现金 {_usd(v.cash)}。请先运行 `gopher fetch`。")
    L.append("")
    L.append("| 资产 | 数量 | 收盘价 | 来源 | 市值 | 权重 | 未实现盈亏 |")
    L.append("|---|---:|---:|---|---:|---:|---:|")
    for pv in v.positions:
        flag = " ⚠冲突" if pv.conflict else (" ⚠过期" if pv.stale else "")
        L.append(f"| {pv.ticker} | {pv.quantity} | {_usd(pv.price)} | {pv.source or '未核实'}{flag} | "
                 f"{_usd(pv.value)} | {_pct(pv.weight, 1).lstrip('+')} | {_usd(pv.unrealized_pnl)} |")
    L.append("")
    if r.attribution:
        L.append(f"现金效应（近似估计，非已实现利润）：{_usd(r.attribution['cash_effect_usd_est'])}")
        L.append("")

    L.append("## 风险")
    L.append("")
    if r.portfolio_beta is not None:
        L.append(f"- 组合 beta（假设值加权）：{float(r.portfolio_beta):.2f}")
    if r.stress:
        L.append("- 市场情景压力测试（按 beta 映射）：" + "，".join(f"{k} → {_usd(v_)}" for k, v_ in r.stress.items()))
    L.append("")

    if r.regime:
        from .agents.regime import LABEL_CN
        rg, ex = r.regime, r.exposure
        L.append(f"## 市场状态与仓位（市场状态 Agent / 仓位管理 Agent）")
        L.append("")
        L.append(f"- 市场状态：**{LABEL_CN.get(rg.label, rg.label)}**（{rg.label}，得分 {rg.score:+d}）— " + "；".join(rg.reasons))
        if ex:
            st_cn = {"IN_BAND": "在区间内", "TOO_MUCH_CASH": "现金过多", "TOO_LITTLE_CASH": "现金过少", "UNKNOWN": "未知"}[ex.status]
            L.append(f"- 目标现金区间 {ex.band[0]:.0%}–{ex.band[1]:.0%}，当前 {ex.cash_weight:.1%} → **{st_cn}**")
            for n in ex.notes[1:]:
                L.append(f"- {n}")
            if ex.mandate:
                L.append(f"- ⚠ 强制要求：{'再部署（提出 BUY 方案）' if ex.mandate == 'REDEPLOY' else '降风险（提出 SELL 方案）'}")
        L.append("")

    if r.upcoming_events or r.new_forecasts or r.open_forecasts:
        L.append("## 事件预期（事件预期 Agent，事前冻结）")
        L.append("")
        if r.upcoming_events:
            L.append("| 事件 | 公布时间 | 标的 | 事前共识 |")
            L.append("|---|---|---|---|")
            for e in r.upcoming_events:
                L.append(f"| {e['name']} | {fmt_dual(parse_dt(e['scheduled_at']))} | "
                         f"{', '.join(e['tickers'])} | {e.get('consensus_before_release') or '未登记'} |")
            L.append("")
        for f in r.open_forecasts:
            new = " 🆕" if f in r.new_forecasts else ""
            L.append(f"- {f['event_name']} · {f['ticker']}{new}：上涨概率 {f['p_up']:.0%}，期望 {f['expected_return_pct']:+.2f}%"
                     f"（事件后 {f['horizon_days']} 个交易日）；方案：{f['conditional_plan']}")
        L.append("")

    L.append("## 行业轮动雷达（相对 SPY）")
    L.append("")
    L.append("| 标的 | 1日 | 5日 | 10日相对 | 20日相对 | 20日年化波动 | 20日线上方 |")
    L.append("|---|---:|---:|---:|---:|---:|:---:|")
    for f in r.features_ranked[:12]:
        L.append(f"| {f.ticker} | {_pct(f.ret.get(1))} | {_pct(f.ret.get(5))} | {_pct(f.rel_spy.get(10))} | "
                 f"{_pct(f.rel_spy.get(20))} | {_pct(f.vol20_ann, 1).lstrip('+') if f.vol20_ann else '—'} | "
                 f"{'是' if f.above_sma20 else ('否' if f.above_sma20 is False else '—')} |")
    L.append("")

    L.append("## 研究结论与拟采取操作（提案，尚未成交）")
    L.append("")
    L.append(f"**市场状态：** {r.proposal.market_state}")
    L.append("")
    if not r.gated:
        L.append(f"**结论：NO_TRADE** — {r.proposal.no_trade_reason or '无可识别优势'}")
        L.append("")
    for i, g in enumerate(r.gated):
        c, vc = g["candidate"], g["validated"]
        src = "（仓位管理 Agent 补位）" if g.get("origin") == "exposure" else ""
        L.append(f"### 方案 {i}：{c.action} {c.ticker} ≈ {_usd(c.budget_usd)} → **{g['gate']}**{src}")
        L.append("")
        L.append(f"- 论点：{c.thesis}")
        L.append("- 情景：" + "；".join(f"{s.name} p={s.probability:.2f} r={s.return_pct:+.2f}%" for s in c.scenarios)
                 + f"；标的 E[R] = {vc.expected_return_pct:+.2f}%，动作期望优势 {vc.edge_pct:+.2f}%（主观估计，需回测校准）")
        L.append(f"- 失效条件：{'；'.join(c.invalidation_conditions)}")
        L.append(f"- 证据：{', '.join(c.evidence_ids) or '无'}")
        if vc.warnings:
            L.append(f"- 校验警告：{'；'.join(vc.warnings)}")
        rv = g.get("review")
        if rv:
            L.append(f"- 反方（{rv.verdict}）：" + " / ".join(o.challenge for o in rv.objections))
        rk = g["risk"]
        L.append(f"- 风险：{c.ticker} 权重 {rk.pre_weight:.1%} → {rk.post_weight:.1%}；现金 {rk.pre_cash_weight:.1%} → "
                 f"{rk.post_cash_weight:.1%}；beta {rk.pre_beta:.2f} → {rk.post_beta:.2f}")
        if rk.hard_blocks:
            L.append(f"- 硬约束阻止：{'；'.join(rk.hard_blocks)}")
        if rk.meeting_triggers:
            L.append(f"- 会议触发：{'；'.join(rk.meeting_triggers)}")
        L.append("")
    L.append(f"**HOLD 对照：** {r.proposal.hold_counterfactual.rationale}；{r.proposal.hold_counterfactual.cash_opportunity_cost_note}")
    L.append("")
    if r.proposal.key_risks:
        L.append("**主要风险：** " + "；".join(r.proposal.key_risks))
        L.append("")

    L.append("## 地鼠会议")
    L.append("")
    if r.meeting_id:
        L.append(f"需要用户授权：会议 `{r.meeting_id}`。查看：`gopher meeting show {r.meeting_id}`；"
                 f"批准需指定方案、额度与有效期：`gopher meeting approve {r.meeting_id} --candidate N --caps TICKER=USD --valid-until YYYY-MM-DD`。")
    elif not r.recorded and any(g["gate"] == "MEETING_REQUIRED" for g in r.gated):
        L.append("dry-run：有提案需要授权，但未写入日志、未创建会议。去掉 --dry-run 重新运行以发起会议。")
    else:
        L.append("本次无需召开会议。")
    L.append("")

    L.append("## 订单状态")
    L.append("")
    if r.orders:
        L.append("| 订单 | 方向 | 标的 | 数量 | 限价 | 生效 | 状态 |")
        L.append("|---|---|---|---:|---:|---|---|")
        for o in r.orders:
            it = o.intent
            L.append(f"| {it.order_id} | {it.side} | {it.ticker} | {it.quantity} | {_usd(it.limit_price)} | "
                     f"{fmt_dual(it.effective_from)} | {o.status} |")
    else:
        L.append("无订单。")
    L.append("")

    pm = r.postmortem
    if pm:
        L.append("## 复盘校准（复盘校准 Agent）")
        L.append("")
        d = pm.decomposition
        if d:
            L.append(f"**超额来源拆分（{d['period']}）：** 相对 SPY {_usd(d['excess_vs_spy'])} = "
                     f"敞口效应 {_usd(d['exposure_effect'])}（平均现金 {d['avg_cash_weight']:.0%}）"
                     f" + 敞口之外（选股/择时/执行）{_usd(d['beyond_exposure'])}")
            L.append("")
        if pm.counterfactuals:
            L.append("| 反事实分支（不计入账本） | 期末净值 | 该分支 − 实际 | 说明 |")
            L.append("|---|---:|---:|---|")
            for c in pm.counterfactuals:
                L.append(f"| {c['name']} | {_usd(c['nav'])} | {_usd(c['vs_actual'])} | {c['note']} |")
            L.append("")
        for kind, name in (("decision", "交易决策"), ("forecast", "事件预测")):
            sm = pm.summary(kind)
            if sm["n"]:
                L.append(f"- 已到期{name} {sm['n']} 个：方向命中率 {sm['hit_rate']:.0%}，Brier {sm['brier']:.3f}"
                         f"（0.25 = 50% 基线），平均误差 {sm['mean_abs_err_pct']:.2f}pp")
        holds = [x for x in pm.scored if x.kind == "hold" and x.realized_pct is not None]
        for h in holds[-5:]:
            L.append(f"- HOLD {h.start}→{h.end}：SPY {h.realized_pct:+.2f}%，{h.note}")
        if pm.pending:
            L.append(f"- 未到期待评分：{pm.pending} 个")
        for x in pm.lessons:
            L.append(f"- 教训：{x}")
        L.append("")

    L.append("## 审计")
    L.append("")
    L.append(f"- 结果：{'通过' if a.passed else '未通过（正式净值暂停发布）'}")
    rec = a.reconciliation
    if rec:
        L.append(f"- 现金对账：登记 {_usd(rec['registered_cash'])}，逐笔推导 ${rec['derived_cash']:.6f}，差异 ${rec['difference']:.6f}")
    for x in a.blocking:
        L.append(f"- ❌ {x}")
    for x in a.warnings:
        L.append(f"- ⚠ {x}")
    for x in a.info:
        L.append(f"- ℹ {x}")
    for x in r.warnings:
        L.append(f"- ⚠ 运行警告：{x}")
    L.append("")
    L.append("---")
    L.append("本报告为模拟交易研究记录，不构成投资建议；所有提案在获得授权并按冻结规则仿真成交前均不是成交。")
    return "\n".join(L) + "\n"

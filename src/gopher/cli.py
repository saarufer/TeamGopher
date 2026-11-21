"""命令行入口：gopher <command>。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

from .agents import auditor
from .approvals import ApprovalError, approve, reject
from .clock import ET, fmt_dual, is_trading_day, market_close, market_open, next_trading_day, now_utc, parse_dt
from .config import Settings
from .event_store import EventStore, to_jsonable
from .evidence import EvidenceStore
from .execution import OrderRejected, cancel_order, create_order, reconcile
from .history import REGISTERED_SNAPSHOT_20261119, import_history
from .ledger import q2, replay
from .market_data import MarketStore, fetch_yfinance, import_csv
from .valuation import performance, value_portfolio


def _settings() -> Settings:
    return Settings()


def _caps(items: list[str] | None) -> dict[str, Decimal]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        if not v:
            raise SystemExit(f"格式应为 TICKER=金额: {it}")
        out[k.upper()] = Decimal(v)
    return out


def _end_of_day(s: str) -> datetime:
    d = date.fromisoformat(s) if len(s) == 10 else None
    return market_close(d) if d else parse_dt(s)


def cmd_init(a):
    s = _settings()
    n = import_history(s)
    st = replay(EventStore(s.events_path).load(), s.initial_cash)
    print(f"已导入 {n} 个历史事件 → {s.events_path}")
    print(f"重放现金 ${q2(st.cash)}；持仓 {', '.join(f'{t} {q}' for t, q in st.held().items())}")


def cmd_verify(a):
    s = _settings()
    store = EventStore(s.events_path)
    events = store.load()
    st = replay(events, s.initial_cash)
    rep = auditor.audit(store, events, st)
    print(f"事件数 {len(events)}；哈希链 {'完整' if not store.verify(events) else '异常'}")
    snap = REGISTERED_SNAPSHOT_20261119
    if not a.no_snapshot:
        ok_pos = all(st.positions.get(t) == q for t, q in snap["positions"].items())
        print(f"与 2026-11-19 登记快照比对：现金 {'一致' if q2(st.cash) == snap['cash'] else '不一致'}"
              f"（{q2(st.cash)} vs {snap['cash']}），持仓 {'一致' if ok_pos else '不一致'}")
    print(f"审计：{'通过' if rep.passed else '未通过'}")
    for x in rep.blocking:
        print("  ❌", x)
    for x in rep.warnings:
        print("  ⚠", x)
    for x in rep.info:
        print("  ℹ", x)
    rec = rep.reconciliation
    print(f"现金对账：登记 {rec['registered_cash']} / 逐笔推导 {rec['derived_cash']:.6f} / 差异 {rec['difference']:.6f}")
    return 0 if rep.passed else 1


def cmd_portfolio(a):
    s = _settings()
    as_of = parse_dt(a.as_of) if a.as_of else now_utc()
    events = EventStore(s.events_path).load()
    st = replay(events, s.initial_cash, as_of=as_of)
    market = MarketStore(s.bars_path, s.data_sources.get("priority"))
    v = value_portfolio(st, market.view(as_of), s)
    p = performance(v, s)
    print(f"as_of {fmt_dual(as_of)}；估值日 {v.session_date}；{'正式' if v.official else '暂估'}")
    for pv in v.positions:
        print(f"  {pv.ticker:5} {pv.quantity:>14}  @ {pv.price or '—':>10}  = {q2(pv.value) if pv.value else '—':>12}"
              f"  {f'{pv.weight:.1%}' if pv.weight is not None else ''}")
    print(f"  现金 ${q2(st.cash)}")
    if p:
        print(f"NAV ${q2(p.nav)}；SPY 基准 ${q2(p.benchmark_nav)}；绝对 {p.absolute_return:+.2%}；"
              f"SPY {p.benchmark_return:+.2%}；相对 {p.active_return_pp:+.2f}pp（${q2(p.active_pnl_usd)}）")
    for r in v.reasons:
        print("  ⚠", r)


def cmd_fetch(a):
    s = _settings()
    tickers = [t.upper() for t in a.tickers] if a.tickers else sorted(
        set(s.agents.get("research_universe", [])) | set(REGISTERED_SNAPSHOT_20261119["positions"]) | {"SPY", "BNO"})
    end = date.fromisoformat(a.end) if a.end else now_utc().astimezone(ET).date()
    start = date.fromisoformat(a.start) if a.start else date(2026, 8, 1)
    market = MarketStore(s.bars_path, s.data_sources.get("priority"))
    delay = int(s.data_sources.get("daily_bar_available_delay_minutes", 15))
    bars = fetch_yfinance(tickers, start, end, s.data_dir / "raw", delay, a.interval)
    n = market.add(bars)
    market.save()
    print(f"抓取 {len(bars)} 根 K 线，新增 {n} 根（{a.interval}，{start}→{end}）→ {s.bars_path}")


def cmd_import_prices(a):
    s = _settings()
    market = MarketStore(s.bars_path, s.data_sources.get("priority"))
    bars = import_csv(a.path, a.source, a.source_type)
    print(f"导入 {market.add(bars)} 根日线")
    market.save()


def cmd_run(a):
    from .orchestrator import run_daily
    s = _settings()
    res = run_daily(s, as_of=parse_dt(a.as_of) if a.as_of else None, engine=a.engine, web=a.web,
                    record=not a.dry_run)
    from .reporting import render_daily
    print(render_daily(res))
    if res.report_path:
        print(f"日报已保存：{res.report_path}（report_hash {res.report_hash[:12]}…）")


def cmd_meeting(a):
    s = _settings()
    store = EventStore(s.events_path)
    st = replay(store.load(), s.initial_cash)
    if a.action == "list":
        for mid, m in st.meetings.items():
            if a.all or m["status"] == "OPEN":
                print(f"{mid}  {m['status']:9} {m.get('summary', '')}")
        return
    m = st.meetings.get(a.meeting_id)
    if not m:
        raise SystemExit(f"会议不存在: {a.meeting_id}")
    if a.action == "show":
        print(json.dumps(to_jsonable(m), ensure_ascii=False, indent=2))
        return
    try:
        if a.action == "approve":
            ap = approve(store, s.initial_cash, a.meeting_id, tickers_caps_usd=_caps(a.caps),
                         sides=[x.upper() for x in a.sides], valid_until=_end_of_day(a.valid_until),
                         execution_rules=a.rules, budget_usd=Decimal(a.budget) if a.budget else None,
                         candidate_index=a.candidate, competition_id=s.active_competition_id)
            print(f"已登记授权 {ap}")
        elif a.action == "reject":
            reject(store, s.initial_cash, a.meeting_id, a.reason, competition_id=s.active_competition_id)
            print("已登记拒绝")
    except ApprovalError as e:
        raise SystemExit(f"授权失败：{e}")


def cmd_order(a):
    s = _settings()
    store = EventStore(s.events_path)
    if a.action == "list":
        st = replay(store.load(), s.initial_cash)
        for o in st.orders.values():
            it = o.intent
            print(f"{it.order_id} {it.side} {it.ticker} {it.quantity} {it.order_type} {it.limit_price} "
                  f"生效 {fmt_dual(it.effective_from)} 到期 {fmt_dual(it.expires_at)} → {o.status}")
        return
    if a.action == "cancel":
        cancel_order(store, s, a.order_id, a.reason)
        print("已撤销")
        return
    market = MarketStore(s.bars_path, s.data_sources.get("priority"))
    now = now_utc()
    if a.effective_from:
        eff = parse_dt(a.effective_from)
    else:
        d = now.astimezone(ET).date()
        if not is_trading_day(d) or now >= market_close(d):
            eff = market_open(next_trading_day(d))   # 周末 / 节假日 / 已收盘 → 下一个交易日开盘
        else:
            eff = max(market_open(d), now)           # 盘前 → 今天开盘；盘中 → 立即
    try:
        it = create_order(store, s, market, ticker=a.ticker, side=a.side.upper(), quantity=Decimal(a.quantity),
                          order_type="LIMIT" if a.limit else "MARKET",
                          limit_price=Decimal(a.limit) if a.limit else None, effective_from=eff,
                          approval_id=a.approval, decision_id=a.decision or "manual", now=now)
    except OrderRejected as e:
        raise SystemExit(f"订单被拒绝：{e}")
    print(f"已冻结订单 {it.order_id}：{it.side} {it.ticker} {it.quantity} {it.order_type} "
          f"{it.limit_price}，生效 {fmt_dual(it.effective_from)}，到期 {fmt_dual(it.expires_at)}")


def cmd_reconcile(a):
    s = _settings()
    if a.as_of and parse_dt(a.as_of) > now_utc():
        raise SystemExit("--as-of 不能晚于当前时间：不得预先写入未来事件")
    log = reconcile(EventStore(s.events_path), s, MarketStore(s.bars_path, s.data_sources.get("priority")),
                    EvidenceStore(s.evidence_path), parse_dt(a.as_of) if a.as_of else None)
    print("\n".join(log) or "没有需要推进的订单")


def cmd_evidence(a):
    s = _settings()
    ev = EvidenceStore(s.evidence_path)
    if a.action == "add":
        now = now_utc()
        e = ev.add(source_url=a.url, retrieved_at=now,
                   available_at=parse_dt(a.available_at) if a.available_at else now,
                   published_at=parse_dt(a.published_at) if a.published_at else None,
                   source_type=a.source_type, reliability=a.reliability, content={"summary": a.summary, "url": a.url},
                   title=a.title, summary=a.summary, claim_type=a.claim_type,
                   tickers=[t.upper() for t in a.tickers or []])
        print(f"已登记证据 {e.evidence_id}")
    elif a.action == "show":
        e = ev.get(a.evidence_id)
        print(json.dumps(to_jsonable(e.model_dump()), ensure_ascii=False, indent=2) if e else "不存在")
    else:
        for e in ev.all()[-a.n:]:
            print(f"{e.evidence_id}  {e.source_type:8} {e.reliability:11} {e.available_at:%Y-%m-%d %H:%M}Z  {e.title}")


def cmd_event(a):
    from .agents import expectations
    s = _settings()
    store = EventStore(s.events_path)
    st = replay(store.load(), s.initial_cash)
    if a.action == "add":
        if not (a.name and a.at):
            raise SystemExit("需要 --name 和 --at，例如：--name \"美国 11 月 CPI\" --at 2026-12-10T08:30")
        key = expectations.schedule(store, name=a.name, scheduled_at=parse_dt(a.at),
                                    tickers=[t.upper() for t in (a.tickers or ["SPY"])], category=a.category,
                                    consensus=a.consensus, source_url=a.url, competition_id=s.active_competition_id)
        print(f"已登记事件 {key}（公布时间 {fmt_dual(parse_dt(a.at))}）。下次 `gopher run` 会在公布前为它冻结预测。")
    elif a.action == "actual":
        try:
            expectations.record_actual(store, st, a.event_key, a.value, now_utc(), s.active_competition_id)
        except expectations.ExpectationError as e:
            raise SystemExit(str(e))
        print("已登记实际值")
    else:
        now = now_utc()
        for e in sorted(st.scheduled_events.values(), key=lambda e: e["scheduled_at"]):
            fcs = [f for f in st.forecasts if f["event_key"] == e["event_key"]]
            act = st.event_actuals.get(e["event_key"])
            when = parse_dt(e["scheduled_at"])
            status = "已公布" if when <= now else "待公布"
            print(f"{e['event_key']:32} {fmt_dual(when)}  {status}  {e['name']}  标的 {','.join(e['tickers'])}"
                  f"  预测 {len(fcs)} 个" + (f"  实际：{act['actual_after_release']}" if act else ""))
            for f in fcs:
                print(f"    └ {f['ticker']} 上涨概率 {f['p_up']:.0%} 期望 {f['expected_return_pct']:+.2f}%  方案：{f['conditional_plan']}")


def cmd_review(a):
    from .agents import postmortem
    from .valuation import nav_series
    s = _settings()
    as_of = parse_dt(a.as_of) if a.as_of else now_utc()
    events = EventStore(s.events_path).load()
    market = MarketStore(s.bars_path, s.data_sources.get("priority"))
    series = nav_series(events, market, s, as_of)
    if not series:
        raise SystemExit("没有行情，先运行 gopher fetch")
    pm = postmortem.run(s, events, market.view(as_of), as_of, series)
    d = pm.decomposition
    print(f"复盘区间 {d['period']}")
    print(f"相对 SPY {d['excess_vs_spy']:+,.2f} = 敞口效应 {d['exposure_effect']:+,.2f}（平均现金 {d['avg_cash_weight']:.0%}）"
          f" + 敞口之外 {d['beyond_exposure']:+,.2f}")
    print("\n反事实（不计入账本）：")
    for c in pm.counterfactuals:
        print(f"  {c['name']:14} {c['nav']:>12,.2f}   该分支−实际 {c['vs_actual']:+10,.2f}   {c['note']}")
    for kind, name in (("decision", "交易决策"), ("forecast", "事件预测"), ("hold", "HOLD")):
        sm = pm.summary(kind)
        if sm["n"]:
            hr = f"命中率 {sm['hit_rate']:.0%}，" if sm["hit_rate"] is not None else ""
            br = f"Brier {sm['brier']:.3f}，" if sm["brier"] is not None else ""
            print(f"{name}：已到期 {sm['n']} 个，{hr}{br}")
    print(f"未到期 {pm.pending} 个")
    for x in pm.lessons:
        print("  教训：", x)


def main(argv=None):
    p = argparse.ArgumentParser(prog="gopher", description="地鼠团队：可审计的多 Agent 投资决策系统（仅模拟交易）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="将第 6 节历史流水导入不可变事件日志（仅一次）").set_defaults(fn=cmd_init)
    v = sub.add_parser("verify", help="校验哈希链、重放账本并审计")
    v.add_argument("--no-snapshot", action="store_true")
    v.set_defaults(fn=cmd_verify)

    pf = sub.add_parser("portfolio", help="按指定时间重放账本并估值")
    pf.add_argument("--as-of")
    pf.set_defaults(fn=cmd_portfolio)

    f = sub.add_parser("fetch", help="从 yfinance 抓取未复权行情")
    f.add_argument("tickers", nargs="*")
    f.add_argument("--start")
    f.add_argument("--end")
    f.add_argument("--interval", default="1d", choices=["1d", "1m"])
    f.set_defaults(fn=cmd_fetch)

    ip = sub.add_parser("import-prices", help="导入手动日线 CSV（date,ticker,open,high,low,close[,volume]）")
    ip.add_argument("path")
    ip.add_argument("--source", required=True)
    ip.add_argument("--source-type", default="manual")
    ip.set_defaults(fn=cmd_import_prices)

    r = sub.add_parser("run", help="运行每日多 Agent 研究流程并生成中文日报")
    r.add_argument("--as-of", help="data_cutoff（回测用）；默认当前时间")
    r.add_argument("--engine", choices=["anthropic", "offline"])
    r.add_argument("--web", action="store_true", help="启用联网新闻检索（仅实时运行）")
    r.add_argument("--dry-run", action="store_true", help="不写入事件日志与报告")
    r.set_defaults(fn=cmd_run)

    m = sub.add_parser("meeting", help="地鼠会议：list/show/approve/reject")
    m.add_argument("action", choices=["list", "show", "approve", "reject"])
    m.add_argument("meeting_id", nargs="?")
    m.add_argument("--all", action="store_true")
    m.add_argument("--candidate", type=int)
    m.add_argument("--caps", nargs="+", help="TICKER=USD 上限，例如 XLV=3000")
    m.add_argument("--budget")
    m.add_argument("--sides", nargs="+", default=["BUY"])
    m.add_argument("--valid-until", help="YYYY-MM-DD（当日收盘）或 ISO 时间")
    m.add_argument("--rules", default="当日限价单，不自动滚动")
    m.add_argument("--reason", default="")
    m.set_defaults(fn=cmd_meeting)

    o = sub.add_parser("order", help="创建/查看/撤销冻结订单")
    o.add_argument("action", choices=["create", "list", "cancel"])
    o.add_argument("order_id", nargs="?")
    o.add_argument("--ticker")
    o.add_argument("--side", default="BUY")
    o.add_argument("--quantity")
    o.add_argument("--limit")
    o.add_argument("--approval")
    o.add_argument("--decision")
    o.add_argument("--effective-from")
    o.add_argument("--reason", default="用户撤销")
    o.set_defaults(fn=cmd_order)

    rc = sub.add_parser("reconcile", help="按行情推进订单状态机（激活/成交/待核实/到期）")
    rc.add_argument("--as-of")
    rc.set_defaults(fn=cmd_reconcile)

    e = sub.add_parser("evidence", help="证据库：list/add/show")
    e.add_argument("action", choices=["list", "add", "show"])
    e.add_argument("evidence_id", nargs="?")
    e.add_argument("--url")
    e.add_argument("--title", default="")
    e.add_argument("--summary", default="")
    e.add_argument("--published-at")
    e.add_argument("--available-at")
    e.add_argument("--source-type", default="news")
    e.add_argument("--reliability", default="provisional")
    e.add_argument("--claim-type", default="fact")
    e.add_argument("--tickers", nargs="*")
    e.add_argument("-n", type=int, default=20)
    e.set_defaults(fn=cmd_evidence)

    ev = sub.add_parser("event", help="事件日历：list/add/actual（事件预期 Agent 会在公布前冻结预测）")
    ev.add_argument("action", choices=["list", "add", "actual"])
    ev.add_argument("event_key", nargs="?")
    ev.add_argument("--name")
    ev.add_argument("--at", help="公布时间，美东，例如 2026-12-10T08:30")
    ev.add_argument("--tickers", nargs="*")
    ev.add_argument("--category", default="macro", choices=["macro", "earnings", "policy", "ipo", "other"])
    ev.add_argument("--consensus", default="", help="事前市场共识，例如 \"CPI 同比 2.9%%\"")
    ev.add_argument("--url", default="")
    ev.add_argument("--value", help="actual：公布后的实际值")
    ev.set_defaults(fn=cmd_event)

    rv = sub.add_parser("review", help="复盘校准：决策打分、反事实、超额来源拆分")
    rv.add_argument("--as-of")
    rv.set_defaults(fn=cmd_review)

    a = p.parse_args(argv)
    rc_ = a.fn(a)
    sys.exit(rc_ or 0)


if __name__ == "__main__":
    main()

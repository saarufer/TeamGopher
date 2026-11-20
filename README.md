# 地鼠团队 Gopher Investment Agents (GIA)

可审计的多 Agent 美股投资研究系统，按 `Gopher_Investment_Multi_Agent_Framework.md` 实现。
**仅模拟交易**：不连接券商，不下真实订单，不构成投资建议。

> 大胆提出假设，保守认定事实；主动配置风险，严格控制权限；允许预测错误，绝不允许事后改账。

## 快速开始

```bash
uv sync --all-extras          # 安装依赖（llm = anthropic，data = yfinance）
uv run gopher init            # 将 §6 历史流水导入不可变事件日志（只能执行一次）
uv run gopher verify          # 哈希链 + 账本重放 + 审计；应得到现金 $66,010.76 与 7 只持仓
uv run gopher fetch           # 抓取未复权日线（yfinance）
uv run gopher portfolio --as-of 2026-11-19T17:00:00
uv run gopher run --engine offline --dry-run   # 离线规则基线，不写日志
uv run gopher run             # Claude 多 Agent 研究（需要凭证），记录决策并生成日报
uv run gopher run --web       # 额外联网检索新闻，作为 provisional 证据入库
uv run pytest                 # §12 验收测试
```

Claude 凭证：设置 `ANTHROPIC_API_KEY`，或 `ant auth login`。没有凭证时会自动退回离线规则基线。
模型等参数在 `config/agents.yaml`（默认 `claude-opus-5`，adaptive thinking，结构化输出，服务端 refusal fallback）。

## 每日流程

```
数据(PIT) → 审计 → 估值/SPY 基准 → 行业轮动(量化) → [新闻/事实核验] → Strategist → Red Team → Risk → 会议门控 → 不可变记录 → 中文日报
```

每个候选的门控结果：

| 结果 | 含义 |
|---|---|
| `BLOCKED` | 校验失败（证据不存在、概率之和 ≠ 1、超出覆盖范围、现金不足、做空等） |
| `NO_EDGE` | 动作期望优势低于 `min_expected_edge_pct` |
| `MEETING_REQUIRED` | 触发会议（单股 ≥15% NAV、现金变化 ≥15pp、没有覆盖的授权、Red Team 建议否决） |
| `AUTHORIZED_BY_EXISTING_APPROVAL` | 已有有效授权覆盖（shadow 模式下仍然不自动下单） |

HOLD / NO_TRADE 也记录为决策（文档 §15 方向 B）。

## 地鼠会议 → 授权 → 订单 → 执行

```bash
uv run gopher meeting list
uv run gopher meeting show <meeting_id>
uv run gopher meeting approve <meeting_id> --candidate 0 --caps XLV=3000 --valid-until 2026-11-27
uv run gopher meeting reject <meeting_id> --reason "..."
uv run gopher order create --ticker XLV --quantity 16 --limit 180 --approval <approval_id>
uv run gopher fetch XLV --interval 1m          # 分钟线（B 级证据，仅近 7 日可得）
uv run gopher reconcile                         # 激活 / 成交 / 待核实 / 到期
```

- 会议有多个方案时，不指定 `--candidate` 不构成批准（“支持”≠ 批准）。
- 订单不得回填（`effective_from ≥ 创建时间`），当日有效，不自动滚动到下一日。
- 限价单只有在**生效之后**的分钟线触价时才算 `FILLED`（按限价成交，B 级）；只有日线 low ≤ limit 时只能标记 `ELIGIBLE_UNVERIFIED`（C 级），3 天宽限期内补不到分钟证据就 `EXPIRED`。
- 历史 $20k 非科技授权的有效期没有登记，所以不能用于新订单；需要重新开会授权。

## 设计要点

- **事件溯源**：`data/events/ledger.jsonl` 只追加，SHA-256 哈希链；现金与持仓只能由重放得到。更正只能追加（`CashAdjustmentRecorded` / `CorrectionAppended`）。
- **两套现金**：登记现金（历史原值 + 追加调整）与逐笔 `数量×价格` 推导现金并行，差异在审计中显示（10/19 的 $0.01 舍入差异保持可见）。
- **Point-in-time**：每根 K 线和每条证据都带 `available_at`；日线收盘 15 分钟后才算可得。所有决策只能通过 `PointInTimeView` 读数据。
- **LLM 只提议**：Strategist / Red Team 输出经 JSON Schema 约束，代码负责校验：引用不存在的证据会被剔除；期望收益由代码计算；新闻 URL 必须出现在本次真实的 web_search 结果中，否则丢弃。
- **确定性风控与审计**：Risk Manager、Auditor、Reporter 都是确定性代码。审计不通过、价格源冲突（>0.5%）或价格口径混用时，净值只能标记为“暂估”。

## 目录

```
config/        competition / risk_limits / data_sources / agents
data/imports/  历史流水 CSV 与历史审批 YAML（Phase 0 冻结）
data/events/   不可变事件日志
data/evidence/ 证据库（只追加）
data/raw/      原始行情快照（不覆盖）
data/normalized/bars.csv  清洗后的行情（多来源并存）
prompts/       strategist / red_team / news 系统提示词
src/gopher/    内核：event_store, ledger, history, market_data, valuation, risk, approvals, execution, reporting, orchestrator, cli
src/gopher/agents/  llm, rotation, strategist, red_team, auditor, news
reports/YYYY-MM-DD/  日报（含 report_hash）
tests/         §12 验收测试
```

## 尚未实现 / 待确认

- 原比赛截止日期（`config/competition.yaml` 中 `end_date: null`），以及延长赛的起始日期。
- Macro / Company / Event-Expectations Agent 目前合并在 Strategist 的上下文与新闻检索里，还没有单独的情景树数据库与概率校准（Brier score）。
- FastAPI / Streamlit 面板（Phase 5）、walk-forward 回测（Phase 4）。
- NYSE 休市日写在 `clock.py` 里，需要每年对照交易所官方日历复核。

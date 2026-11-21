"""Market Regime Agent（新增）：用 point-in-time 价格判断市场处于 risk_on / neutral / risk_off。

这是 §3.3 Macro Agent 的轻量替代：不依赖经济数据源，只用趋势、波动、回撤和行业广度。
输出供仓位管理 Agent 决定目标现金区间，也写进 Strategist 的上下文。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from ..market_data import PointInTimeView

SECTORS = ["XLK", "XLV", "XLF", "XLI", "XLE", "XLP", "XLU", "XLY", "XLC", "XLB", "XLRE"]


@dataclass
class Regime:
    label: str                      # risk_on | neutral | risk_off | unknown
    score: int                      # -4..+4，越高越偏 risk_on
    spy_vs_sma50: float | None
    sma20_slope_5d: float | None
    vol20_ann: float | None
    drawdown_60d: float | None
    breadth_above_sma20: float | None
    rsp_minus_spy_20d: float | None
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def assess(view: PointInTimeView, benchmark: str = "SPY") -> Regime:
    spy = [float(p) for _, p in view.close_series(benchmark, 80)]
    if len(spy) < 25:
        return Regime("unknown", 0, None, None, None, None, None, None, ["行情不足 25 个交易日，无法判断市场状态"])
    last = spy[-1]
    sma50 = _sma(spy, 50)
    sma20_now, sma20_prev = _sma(spy, 20), _sma(spy[:-5], 20)
    rets = [spy[i] / spy[i - 1] - 1 for i in range(len(spy) - 20, len(spy))]
    m = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)
    peak = max(spy[-60:])
    dd = last / peak - 1

    above = total = 0
    for t in SECTORS:
        ser = [float(p) for _, p in view.close_series(t, 20)]
        if len(ser) == 20:
            total += 1
            above += ser[-1] > sum(ser) / 20
    breadth = above / total if total else None

    rsp = [float(p) for _, p in view.close_series("RSP", 21)]
    rsp_rel = (rsp[-1] / rsp[0] - spy[-1] / spy[-21]) if len(rsp) == 21 and len(spy) >= 21 else None

    score, why = 0, []
    vs50 = last / sma50 - 1 if sma50 else None
    if vs50 is not None:
        score += 1 if vs50 > 0 else -1
        why.append(f"SPY {'高于' if vs50 > 0 else '低于'} 50 日均线 {vs50:+.1%}")
    slope = sma20_now / sma20_prev - 1 if sma20_now and sma20_prev else None
    if slope is not None:
        score += 1 if slope > 0 else -1
        why.append(f"20 日均线 5 日斜率 {slope:+.2%}")
    if vol < 0.15:
        score += 1
        why.append(f"20 日年化波动 {vol:.0%}（平静）")
    elif vol > 0.25:
        score -= 1
        why.append(f"20 日年化波动 {vol:.0%}（紧张）")
    else:
        why.append(f"20 日年化波动 {vol:.0%}（正常）")
    if breadth is not None:
        if breadth >= 0.6:
            score += 1
        elif breadth <= 0.4:
            score -= 1
        why.append(f"行业广度：{above}/{total} 个行业 ETF 在 20 日均线上方")
    if dd < -0.08:
        score -= 1
        why.append(f"距 60 日高点回撤 {dd:.1%}")
    label = "risk_on" if score >= 2 else "risk_off" if score <= -2 else "neutral"
    return Regime(label, score, vs50, slope, vol, dd, breadth, rsp_rel, why)


LABEL_CN = {"risk_on": "偏进攻", "neutral": "中性", "risk_off": "偏防守", "unknown": "未知"}

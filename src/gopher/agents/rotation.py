"""Industry Rotation Agent（§3.4）：确定性量化特征，只读 point-in-time 行情。"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from ..market_data import PointInTimeView


@dataclass
class TickerFeatures:
    ticker: str
    last_date: date
    last_close: float
    ret: dict[int, float | None]          # 1/5/10/20 日收益
    rel_spy: dict[int, float | None]      # 相对 SPY
    vol20_ann: float | None               # 20 日年化波动
    above_sma20: bool | None
    sma20_gap: float | None
    avg_volume20: float | None
    beta60: float | None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["last_date"] = self.last_date.isoformat()
        return d


WINDOWS = (1, 5, 10, 20)


def _ret(series: list[float], n: int) -> float | None:
    if len(series) <= n or series[-n - 1] == 0:
        return None
    return series[-1] / series[-n - 1] - 1


def _beta(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 40:
        return None
    ra = [a[i] / a[i - 1] - 1 for i in range(len(a) - n + 1, len(a))]
    rb = [b[i] / b[i - 1] - 1 for i in range(len(b) - n + 1, len(b))]
    mb = sum(rb) / len(rb)
    ma = sum(ra) / len(ra)
    var = sum((x - mb) ** 2 for x in rb)
    if var == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / var


def compute(view: PointInTimeView, universe: list[str], benchmark: str = "SPY") -> list[TickerFeatures]:
    bench = [float(p) for _, p in view.close_series(benchmark, 61)]
    out = []
    for t in universe:
        ser = view.close_series(t, 61)
        if len(ser) < 2:
            continue
        closes = [float(p) for _, p in ser]
        rets = {n: _ret(closes, n) for n in WINDOWS}
        rel = {n: (None if rets[n] is None or _ret(bench, n) is None else rets[n] - _ret(bench, n)) for n in WINDOWS}
        last20 = closes[-21:]
        daily = [last20[i] / last20[i - 1] - 1 for i in range(1, len(last20))]
        vol = None
        if len(daily) >= 10:
            m = sum(daily) / len(daily)
            vol = math.sqrt(sum((x - m) ** 2 for x in daily) / (len(daily) - 1)) * math.sqrt(252)
        sma = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 20 else None
        vols = [b.volume for d in [x for x, _ in ser[-20:]] for b in view.daily(t, d)[:1]]
        out.append(TickerFeatures(
            t, ser[-1][0], closes[-1], rets, rel, vol,
            None if sma is None else closes[-1] > sma,
            None if sma is None else closes[-1] / sma - 1,
            float(sum(vols) / len(vols)) if vols else None,
            _beta(closes, bench) if t != benchmark else 1.0,
        ))
    return out


def rank(features: list[TickerFeatures]) -> list[TickerFeatures]:
    """综合相对强度分（10 日与 20 日相对 SPY 的均值）。仅作描述，不推导明日上涨。"""
    def score(f: TickerFeatures) -> float:
        vals = [v for v in (f.rel_spy.get(10), f.rel_spy.get(20)) if v is not None]
        return sum(vals) / len(vals) if vals else float("-inf")
    return sorted(features, key=score, reverse=True)

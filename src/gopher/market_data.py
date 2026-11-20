"""行情存储与 point-in-time 访问（§3.1、§7.4）。

每根 K 线带 available_at（首次可得时间）。回测/决策只能通过 PointInTimeView 读取，
保证 available_at <= data_cutoff；冲突来源保存全部候选值而非覆盖。
"""

from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .clock import ET, ensure_aware, market_close, now_utc, parse_dt

FIELDS = ["source", "source_type", "ticker", "interval", "start", "end", "open", "high", "low", "close",
          "volume", "adjustment_type", "currency", "available_at", "retrieved_at", "quality_flag"]


@dataclass(frozen=True)
class Bar:
    source: str
    source_type: str
    ticker: str
    interval: str          # "1d" | "1m"
    start: datetime        # UTC
    end: datetime          # UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    adjustment_type: str   # unadjusted | split_adjusted | total_return_adjusted
    currency: str
    available_at: datetime
    retrieved_at: datetime
    quality_flag: str = "ok"

    @property
    def session_date(self) -> date:
        return self.start.astimezone(ET).date()

    def key(self) -> tuple:
        return (self.source, self.ticker, self.interval, self.start)

    def to_row(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
            elif isinstance(v, Decimal):
                d[k] = str(v)
        return d

    @classmethod
    def from_row(cls, r: dict) -> "Bar":
        return cls(
            source=r["source"], source_type=r["source_type"], ticker=r["ticker"], interval=r["interval"],
            start=parse_dt(r["start"]), end=parse_dt(r["end"]),
            open=Decimal(r["open"]), high=Decimal(r["high"]), low=Decimal(r["low"]), close=Decimal(r["close"]),
            volume=Decimal(r["volume"] or "0"), adjustment_type=r["adjustment_type"], currency=r["currency"],
            available_at=parse_dt(r["available_at"]), retrieved_at=parse_dt(r["retrieved_at"]),
            quality_flag=r.get("quality_flag") or "ok",
        )


def daily_bar(source: str, ticker: str, d: date, o, h, l, c, v=0, *, source_type: str = "vendor",
              retrieved_at: datetime | None = None, delay_minutes: int = 15,
              adjustment_type: str = "unadjusted") -> Bar:
    close_dt = market_close(d)
    return Bar(source, source_type, ticker, "1d", parse_dt(f"{d.isoformat()}T09:30:00"), close_dt,
               Decimal(str(o)), Decimal(str(h)), Decimal(str(l)), Decimal(str(c)), Decimal(str(v)),
               adjustment_type, "USD", close_dt + timedelta(minutes=delay_minutes),
               retrieved_at or now_utc())


def minute_bar(source: str, ticker: str, start: datetime, o, h, l, c, v, *, source_type: str = "vendor",
               retrieved_at: datetime | None = None) -> Bar:
    start = ensure_aware(start)
    end = start + timedelta(minutes=1)
    return Bar(source, source_type, ticker, "1m", start, end, Decimal(str(o)), Decimal(str(h)),
               Decimal(str(l)), Decimal(str(c)), Decimal(str(v)), "unadjusted", "USD", end,
               retrieved_at or now_utc())


class MarketStore:
    def __init__(self, path: Path | None = None, priority: dict[str, int] | None = None):
        self.path = Path(path) if path else None
        self.priority = priority or {"exchange": 0, "filing": 1, "official": 2, "vendor": 3, "manual": 4}
        self.bars: dict[tuple, Bar] = {}
        if self.path and self.path.exists():
            with self.path.open(encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    b = Bar.from_row(r)
                    self.bars[b.key()] = b

    def add(self, bars: list[Bar]) -> int:
        """新增 K 线；同源同键已存在则保留原值（不覆盖），返回新增数量。"""
        n = 0
        for b in bars:
            if b.key() not in self.bars:
                self.bars[b.key()] = b
                n += 1
        return n

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for b in sorted(self.bars.values(), key=lambda b: (b.ticker, b.interval, b.start, b.source)):
                w.writerow(b.to_row())
        os.replace(tmp, self.path)  # 原子替换，避免写到一半时文件被截断

    def view(self, as_of: datetime) -> "PointInTimeView":
        return PointInTimeView(self, ensure_aware(as_of))


@dataclass
class PriceQuote:
    ticker: str
    session_date: date
    price: Decimal
    source: str
    available_at: datetime
    candidates: dict[str, Decimal]
    conflict: bool
    adjustment_type: str


class PointInTimeView:
    """只暴露 available_at <= as_of 的数据。"""

    def __init__(self, store: MarketStore, as_of: datetime):
        self.store = store
        self.as_of = as_of

    def _visible(self, ticker: str, interval: str) -> list[Bar]:
        return [b for b in self.store.bars.values()
                if b.ticker == ticker and b.interval == interval and b.available_at <= self.as_of]

    def daily(self, ticker: str, d: date) -> list[Bar]:
        return [b for b in self._visible(ticker, "1d") if b.session_date == d]

    def minutes(self, ticker: str, start: datetime, end: datetime) -> list[Bar]:
        start, end = ensure_aware(start), ensure_aware(end)
        return sorted((b for b in self._visible(ticker, "1m") if start <= b.start and b.end <= end),
                      key=lambda b: b.start)

    def daily_dates(self, ticker: str) -> list[date]:
        return sorted({b.session_date for b in self._visible(ticker, "1d")})

    def close(self, ticker: str, d: date, conflict_threshold: Decimal = Decimal("0.005")) -> PriceQuote | None:
        bars = self.daily(ticker, d)
        if not bars:
            return None
        bars.sort(key=lambda b: (self.store.priority.get(b.source_type, 9), b.source))
        best = bars[0]
        cands = {b.source: b.close for b in bars}
        lo, hi = min(cands.values()), max(cands.values())
        conflict = lo > 0 and (hi - lo) / lo > conflict_threshold
        return PriceQuote(ticker, d, best.close, best.source, best.available_at, cands, conflict,
                          best.adjustment_type)

    def latest_close(self, ticker: str, conflict_threshold: Decimal = Decimal("0.005")) -> PriceQuote | None:
        dates = self.daily_dates(ticker)
        return self.close(ticker, dates[-1], conflict_threshold) if dates else None

    def close_series(self, ticker: str, n: int | None = None) -> list[tuple[date, Decimal]]:
        dates = self.daily_dates(ticker)
        if n:
            dates = dates[-n:]
        out = []
        for d in dates:
            q = self.close(ticker, d)
            if q:
                out.append((d, q.price))
        return out

    def adjustment_types(self, tickers: list[str]) -> set[str]:
        return {b.adjustment_type for t in tickers for b in self._visible(t, "1d")}


def snapshot_hash(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(repr(sorted(r.items())).encode())
    return h.hexdigest()


# ---------- 数据接入 ----------

def fetch_yfinance(tickers: list[str], start: date, end: date, raw_dir: Path, delay_minutes: int = 15,
                   interval: str = "1d") -> list[Bar]:
    """从 yfinance 抓取未复权日线（或 1m 分钟线，仅近 7 日可用）。原始快照写入 data/raw，不覆盖。"""
    try:
        import yfinance as yf
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("需要安装可选依赖：uv sync --extra data") from e
    retrieved = now_utc()
    raw_dir = Path(raw_dir) / "yfinance"
    raw_dir.mkdir(parents=True, exist_ok=True)
    bars: list[Bar] = []
    for t in tickers:
        df = yf.Ticker(t).history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                  interval=interval, auto_adjust=False, actions=False)
        if df is None or df.empty:
            continue
        raw_path = raw_dir / f"{t}_{interval}_{retrieved:%Y%m%dT%H%M%SZ}.csv"
        df.to_csv(raw_path)
        for idx, r in df.iterrows():
            ts = idx.to_pydatetime()
            r = {k: (round(float(r[k]), 4) if k != "Volume" else int(r[k])) for k in ("Open", "High", "Low", "Close", "Volume")}
            if interval == "1d":
                d = ts.astimezone(ET).date() if ts.tzinfo else ts.date()
                if d >= retrieved.astimezone(ET).date() and retrieved < market_close(d):
                    continue  # 未收盘的当日不形成日线
                bars.append(daily_bar("yfinance", t, d, r["Open"], r["High"], r["Low"], r["Close"],
                                      r["Volume"], retrieved_at=retrieved, delay_minutes=delay_minutes))
            else:
                bars.append(minute_bar("yfinance", t, ts, r["Open"], r["High"], r["Low"], r["Close"],
                                       r["Volume"], retrieved_at=retrieved))
    return bars


def import_csv(path: Path, source: str, source_type: str = "manual", delay_minutes: int = 15) -> list[Bar]:
    """手动导入日线 CSV：列 date,ticker,open,high,low,close[,volume]。"""
    retrieved = now_utc()
    out = []
    with Path(path).open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append(daily_bar(source, r["ticker"].upper(), date.fromisoformat(r["date"]), r["open"], r["high"],
                                 r["low"], r["close"], r.get("volume") or 0, source_type=source_type,
                                 retrieved_at=retrieved, delay_minutes=delay_minutes))
    return out

import math
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from gopher.clock import is_trading_day
from gopher.config import Settings
from gopher.event_store import EventStore
from gopher.history import import_history
from gopher.market_data import MarketStore, daily_bar

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path) -> Settings:
    for d in ("config", "prompts"):
        shutil.copytree(ROOT / d, tmp_path / d)
    (tmp_path / "data" / "imports").mkdir(parents=True)
    for f in (ROOT / "data" / "imports").iterdir():
        shutil.copy(f, tmp_path / "data" / "imports" / f.name)
    return Settings(root=tmp_path)


@pytest.fixture
def store(settings) -> EventStore:
    s = EventStore(settings.events_path)
    import_history(settings, s)
    return s


def trading_days(start: date, end: date):
    d = start
    while d <= end:
        if is_trading_day(d):
            yield d
        d += timedelta(days=1)


def synth_market(settings, tickers=None, end=date(2026, 11, 19)) -> MarketStore:
    """确定性合成行情：SPY 在 2026-10-02 收于 769.64。"""
    tickers = tickers or ["SPY", "QQQ", "NVDA", "XLE", "XLI", "XLF", "XLV", "XLU", "XLP", "XLK", "RSP", "IWM", "BNO"]
    base = {"SPY": 769.64, "QQQ": 760.0, "NVDA": 240.0, "XLE": 65.0, "XLI": 170.0, "XLF": 57.0, "XLV": 184.0,
            "XLU": 80.0, "XLP": 78.0, "XLK": 280.0, "RSP": 190.0, "IWM": 240.0, "BNO": 63.0}
    drift = {"XLU": 0.002, "XLP": 0.0015, "NVDA": -0.004, "SPY": -0.001}
    m = MarketStore(settings.bars_path)
    days = list(trading_days(date(2026, 8, 3), end))
    anchor = days.index(date(2026, 10, 2))
    bars = []
    for t in tickers:
        for i, d in enumerate(days):
            k = i - anchor
            px = base[t] * math.exp(drift.get(t, 0.0) * k + 0.01 * math.sin(k * 0.7 + len(t)))
            if t == "SPY" and k == 0:
                px = 769.64
            px = round(px, 2)
            bars.append(daily_bar("synthetic", t, d, px, round(px * 1.01, 2), round(px * 0.99, 2), px, 1_000_000))
    m.add(bars)
    m.save()
    return m

"""时间规则（§7.4）：统一存 UTC，显示转换 America/New_York 与 Europe/Berlin；按 IANA 时区计算夏令时。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc

# NYSE 休市日（需每年对照交易所官方日历复核）
NYSE_HOLIDAYS: set[date] = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}
# 提前收盘（13:00 ET）
NYSE_EARLY_CLOSE: set[date] = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


def now_utc() -> datetime:
    return datetime.now(UTC)


def ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"时间戳必须带时区: {dt!r}")
    return dt.astimezone(UTC)


def parse_dt(value: str | datetime) -> datetime:
    """解析 ISO 时间；无时区的字符串一律按美东解释（并显式转换为 UTC）。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(UTC)


def et(d: date, t: time) -> datetime:
    """美东本地时间 → UTC。"""
    return datetime.combine(d, t, tzinfo=ET).astimezone(UTC)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def market_open(d: date) -> datetime:
    return et(d, REGULAR_OPEN)


def market_close(d: date) -> datetime:
    return et(d, EARLY_CLOSE if d in NYSE_EARLY_CLOSE else REGULAR_CLOSE)


def market_session(dt: datetime) -> str:
    local = ensure_aware(dt).astimezone(ET)
    d = local.date()
    if not is_trading_day(d):
        return "closed"
    t = local.time()
    close_t = EARLY_CLOSE if d in NYSE_EARLY_CLOSE else REGULAR_CLOSE
    if time(4, 0) <= t < REGULAR_OPEN:
        return "premarket"
    if REGULAR_OPEN <= t < close_t:
        return "regular"
    if close_t <= t < time(20, 0):
        return "afterhours"
    return "closed"


def previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def last_completed_session(as_of: datetime, delay_minutes: int = 0) -> date:
    """as_of 时点已收盘（含可得延迟）的最近交易日。"""
    local = ensure_aware(as_of).astimezone(ET)
    d = local.date()
    if is_trading_day(d) and as_of >= market_close(d) + timedelta(minutes=delay_minutes):
        return d
    return previous_trading_day(d)


def fmt_dual(dt: datetime) -> str:
    dt = ensure_aware(dt)
    return f"{dt.astimezone(ET):%Y-%m-%d %H:%M} ET / {dt.astimezone(BERLIN):%H:%M} 柏林"

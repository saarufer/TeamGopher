from decimal import Decimal

from gopher.approvals import approve, create_meeting
from gopher.clock import parse_dt


def approved(store, settings, caps=None, budget=None, valid_until="2026-12-04T16:00:00", at="2026-11-30T20:00:00",
             sides=("BUY",), candidates=None):
    caps = caps or {"XLV": Decimal("3000")}
    candidates = candidates or [{"ticker": next(iter(caps)), "action": sides[0]}]
    mid = create_meeting(store, candidates=candidates, triggers=["test"], summary="test", at=parse_dt(at))
    ap = approve(store, settings.initial_cash, mid, tickers_caps_usd=caps,
                 sides=list(sides), valid_until=parse_dt(valid_until), execution_rules="当日限价",
                 budget_usd=budget, candidate_index=0 if candidates is None or len(candidates) > 1 else None,
                 at=parse_dt(at))
    return mid, ap

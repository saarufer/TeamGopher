"""§12.1 不可变账本。"""

from decimal import Decimal

import pytest

from gopher.agents.auditor import audit
from gopher.event_store import EventStore, TamperError
from gopher.history import REGISTERED_SNAPSHOT_20261119, import_history
from gopher.ledger import q2, replay


def test_replay_reproduces_registered_cash_and_positions(store, settings):
    st = replay(store.load(), settings.initial_cash)
    assert q2(st.cash) == Decimal("66010.76")
    for t, q in REGISTERED_SNAPSHOT_20261119["positions"].items():
        assert st.positions[t] == q
    assert st.positions["BNO"] == 0
    assert len(st.held()) == 7


def test_all_cash_checkpoints_match(store, settings):
    st = replay(store.load(), settings.initial_cash)
    assert len(st.checkpoints) == 6
    assert all(cp.diff == 0 for cp in st.checkpoints)


def test_one_cent_rounding_difference_is_visible(store, settings):
    events = store.load()
    st = replay(events, settings.initial_cash)
    adj = [a for a in st.adjustments if a["reason_code"] == "ROUNDING_RECONCILIATION"]
    assert len(adj) == 1 and Decimal(adj[0]["amount"]) == Decimal("0.01")
    rep = audit(store, events, st)
    assert any("ROUNDING_RECONCILIATION" in x for x in rep.info)
    # 逐笔推导现金与登记现金的差异显式存在，不会自动消失
    assert rep.reconciliation["difference"] != 0


def test_any_char_change_is_detected(store):
    path = store.path
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[5] = lines[5].replace("778.15", "778.16") if "778.15" in lines[5] else lines[5].replace("0", "1", 1)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert store.verify()
    with pytest.raises(TamperError):
        store.append("AuditFlagRaised", {"flag_id": "x", "code": "X"})


def test_deleting_an_event_is_detected(store):
    lines = store.path.read_text(encoding="utf-8").splitlines()
    del lines[3]
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert any("seq" in i for i in store.verify())


def test_history_import_only_once(store, settings):
    with pytest.raises(RuntimeError):
        import_history(settings, store)


def test_provisional_fills_keep_open_audit_flags(store, settings):
    st = replay(store.load(), settings.initial_cash)
    codes = {f["code"] for f in st.open_flags.values()}
    assert {"PRICE_CONFLICT", "LIMIT_FILL_UNVERIFIED", "TOUCH_TIME_UNVERIFIED"} <= codes
    xlf = [f for f in st.fills if f["ticker"] == "XLF"][0]
    assert xlf["fill_confidence"] == "provisional"
    assert xlf["price"] == "56.94"  # 原登记价不被改写


def test_as_of_replay(store, settings):
    from gopher.clock import parse_dt
    st = replay(store.load(), settings.initial_cash, as_of=parse_dt("2026-10-12T17:00:00"))
    assert q2(st.cash) == Decimal("72244.82")
    assert st.positions["BNO"] == 0


from hypothesis import given, settings as hsettings, strategies as hst, HealthCheck


@hsettings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(idx=hst.integers(min_value=0, max_value=33), delta=hst.sampled_from(["0.01", "1", "-0.01"]))
def test_property_any_amount_change_detected(store, idx, delta):
    import json
    original = store.path.read_text(encoding="utf-8")
    try:
        lines = original.splitlines()
        ev = json.loads(lines[idx])
        ev["payload"]["__tamper"] = delta
        lines[idx] = json.dumps(ev, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert any("哈希不匹配" in i for i in store.verify())
    finally:
        store.path.write_text(original, encoding="utf-8")

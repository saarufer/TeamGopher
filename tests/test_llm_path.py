"""LLM 路径（用假 LLM）：结构化输出接入、防编造证据/URL、schema 严格化。"""

import json

from gopher.agents.llm import _strict_schema
from gopher.agents.news import gather_news
from gopher.clock import parse_dt
from gopher.evidence import EvidenceStore
from gopher.ledger import replay
from gopher.orchestrator import run_daily
from gopher.schemas import (Candidate, CandidateReview, DecisionProposal, HoldCounterfactual, Objection,
                            RedTeamReview, Scenario)

from conftest import synth_market


class FakeLLM:
    name = "fake"

    def __init__(self):
        self.calls = []

    def structured(self, system, user, schema, *, model=None):
        self.calls.append((schema.__name__, user))
        ctx = json.loads(user[user.find("{"):])
        if schema is DecisionProposal:
            ev = [e["evidence_id"] for e in ctx["evidence"]][:1]
            sc = [Scenario(name="bull", probability=0.3, return_pct=4, rationale="r"),
                  Scenario(name="base", probability=0.5, return_pct=1.5, rationale="r"),
                  Scenario(name="bear", probability=0.2, return_pct=-3, rationale="r")]
            return DecisionProposal(
                market_state="测试", key_risks=["k"], no_trade_reason=None,
                hold_counterfactual=HoldCounterfactual(rationale="h", cash_opportunity_cost_note="c"),
                candidates=[
                    Candidate(action="BUY", ticker="XLU", budget_usd=3000, thesis="t", evidence_ids=ev + ["ev_made_up"],
                              scenarios=sc, invalidation_conditions=["x"], horizon_days=10, confidence="medium"),
                    Candidate(action="BUY", ticker="TSLA", budget_usd=3000, thesis="t", evidence_ids=ev,
                              scenarios=sc, invalidation_conditions=["x"], horizon_days=10, confidence="low"),
                ])
        if schema is RedTeamReview:
            o = Objection(question="q", challenge="c", evidence_gap="g")
            return RedTeamReview(portfolio_level_concerns=[], reviews=[
                CandidateReview(ticker="XLU", action="BUY", objections=[o, o, o], would_flip_if="w", verdict="PROCEED")])
        raise AssertionError(schema)

    def research_with_web(self, system, user, *, max_uses=5):
        text = '```json\n{"items": [' \
               '{"url": "https://real.example/a", "title": "A", "published_at": null, "claim_type": "fact", "summary": "s", "tickers": ["SPY"]},' \
               '{"url": "https://invented.example/b", "title": "B", "published_at": null, "claim_type": "fact", "summary": "s", "tickers": []}' \
               ']}\n```'
        return text, {"https://real.example/a"}


def test_llm_pipeline_validates_and_gates(store, settings):
    synth_market(settings)
    llm = FakeLLM()
    res = run_daily(settings, as_of=parse_dt("2026-11-20T09:00:00"), engine="anthropic", llm=llm)
    assert [c[0] for c in llm.calls] == ["DecisionProposal", "RedTeamReview"]
    by = {g["candidate"].ticker: g for g in res.gated}
    assert "ev_made_up" not in by["XLU"]["candidate"].evidence_ids
    assert any("不存在" in w for w in by["XLU"]["validated"].warnings)
    assert by["TSLA"]["gate"] == "BLOCKED"           # 不在覆盖范围
    assert by["XLU"]["gate"] == "MEETING_REQUIRED"   # 无授权 → 必须开会
    st = replay(store.load(), settings.initial_cash)
    assert st.meetings[res.meeting_id]["status"] == "OPEN"
    assert not st.orders


def test_news_drops_urls_not_in_search_results(settings):
    ev = EvidenceStore(settings.evidence_path)
    items, warns = gather_news(FakeLLM(), "sys", ["SPY"], parse_dt("2026-11-20T09:00:00"), ev)
    assert [e.source_url for e in items] == ["https://real.example/a"]
    assert any("invented.example" in w for w in warns)
    assert items[0].reliability == "provisional"


def test_strict_schema_closes_all_objects():
    js = _strict_schema(DecisionProposal.model_json_schema())

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "object" and "properties" in n:
                assert n["additionalProperties"] is False
                assert set(n["required"]) == set(n["properties"])
            for k in ("minimum", "maximum", "exclusiveMinimum"):
                assert k not in n
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(js)

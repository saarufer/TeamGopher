"""Data / Fact Checker（§3.1–3.2）：联网检索新闻并入证据库。只接受本次真实检索到的 URL。"""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError

from ..clock import parse_dt
from ..evidence import EvidenceStore
from ..schemas import Evidence, NewsDigest
from .llm import LLM, LLMError, extract_json_block


def gather_news(llm: LLM, system: str, tickers: list[str], now: datetime, evidence: EvidenceStore) -> tuple[list[Evidence], list[str]]:
    user = (f"今天是 {now:%Y-%m-%d}（UTC {now:%H:%M}）。请检索与以下标的及美国宏观相关的最新重要信息：{', '.join(tickers)}。"
            "包括：FOMC/CPI/非农等宏观事件、相关公司财报或重大公告、行业轮动相关新闻。")
    text, urls = llm.research_with_web(system, user)
    warnings: list[str] = []
    try:
        digest = NewsDigest.model_validate(extract_json_block(text))
    except (LLMError, ValidationError) as e:
        return [], [f"新闻摘要解析失败: {e}"]
    out = []
    for item in digest.items:
        if item.url not in urls:
            warnings.append(f"丢弃未在检索结果中出现的 URL（疑似编造）: {item.url}")
            continue
        published = None
        if item.published_at:
            try:
                published = parse_dt(item.published_at)
            except ValueError:
                warnings.append(f"published_at 无法解析: {item.published_at}")
        if published and published > now:
            warnings.append(f"published_at 晚于当前时间，忽略该时间: {item.url}")
            published = None
        out.append(evidence.add(
            source_url=item.url, retrieved_at=now, available_at=now, published_at=published,
            source_type="news", reliability="provisional", content=item.model_dump(), title=item.title,
            summary=item.summary, claim_type=item.claim_type, tickers=[t.upper() for t in item.tickers]))
    return out, warnings

"""证据库：仅追加。每条证据带 available_at；决策只能引用 available_at <= data_cutoff 的证据。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from .clock import ensure_aware
from .event_store import canonical, sha256, to_jsonable
from .schemas import Evidence


class EvidenceStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def all(self) -> list[Evidence]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [Evidence.model_validate_json(l) for l in f if l.strip()]

    def get(self, evidence_id: str) -> Evidence | None:
        return next((e for e in self.all() if e.evidence_id == evidence_id), None)

    def visible(self, as_of: datetime) -> list[Evidence]:
        as_of = ensure_aware(as_of)
        return [e for e in self.all() if e.available_at <= as_of]

    def add(self, *, source_url: str, retrieved_at: datetime, available_at: datetime, source_type: str,
            reliability: str, content: object, published_at: datetime | None = None, title: str = "",
            summary: str = "", claim_type: str = "data", tickers: list[str] | None = None,
            evidence_id: str | None = None, persist: bool = True) -> Evidence:
        if published_at and available_at < ensure_aware(published_at):
            raise ValueError("available_at 不得早于 published_at")
        ev = Evidence(
            evidence_id=evidence_id or f"ev_{uuid.uuid4().hex[:12]}",
            source_url=source_url, retrieved_at=retrieved_at, published_at=published_at,
            available_at=available_at, source_type=source_type, reliability=reliability,
            content_hash=sha256(canonical(to_jsonable(content))), title=title, summary=summary,
            claim_type=claim_type, tickers=tickers or [],
        )
        if not persist:
            return ev
        if self.get(ev.evidence_id):
            raise ValueError(f"证据 ID 已存在: {ev.evidence_id}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(to_jsonable(ev.model_dump()), ensure_ascii=False) + "\n")
        return ev

"""配置加载。所有金额读取为 Decimal。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    env = os.environ.get("GOPHER_HOME")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for p in [here, *here.parents]:
        if (p / "config" / "competition.yaml").exists():
            return p
    return here


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass
class Settings:
    root: Path = field(default_factory=project_root)

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def events_path(self) -> Path:
        return self.data_dir / "events" / "ledger.jsonl"

    @property
    def evidence_path(self) -> Path:
        return self.data_dir / "evidence" / "evidence.jsonl"

    @property
    def bars_path(self) -> Path:
        return self.data_dir / "normalized" / "bars.csv"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"

    @cached_property
    def competition(self) -> dict:
        return _load_yaml(self.config_dir / "competition.yaml")

    @cached_property
    def risk(self) -> dict:
        return _load_yaml(self.config_dir / "risk_limits.yaml")

    @cached_property
    def data_sources(self) -> dict:
        return _load_yaml(self.config_dir / "data_sources.yaml")

    @cached_property
    def agents(self) -> dict:
        return _load_yaml(self.config_dir / "agents.yaml")

    # ---- 便捷访问 ----
    @property
    def initial_cash(self) -> Decimal:
        return Decimal(str(self.competition.get("initial_cash", "100000.00")))

    @property
    def benchmark_ticker(self) -> str:
        return self.competition.get("benchmark", {}).get("ticker", "SPY")

    @property
    def benchmark_base_close(self) -> Decimal:
        return Decimal(str(self.competition.get("benchmark", {}).get("base_close", "769.64")))

    @property
    def active_competition_id(self) -> str:
        return self.competition.get("active_competition_id", "gopher_extension_2026")

    def risk_decimal(self, key: str, default: str) -> Decimal:
        return Decimal(str(self.risk.get(key, default)))

    @property
    def etfs(self) -> set[str]:
        return set(self.risk.get("etfs", []))

    def beta(self, ticker: str) -> Decimal:
        betas = self.risk.get("assumed_beta", {})
        return Decimal(str(betas.get(ticker, self.risk.get("default_beta", "1.00"))))

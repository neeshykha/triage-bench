"""Loads and validates config.yaml, so a typo fails at startup instead of mid-run."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


class AgentSettings(BaseModel):
    model: str
    effort: Literal["low", "medium", "high", "xhigh"]
    max_tokens: int


class GeneratorSettings(AgentSettings):
    seed: int
    cases: int
    hard_share: float
    test_size: int


class Price(BaseModel):
    """USD per million tokens."""

    input: float
    output: float
    cache_write: float
    cache_read: float


class PipelineSpec(BaseModel):
    description: str
    rules: bool
    challenger: bool
    triage_model: str | None = None  # overrides agents.triage.model for this pipeline
    challenger_model: str | None = None


class RulesSettings(BaseModel):
    after_hours_start: time
    after_hours_end: time


class Settings(BaseModel):
    agents: dict[Literal["triage", "challenger"], AgentSettings]
    generator: GeneratorSettings
    max_revisions: int
    rules: RulesSettings
    pipelines: dict[str, PipelineSpec]
    prices: dict[str, Price]
    budget_ceiling_usd: float | None

    @model_validator(mode="after")
    def _every_model_has_a_price(self) -> Settings:
        models = {a.model for a in self.agents.values()} | {self.generator.model}
        for spec in self.pipelines.values():
            models |= {m for m in (spec.triage_model, spec.challenger_model) if m}
        missing = sorted(models - self.prices.keys())
        if missing:
            raise ValueError(f"no price in config.yaml for: {', '.join(missing)}")
        return self


def load_settings(path: Path = DEFAULT_PATH) -> Settings:
    raw = yaml.safe_load(path.read_text())
    return Settings(
        agents=raw["agents"],
        generator=raw["generator"],
        max_revisions=raw["loop"]["max_revisions"],
        rules=raw["rules"],
        pipelines=raw["pipelines"],
        prices=raw["prices"],
        budget_ceiling_usd=raw["budget"]["ceiling_usd"],
    )

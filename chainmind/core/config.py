"""Central configuration. Everything is env-overridable so the same image runs
locally in mock mode and in production with real keys."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

try:  # optional: load .env when present, never required
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ModelTiers:
    """Multi-model routing table.

    Each stage of the pipeline has a different latency/quality requirement, so
    each stage gets its own model. Mapping stages to the *cheapest model that
    still succeeds* is what keeps the end-to-end flow fast.
    """

    # Tier 0 - classification / extraction on the hot path. Smallest model.
    triage: str = field(default_factory=lambda: os.getenv("MODEL_TRIAGE", "gemini-3.5-flash-lite"))
    # Tier 1 - the fulfilment planner. Balanced reasoning, still a flash model.
    planner: str = field(default_factory=lambda: os.getenv("MODEL_PLANNER", "gemini-3.6-flash"))
    # Tier 2 - exception handling / self-healing re-plan. Strongest tier, and it
    # only ever loads on the failure path, so its latency never hits the happy path.
    repair: str = field(default_factory=lambda: os.getenv("MODEL_REPAIR", "gemini-3.7-flash"))
    # Tier 3 - multimodal QA (parcel damage photos, shipping-label OCR).
    vision: str = field(default_factory=lambda: os.getenv("MODEL_VISION", "gemini-3.5-flash"))
    # Conversational ops copilot (multi-turn tool calling).
    chat: str = field(default_factory=lambda: os.getenv("MODEL_CHAT", "gemini-3.5-flash"))

    def as_dict(self) -> dict[str, str]:
        return {
            "triage": self.triage,
            "planner": self.planner,
            "repair": self.repair,
            "vision": self.vision,
            "chat": self.chat,
        }


@dataclass(frozen=True)
class Settings:
    app_name: str = "ChainMind"
    version: str = "1.0.0"

    gemini_api_key: str | None = field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    )
    maps_api_key: str | None = field(default_factory=lambda: os.getenv("GOOGLE_MAPS_API_KEY"))

    db_path: str = field(default_factory=lambda: os.getenv("CHAINMIND_DB", "./data/chainmind.db"))

    models: ModelTiers = field(default_factory=ModelTiers)

    # Reliability knobs (self-healing)
    tool_max_attempts: int = field(default_factory=lambda: _int("TOOL_MAX_ATTEMPTS", 3))
    tool_backoff_base_ms: int = field(default_factory=lambda: _int("TOOL_BACKOFF_BASE_MS", 40))
    breaker_threshold: int = field(default_factory=lambda: _int("BREAKER_THRESHOLD", 4))
    breaker_cooldown_s: float = field(default_factory=lambda: _float("BREAKER_COOLDOWN_S", 15.0))
    llm_timeout_s: float = field(default_factory=lambda: _float("LLM_TIMEOUT_S", 25.0))

    # Pipeline
    max_agent_turns: int = field(default_factory=lambda: _int("MAX_AGENT_TURNS", 6))
    force_mock: bool = field(default_factory=lambda: _bool("CHAINMIND_MOCK", False))

    @property
    def llm_live(self) -> bool:
        """True when we can really call Gemini."""
        return bool(self.gemini_api_key) and not self.force_mock

    @property
    def maps_live(self) -> bool:
        return bool(self.maps_api_key) and not self.force_mock

    def capabilities(self) -> dict[str, object]:
        return {
            "llm": "gemini" if self.llm_live else "deterministic-mock",
            "maps": "google-routes-v2" if self.maps_live else "haversine-mock",
            "models": self.models.as_dict(),
            "mock_mode": not self.llm_live,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

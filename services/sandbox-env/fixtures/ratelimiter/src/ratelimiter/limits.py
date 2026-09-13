"""Settings loading and derived limits."""

import json
import math
from dataclasses import dataclass
from pathlib import Path

REQUIRED_KEYS = ("capacity", "refill_per_sec", "burst_multiplier")


@dataclass(frozen=True)
class Settings:
    capacity: int
    refill_per_sec: float
    burst_multiplier: float


def load_settings(path: str | Path = "config/settings.json") -> Settings:
    raw = json.loads(Path(path).read_text())
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if missing:
        raise KeyError(f"settings missing keys: {missing}")
    return Settings(
        capacity=int(raw["capacity"]),
        refill_per_sec=float(raw["refill_per_sec"]),
        burst_multiplier=float(raw["burst_multiplier"]),
    )


def allowed_burst(settings: Settings) -> int:
    """Maximum burst size: floor(capacity * burst_multiplier)."""
    return math.floor(settings.capacity * settings.burst_multiplier)

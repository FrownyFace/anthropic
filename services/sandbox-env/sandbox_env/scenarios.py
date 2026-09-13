"""Scenario catalogue.

A scenario file has a *public* half and a *private* half, and `ScenarioBundle` keeps the two apart
so it is hard to leak the private half by accident: the REST layer only ever serialises
`bundle.public`.

Public (`Scenario`, safe for the browser; only `task_prompt` ever reaches the model):
    id · title · description · task_prompt · max_steps · fault_kinds (kinds only) ·
    harness_faults (REAL interruptions the harness inflicts on itself) ·
    checks (id/description/weight — what the grader will look for) ·
    faults_public (one row per fault class: kind + origin + layer + a sentence)

Private (never leaves this service): the fault plan's paths, modes, hit counts and delays, the
setup overlays, the check weights' arithmetic, and the hidden grader tests.

Data lives in three directories that are baked into the Modal image under /app; locally they sit
in the service tree. Both layouts are probed, so the same code runs in a container and in pytest.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultline_common.schemas import CheckSpec as PublicCheckSpec
from faultline_common.schemas import FaultKind, FaultPlan, HarnessFault, Scenario

from . import provenance

_HERE = Path(__file__).resolve().parent
_SERVICE_ROOT = _HERE.parent


def _first_existing(*candidates: Path) -> Path:
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[-1]


#: scenario JSON + overlays/ (image: /app/scenarios, local: sandbox_env/scenarios)
SCENARIOS_DIR = _first_existing(Path("/app/scenarios"), _HERE / "scenarios")
#: fixture repos copied into the sandbox at reset (image: /app/fixtures)
FIXTURES_DIR = _first_existing(Path("/app/fixtures"), _SERVICE_ROOT / "fixtures")
#: hidden grader tests, uploaded only at evaluate time (image: /app/graders)
GRADERS_DIR = _first_existing(Path("/app/graders"), _SERVICE_ROOT / "graders")


class ScenarioNotFound(KeyError):
    pass


@dataclass(frozen=True)
class SetupOp:
    """A pre-episode edit applied to the fixture before the agent ever sees it."""

    op: str
    path: str
    content: bytes


@dataclass(frozen=True)
class CheckSpec:
    id: str
    weight: float
    description: str = ""


@dataclass(frozen=True)
class ScenarioBundle:
    public: Scenario
    fixture: str
    setup: tuple[SetupOp, ...]
    fault_plan: FaultPlan
    checks: tuple[CheckSpec, ...]
    hidden_tests: str | None

    @property
    def id(self) -> str:
        return self.public.id

    @property
    def fixture_dir(self) -> Path:
        return FIXTURES_DIR / self.fixture

    @property
    def hidden_tests_dir(self) -> Path | None:
        if not self.hidden_tests:
            return None
        rel = self.hidden_tests
        # scenario files say "graders/<id>"; the image drops the "graders/" prefix
        if rel.startswith("graders/"):
            return GRADERS_DIR / rel[len("graders/") :]
        p = Path(rel)
        return p if p.is_absolute() else _SERVICE_ROOT / p


def _load_one(path: Path) -> ScenarioBundle:
    raw: dict[str, Any] = json.loads(path.read_text())
    plan = FaultPlan.model_validate(raw.get("fault_plan") or {})
    kinds: list[FaultKind] = []
    for f in plan.faults:
        if f.kind not in kinds:
            kinds.append(f.kind)
    # REAL interruptions the harness inflicts on itself. They are public whole (path and timing
    # included) because they are a chaos setting for the *run*, executed by the harness, not a trap
    # laid for the agent — nothing here reaches the model, which only ever sees `task_prompt`.
    harness_faults = [HarnessFault.model_validate(h) for h in raw.get("harness_faults") or []]
    public = Scenario(
        id=raw["id"],
        title=raw["title"],
        description=raw.get("description", ""),
        task_prompt=raw["task_prompt"],
        max_steps=int(raw.get("max_steps", 20)),
        fault_kinds=kinds,
        harness_faults=harness_faults,
        # what the grader will look for: id/description/weight, no thresholds, no ledger internals
        checks=[
            PublicCheckSpec(
                id=c["id"],
                description=c.get("description", ""),
                weight=float(c.get("weight", 1.0)),
            )
            for c in raw.get("checks") or []
        ],
        # the fault *classes* in play, with provenance — never paths, hits, modes or timings
        faults_public=provenance.faults_public(plan, harness_faults),
    )
    setup: list[SetupOp] = []
    for op in raw.get("setup") or []:
        if op.get("op") != "write":
            raise ValueError(f"{path.name}: unsupported setup op {op.get('op')!r}")
        if "content_file" in op:
            src = (SCENARIOS_DIR / op["content_file"]).resolve()
            content = src.read_bytes()
        else:
            content = str(op.get("content", "")).encode()
        setup.append(SetupOp(op="write", path=op["path"], content=content))
    checks = tuple(
        CheckSpec(id=c["id"], weight=float(c.get("weight", 1.0)), description=c.get("description", ""))
        for c in raw.get("checks") or []
    )
    return ScenarioBundle(
        public=public,
        fixture=raw.get("fixture", "ratelimiter"),
        setup=tuple(setup),
        fault_plan=plan,
        checks=checks,
        hidden_tests=raw.get("hidden_tests"),
    )


@functools.lru_cache(maxsize=1)
def _catalogue() -> dict[str, ScenarioBundle]:
    out: dict[str, ScenarioBundle] = {}
    for p in sorted(SCENARIOS_DIR.glob("*.json")):
        b = _load_one(p)
        out[b.id] = b
    return out


def reload_catalogue() -> None:
    """Drop the cache (tests only)."""
    _catalogue.cache_clear()


def list_bundles() -> list[ScenarioBundle]:
    return list(_catalogue().values())


def list_scenarios() -> list[Scenario]:
    """Public catalogue: what the UI and the agent are allowed to see."""
    return [b.public for b in _catalogue().values()]


def get_bundle(scenario_id: str) -> ScenarioBundle:
    try:
        return _catalogue()[scenario_id]
    except KeyError as exc:
        raise ScenarioNotFound(scenario_id) from exc

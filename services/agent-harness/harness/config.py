"""Deploy/runtime configuration for the harness (env-driven, no secrets read here)."""

from __future__ import annotations

import os
from typing import Any

APP_NAME = "faultline-harness"
SVC = "harness"

# Persistence (PLAN.md §2.9 / ARCHITECTURE.md §4): one SQLite file, hot on the container's own disk,
# snapshotted onto the Modal Volume `faultline-db` by VACUUM INTO + os.replace + commit.
DB_VOLUME = "faultline-db"
DB_MOUNT = "/data"
DB_FILENAME = "faultline.sqlite3"
HOT_DB_PATH = f"/tmp/{DB_FILENAME}"
SNAPSHOT_PATH = f"{DB_MOUNT}/{DB_FILENAME}"
CHECKPOINT_INTERVAL_S = 15.0

DEFAULT_SANDBOX_ENV_URL = "https://appliedlabsai-local--faultline-sandbox-env-api.modal.run"
DEFAULT_HARNESS_URL = "https://appliedlabsai-local--faultline-harness-api.modal.run"
DEFAULT_SECRET_NAME = "anthropic-secret"

# Per PLAN.md §2.3 / schemas.MODEL_ALLOWLIST.
MODEL_ALLOWLIST = ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"]
DEFAULT_MODEL = "claude-haiku-4-5"
# Sonnet 5 / Opus 5 get adaptive thinking; Haiku 4.5 sends no `thinking` param by default.
NO_THINKING_MODELS = {"claude-haiku-4-5"}

MAX_TOKENS = 4096
DEFAULT_MAX_STEPS = 20
HARD_MAX_STEPS = 40

# Opt-in only (HARNESS_HAIKU_THINKING=1). Must stay strictly below MAX_TOKENS.
HAIKU_THINKING_BUDGET = 2048

# Modal web requests are hard-capped at 150 s; close our SSE window well before that.
SSE_WINDOW_S = 110.0
SSE_POLL_S = 0.4  # PLAN.md §2.3: the api polls Store.events_after every 400 ms
SSE_KEEPALIVE_S = 10.0

# Events are appended to the Store in coalesced batches: at most one round trip per this many
# seconds while a run is producing events (the sink re-sends everything at run.finished anyway).
EVENT_FLUSH_S = 0.25

TOOL_TIMEOUT_S = 90.0  # per MCP tool call (ack_lost sleeps ~3 s, run_command caps at 60 s)
GYM_TIMEOUT_S = 60.0
MODEL_RETRIES = 3

# Harness-level retries of ONE MCP call. Only a failure to establish the connection is retried —
# the request provably never left this process. A timeout, an aborted stream or any other
# unknown-outcome failure is handed to the agent as-is: re-sending it is the duplicate-write bug
# the grader exists to catch (GRADING.md `verified_before_rewrite`).
TOOL_MAX_ATTEMPTS = 2

# Modal re-invokes `run_episode` with the SAME inputs after the worker dies (PLAN.md §2.11).
RUN_RETRIES = 2
RUN_RETRY_DELAY_S = 1.0

# `Scenario.harness_faults` is the contract source (GET /scenarios -> reset -> here). The deployed
# sandbox-env does not publish the field yet (it returns `harness_faults: []` for worker-crash), so
# the trigger the scenario file on disk declares is mirrored here as a LAST resort: it is used only
# when the gym published none, it is logged as `source: fallback`, and it disappears the moment the
# gym starts serving the field. `HARNESS_FAULTS_FALLBACK=0` disables it.
HARNESS_FAULTS_FALLBACK: dict[str, list[dict[str, Any]]] = {
    "worker-crash": [
        {"kind": "worker_crash", "tool": "write_file", "path": "CHANGELOG.md", "nth": 1,
         "after_ms": 400}
    ],
}

LOG_VALUE_CAP = 500  # log events mirrored into the stream are truncated to this


def sandbox_env_url() -> str:
    return os.environ.get("SANDBOX_ENV_URL", DEFAULT_SANDBOX_ENV_URL).rstrip("/")


def model_default() -> str:
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)


def truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def thinking_for(model: str) -> dict[str, Any] | None:
    """`thinking` kwarg for messages.create, or None to omit it.

    Sonnet 5 / Opus 5 -> `{"type": "adaptive"}` (never `budget_tokens`).
    Haiku 4.5 -> nothing at all, which is what every default run uses.

    HARNESS_HAIKU_THINKING=1 is an explicit, deploy-time opt-in for Haiku (the one case where an
    extended-thinking budget is legal: `{"type": "enabled", "budget_tokens": n}` with n < max_tokens).
    Keep it here rather than in the loop so one env var covers every call site, and keep the default
    OFF so nothing about the demo path changes unless an operator asks for it.
    """
    if model not in NO_THINKING_MODELS:
        return {"type": "adaptive"}
    if truthy("HARNESS_HAIKU_THINKING"):
        return {"type": "enabled", "budget_tokens": min(HAIKU_THINKING_BUDGET, MAX_TOKENS - 1)}
    return None


def harness_faults_for(scenario_id: str) -> list[dict[str, Any]]:
    """Fallback triggers for a scenario whose gym does not publish `harness_faults` yet."""
    if not truthy("HARNESS_FAULTS_FALLBACK", True):
        return []
    return [dict(f) for f in HARNESS_FAULTS_FALLBACK.get(scenario_id, [])]


def secret_name() -> str:
    return os.environ.get("FAULTLINE_ANTHROPIC_SECRET", DEFAULT_SECRET_NAME)


def modal_environment() -> str | None:
    return os.environ.get("MODAL_ENVIRONMENT") or None


def masked_workspace() -> str | None:
    """ANTHROPIC_WORKSPACE surfaced as `first 6 chars + …`; never the key, never the full value."""
    ws = os.environ.get("ANTHROPIC_WORKSPACE")
    if not ws:
        return None
    return ws[:6] + "…" if len(ws) > 6 else ws + "…"

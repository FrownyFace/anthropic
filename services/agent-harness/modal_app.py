"""Modal app `faultline-harness`: the browser-facing API + the model loop + the SQLite Store.

    api           ASGI (FastAPI). NO secrets. Spawns runs, serves run state and the SSE tail.
                  https://appliedlabsai-local--faultline-harness-api.modal.run
    Store         @app.cls(max_containers=1, volumes={"/data": faultline-db}) — the ONLY process
                  that opens the database. Hot copy on /tmp, consistent snapshots on the Volume
                  (PLAN.md §2.9, ARCHITECTURE.md §4).
    run_episode   the ONLY function with the provider secret attached; runs one episode.

Deploy:  modal deploy -e local services/agent-harness/modal_app.py
Smoke:   modal run -e local services/agent-harness/modal_app.py --scenario lost-ack
Backup:  modal run -e local services/agent-harness/modal_app.py::checkpoint_now
         modal volume get -e local faultline-db faultline.sqlite3 ./runs/

NOTE on `modal serve`: the served app gets its OWN Store container with the SAME Volume mounted,
which would make two writers for one database file. Deploy (or point the served app at a different
volume) rather than serving this app while the deployed one is live.

Deploy-time env (baked into the image so containers need no lookup):
    SANDBOX_ENV_URL             default https://appliedlabsai-local--faultline-sandbox-env-api.modal.run
    ANTHROPIC_MODEL             default claude-haiku-4-5
    LOG_LEVEL                   default info
    FAULTLINE_ANTHROPIC_SECRET  default anthropic-secret (the secret is attached to run_episode only)
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Any

# Make `faultline_common` (shared contracts + logging) and `harness` importable at DEPLOY time,
# without hardcoding an absolute path: this service directory and <repo>/packages/common.
# Inside a container this file lives at /root/modal_app.py, where neither sibling exists (both
# packages are baked in by add_local_python_source) — so every candidate is probed, never assumed.
_HERE = pathlib.Path(__file__).resolve().parent
_CANDIDATES = [_HERE]
if len(_HERE.parents) >= 2:
    _CANDIDATES.append(_HERE.parents[1] / "packages" / "common")
for _p in _CANDIDATES:
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import modal  # noqa: E402

from harness import config  # noqa: E402

SANDBOX_ENV_URL = os.environ.get("SANDBOX_ENV_URL", config.DEFAULT_SANDBOX_ENV_URL).rstrip("/")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", config.DEFAULT_MODEL)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info")
# Off by default; set HARNESS_HAIKU_THINKING=1 at deploy time to let Haiku 4.5 think.
HAIKU_THINKING = os.environ.get("HARNESS_HAIKU_THINKING", "0")
SECRET_NAME = os.environ.get("FAULTLINE_ANTHROPIC_SECRET", config.DEFAULT_SECRET_NAME)

IMAGE = (
    modal.Image.debian_slim(python_version="3.11")
    .uv_pip_install(
        "anthropic==1.5.0",
        "fastapi>=0.115",
        "fastmcp==4.0.3",
        "httpx>=0.27",
        "pydantic>=2.7",
        "uvicorn>=0.30",
    )
    .env(
        {
            "SANDBOX_ENV_URL": SANDBOX_ENV_URL,
            "ANTHROPIC_MODEL": ANTHROPIC_MODEL,
            "LOG_LEVEL": LOG_LEVEL,
            "HARNESS_HAIKU_THINKING": HAIKU_THINKING,
            "FAULTLINE_MIGRATIONS_DIR": "/root/faultline_migrations",
            "PYTHONUNBUFFERED": "1",
        }
    )
    # add_local_python_source ships ONLY the package's .py files (measured on modal 1.5.5: the
    # container had harness/*.py and no harness/migrations/), so the schema is copied in as its own
    # directory — outside the package, where the python-source mount cannot shadow it.
    .add_local_dir(str(_HERE / "harness" / "migrations"), "/root/faultline_migrations", copy=True)
    .add_local_python_source("faultline_common", "harness")
)

app = modal.App(config.APP_NAME)

# v1 volume: v2 needs an explicit version= and the 1.5.5 CLI still flags it experimental; it buys
# nothing for a single file (PLAN.md §2.4).
DB_VOLUME = modal.Volume.from_name(config.DB_VOLUME, create_if_missing=True)


@app.cls(
    image=IMAGE,
    volumes={config.DB_MOUNT: DB_VOLUME},
    max_containers=1,   # single writer: the only process that ever opens the database
    min_containers=1,   # an SSE poll must not pay a cold start plus a restore
    timeout=120,
    scaledown_window=300,
)
@modal.concurrent(max_inputs=32)
class Store:
    """SQLite on the Modal Volume. Every method is a thin wrapper over harness.sqlite_store."""

    @modal.enter()
    def enter(self) -> None:
        from harness.sqlite_store import SqliteStore

        self._store = SqliteStore(
            config.HOT_DB_PATH,
            snapshot_path=config.SNAPSHOT_PATH,
            commit=DB_VOLUME.commit,
            reload=DB_VOLUME.reload,
            checkpoint_interval_s=config.CHECKPOINT_INTERVAL_S,
        ).open()
        self._store.start_autocheckpoint()

    @modal.exit()
    def exit(self) -> None:
        try:
            self._store.checkpoint(force=True)
        finally:
            self._store.close()

    # ------------------------------------------------------------------ identity + conversations
    @modal.method()
    def upsert_user(self, user_id: str, user_agent: str | None = None) -> dict[str, Any]:
        return self._store.upsert_user(user_id, user_agent)

    @modal.method()
    def list_conversations(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._store.list_conversations(user_id, limit)

    @modal.method()
    def create_conversation(self, user_id: str, scenario_id: str, title: str | None = None,
                            user_agent: str | None = None) -> dict[str, Any]:
        return self._store.create_conversation(user_id, scenario_id, title, user_agent)

    @modal.method()
    def get_conversation(self, conversation_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        return self._store.get_conversation(conversation_id, user_id)

    @modal.method()
    def update_conversation(self, conversation_id: str, user_id: str | None = None, *,
                            title: str | None = None,
                            archived: bool | None = None) -> dict[str, Any] | None:
        return self._store.update_conversation(conversation_id, user_id, title=title, archived=archived)

    @modal.method()
    def delete_conversation(self, conversation_id: str,
                            user_id: str | None = None) -> dict[str, Any] | None:
        return self._store.delete_conversation(conversation_id, user_id)

    # ------------------------------------------------------------------ runs + events
    @modal.method()
    def create_run(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._store.create_run(record)

    @modal.method()
    def append_events(self, run_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        return self._store.append_events(run_id, events)

    @modal.method()
    def finish_run(self, run_id: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._store.finish_run(run_id, fields)

    @modal.method()
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._store.get_run(run_id)

    @modal.method()
    def list_runs(self, limit: int = 50, user_id: str | None = None) -> list[dict[str, Any]]:
        return self._store.list_runs(limit, user_id)

    @modal.method()
    def events_after(self, run_id: str, after: int = -1, limit: int = 2000) -> dict[str, Any]:
        return self._store.events_after(run_id, after, limit)

    # ------------------------------------------------------------------ ops
    @modal.method()
    def checkpoint(self, force: bool = False) -> dict[str, Any]:
        return self._store.checkpoint(force)

    @modal.method()
    def health(self) -> dict[str, Any]:
        return self._store.health()


def _store() -> Any:
    """RunStore wired to THIS app's Store class (no name lookup, no deploy-order dependency)."""
    from harness.store import ModalStoreClient, RunStore

    return RunStore(ModalStoreClient(Store))


def _spawn(run_id: str, req: dict[str, Any]) -> str | None:
    """Hand the run to the secret-bearing function and return immediately (Modal caps requests at 150 s)."""
    call = run_episode.spawn(run_id, req)
    return getattr(call, "object_id", None)


@app.function(image=IMAGE, min_containers=1, scaledown_window=300, timeout=300)
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def api():
    """FastAPI app. Deliberately has no secrets: /health must report has_provider_key=false."""
    from faultline_common.log import get_logger

    from harness.api import create_app

    log = get_logger(config.SVC)
    log.info(
        "api.boot",
        "asgi app starting",
        has_key=bool(os.environ.get("ANTHROPIC_API_KEY")),
        sandbox_env_url=config.sandbox_env_url(),
        model_default=config.model_default(),
    )
    return create_app(store=_store(), spawn=_spawn)


@app.function(
    image=IMAGE,
    # required_keys makes a misconfigured secret fail at deploy time, not mid-episode.
    secrets=[modal.Secret.from_name(SECRET_NAME, required_keys=["ANTHROPIC_API_KEY"])],
    timeout=900,
    # PLAN.md §2.11: this is the resume mechanism. Modal re-invokes the function with the SAME
    # (run_id, req) after the worker dies — which is what the `worker_crash` harness fault causes
    # on purpose, and what an OOM/preemption causes by accident. `run_episode_sync` notices the
    # run already has events, rebuilds the conversation and continues it; a run that already
    # reached a terminal status returns immediately, so a retry can never double-run an episode.
    # Constant backoff: the sandbox is still alive (idle_timeout 600 s) and waiting is pure loss.
    retries=modal.Retries(max_retries=config.RUN_RETRIES, initial_delay=config.RUN_RETRY_DELAY_S,
                          backoff_coefficient=1.0),
)
def run_episode(run_id: str, req: dict[str, Any]) -> dict[str, Any]:
    """Run one episode. The only process in Faultline that sees ANTHROPIC_API_KEY."""
    from faultline_common.log import get_logger

    from harness.loop import run_episode_sync

    log = get_logger(config.SVC)
    log.info(
        "run_episode.start",
        "spawned",
        run_id=run_id,
        has_key=bool(os.environ.get("ANTHROPIC_API_KEY")),  # never the value
        anthropic_workspace=config.masked_workspace(),
        scenario_id=req.get("scenario_id"),
        model=req.get("model") or config.model_default(),
        sandbox_env_url=config.sandbox_env_url(),
    )
    record = run_episode_sync(run_id, req, store=_store())
    return {
        "run_id": record.get("run_id"),
        "status": record.get("status"),
        "episode_id": record.get("episode_id"),
        "steps": record.get("steps"),
        "score": (record.get("evaluation") or {}).get("score"),
        "events": len(record.get("events") or []),
        "error": record.get("error"),
        "error_class": (record.get("error_class") or {}).get("code"),
        "worker_generation": record.get("worker_generation"),
        "interruptions": len(record.get("interruptions") or []),
    }


def _deployed_store() -> Any:
    """The DEPLOYED Store, resolved by name.

    Local entrypoints must never touch the in-module `Store`: `modal run` builds an ephemeral app,
    so that would start a SECOND container with the same Volume mounted — two writers for one
    SQLite file. By name, every caller lands on the one deployed container.
    """
    return modal.Cls.from_name(
        config.APP_NAME, "Store", environment_name=config.modal_environment()
    )()


@app.local_entrypoint()
def run(scenario: str = "lost-ack", model: str = "", max_steps: int = 0, seed: int = -1,
        user: str = "") -> None:
    """Spawn one episode on the DEPLOYED harness and tail its events from the Store.

    Dev smoke test, no web app needed. `--user u_<uuid4>` scopes the run to a browser identity, so
    `GET /conversations` with the same header lists it afterwards.
    """
    import json
    import time
    import uuid

    run_id = f"r_{uuid.uuid4().hex[:12]}"
    req: dict[str, Any] = {"scenario_id": scenario}
    if model:
        req["model"] = model
    if max_steps:
        req["max_steps"] = max_steps
    if seed >= 0:
        req["seed"] = seed
    if user:
        req["user_id"] = user

    store = _deployed_store()
    fn = modal.Function.from_name(config.APP_NAME, "run_episode",
                                  environment_name=config.modal_environment())
    print(json.dumps({"ev": "cli.spawn", "run_id": run_id, "req": req}))
    call = fn.spawn(run_id, req)

    seen = -1
    deadline = time.time() + 900
    status = None
    while time.time() < deadline:
        page = store.events_after.remote(run_id, seen) or {}
        for event in page.get("events") or []:
            data = event.get("data") or {}
            label = data.get("summary") or data.get("msg") or data.get("status") or ""
            print(f"[{event.get('id'):>3}] step={event.get('step')} {event.get('type'):<18} {str(label)[:110]}")
            seen = max(seen, int(event.get("id", seen)))
        status = page.get("status")
        if status in {"ok", "error", "truncated", "unevaluated", "interrupted"}:
            record = store.get_run.remote(run_id) or {}
            print(json.dumps({"ev": "cli.finished", "status": status,
                              "score": (record.get("evaluation") or {}).get("score"),
                              "conversation_id": record.get("conversation_id"),
                              "error": record.get("error")}))
            break
        time.sleep(1.0)
    else:
        print(json.dumps({"ev": "cli.timeout", "run_id": run_id}))

    print(json.dumps({"ev": "cli.result", "result": call.get(timeout=60)}, default=str))


@app.local_entrypoint()
def import_legacy_runs(dict_name: str = "faultline-runs", apply: bool = False,
                       chunk: int = 50) -> None:
    """One-off: copy runs out of the old Modal Dict into the Store, so pre-Store `?run=<id>` links
    (the demo runs the evidence and the web app reference) keep resolving.

    Idempotent: `create_run` is INSERT OR IGNORE and `append_events` is keyed on (run_id, seq), so
    re-running changes nothing. Default is a dry run; pass --apply to write.
    """
    import json

    import modal as _modal

    src = _modal.Dict.from_name(dict_name, environment_name=config.modal_environment(),
                                create_if_missing=False)
    store = _deployed_store()
    index = list(src.get("__index__", []) or [])
    moved, skipped = 0, 0
    for run_id in index:
        record = src.get(run_id) or {}
        if not isinstance(record, dict) or not record.get("run_id"):
            skipped += 1
            continue
        events = list(record.get("events") or [])
        line = {"run_id": run_id, "scenario": record.get("scenario_id"),
                "status": record.get("status"), "events": len(events)}
        if not apply:
            print(json.dumps({"ev": "import.dry_run", **line}))
            moved += 1
            continue
        store.create_run.remote({k: v for k, v in record.items() if k != "events"})
        appended = 0
        for i in range(0, len(events), chunk):
            appended += int((store.append_events.remote(run_id, events[i:i + chunk]) or {}).get("appended") or 0)
        store.finish_run.remote(run_id, {k: record[k] for k in
                                         ("status", "finished_at", "error", "steps", "summary",
                                          "episode_id", "evaluation", "usage", "task_prompt")
                                         if record.get(k) is not None})
        print(json.dumps({"ev": "import.moved", **line, "appended": appended}))
        moved += 1
    print(json.dumps({"ev": "import.done", "runs": moved, "skipped": skipped, "applied": apply,
                      "store": store.health.remote()}, default=str))


@app.local_entrypoint()
def prune_empty_conversations(user: str = "", apply: bool = False) -> None:
    """Archive conversations that have no runs (maintenance; soft, nothing is hard-deleted)."""
    import json

    from harness.sqlite_store import ANON_USER_ID

    store = _deployed_store()
    owner = user or ANON_USER_ID
    empty = [c for c in (store.list_conversations.remote(owner, 500) or []) if not c.get("last_run")]
    for conv in empty:
        if apply:
            store.delete_conversation.remote(conv["id"], owner)
        print(json.dumps({"ev": "prune.archived" if apply else "prune.dry_run",
                          "conversation_id": conv["id"], "title": conv.get("title")}))
    print(json.dumps({"ev": "prune.done", "user": owner, "archived": len(empty), "applied": apply,
                      "store": store.health.remote()}, default=str))


@app.local_entrypoint()
def checkpoint_now() -> None:
    """Force a consistent snapshot onto the Volume (evidence/export, PLAN.md §5)."""
    import json

    print(json.dumps(_deployed_store().checkpoint.remote(True), default=str))


@app.local_entrypoint()
def store_health() -> None:
    """Print the Store's health (row counts, last checkpoint, schema version)."""
    import json

    print(json.dumps(_deployed_store().health.remote(), default=str))

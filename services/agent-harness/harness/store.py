"""Run/conversation persistence — a thin client over the single-writer SQLite `Store`.

`RunStore` keeps the small verb set `api.py`, `events.py` and `loop.py` already use
(`get / create / append_events / update / list_runs`) so the migration off the Modal Dict
`faultline-runs` was a backend swap, not a rewrite (PLAN.md §2.9, ARCHITECTURE.md §4.3). It also
passes the identity/conversation verbs straight through.

Two backends, same surface:

  * `SqliteStore`         — in-process. Used by every test (temp dir or `:memory:`) and by a local
                            `uvicorn harness.api:app` run.
  * `ModalStoreClient`    — `Store().<method>.remote(...)` on the `@app.cls(max_containers=1)`
                            defined in `modal_app.py`. Used in the deployed harness, where the
                            database must live in exactly one process.

Nothing else may open the database file: single writer is what makes SQLite safe here.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from faultline_common.log import get_logger

from . import config
from .sqlite_store import (  # re-exported: callers import these from harness.store
    ANON_USER_ID,
    TERMINAL_STATUSES,
    SqliteStore,
    new_id,
    now_iso,
    valid_user_id,
)

log = get_logger(config.SVC)

__all__ = [
    "ANON_USER_ID",
    "TERMINAL_STATUSES",
    "ModalStoreClient",
    "RunStore",
    "SqliteStore",
    "memory_store",
    "new_id",
    "now_iso",
    "valid_user_id",
]

# Every verb the Store exposes; `ModalStoreClient` forwards each one to the Modal class.
STORE_METHODS = (
    "upsert_user",
    "list_conversations",
    "create_conversation",
    "get_conversation",
    "update_conversation",
    "delete_conversation",
    "create_run",
    "append_events",
    "finish_run",
    "get_run",
    "list_runs",
    "events_after",
    "checkpoint",
    "health",
)


def memory_store() -> SqliteStore:
    """A fresh private database (tests, and any caller that wants a throwaway store)."""
    return SqliteStore(":memory:").open()


class ModalStoreClient:
    """Forwards every Store verb to the single-container Modal class.

    `cls_handle` is the decorated class from `modal_app` when we are running inside the app (no
    lookup, no dependency on what is currently deployed); otherwise the class is resolved by name.
    """

    def __init__(self, cls_handle: Any = None, *, app_name: str = config.APP_NAME,
                 class_name: str = "Store"):
        self._cls_handle = cls_handle
        self._app_name = app_name
        self._class_name = class_name
        self._instance: Any = None
        self._lock = threading.Lock()

    def _obj(self) -> Any:
        with self._lock:
            if self._instance is None:
                handle = self._cls_handle
                if handle is None:
                    import modal

                    handle = modal.Cls.from_name(
                        self._app_name, self._class_name,
                        environment_name=config.modal_environment(),
                    )
                self._instance = handle()
            return self._instance

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name not in STORE_METHODS:
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> Any:
            return getattr(self._obj(), name).remote(*args, **kwargs)

        return call


class RunStore:
    """The verbs the rest of the harness calls. Backend-agnostic; never opens a file itself."""

    def __init__(self, backend: Any = None):
        self._backend = backend

    @property
    def backend(self) -> Any:
        if self._backend is None:
            self._backend = ModalStoreClient()
        return self._backend

    # ------------------------------------------------------------------ runs
    def get(self, run_id: str) -> dict[str, Any] | None:
        record = self.backend.get_run(run_id)
        return record if isinstance(record, dict) else None

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.backend.create_run(dict(record))

    def append_events(self, run_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Idempotent on (run_id, seq); returns {run_id, appended, count} — NOT the whole record."""
        return self.backend.append_events(run_id, list(events or []))

    def update(self, run_id: str, **fields: Any) -> dict[str, Any]:
        """Merge scalar fields (status, finished_at, evaluation, usage, …) into the run row."""
        return self.backend.finish_run(run_id, dict(fields))

    def finish_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        return self.update(run_id, **fields)

    def list_runs(self, limit: int = 50, user_id: str | None = None) -> list[dict[str, Any]]:
        return list(self.backend.list_runs(limit, user_id) or [])

    def events_after(self, run_id: str, after: int = -1, limit: int = 2000) -> dict[str, Any]:
        return self.backend.events_after(run_id, after, limit)

    # ------------------------------------------------------------------ identity + conversations
    def upsert_user(self, user_id: str, user_agent: str | None = None) -> dict[str, Any]:
        return self.backend.upsert_user(user_id, user_agent)

    def list_conversations(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.backend.list_conversations(user_id, limit) or [])

    def create_conversation(self, user_id: str, scenario_id: str, title: str | None = None,
                            user_agent: str | None = None) -> dict[str, Any]:
        return self.backend.create_conversation(user_id, scenario_id, title, user_agent)

    def get_conversation(self, conversation_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        return self.backend.get_conversation(conversation_id, user_id)

    def update_conversation(self, conversation_id: str, user_id: str | None = None, *,
                            title: str | None = None, archived: bool | None = None) -> dict[str, Any] | None:
        return self.backend.update_conversation(conversation_id, user_id, title=title, archived=archived)

    def delete_conversation(self, conversation_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        return self.backend.delete_conversation(conversation_id, user_id)

    # ------------------------------------------------------------------ ops
    def checkpoint(self, force: bool = False) -> dict[str, Any]:
        return self.backend.checkpoint(force)

    def health(self) -> dict[str, Any]:
        return self.backend.health()

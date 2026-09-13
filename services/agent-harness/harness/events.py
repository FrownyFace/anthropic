"""Event sink: the single writer of a run's event stream.

Every event gets a monotonic 0-based `id` (that is also the SSE `Last-Event-ID`), an ISO-8601 Z
timestamp, is validated against `faultline_common.schemas.Event`, appended to the in-memory
`RunRecord`, handed to the Store via `append_events` (idempotent on `(run_id, seq)`) and mirrored to
stdout as a unified JSON log line.

Persistence policy (PLAN.md §2.9): the loop no longer re-`put`s the whole record after every event.
Events are appended in small batches — coalesced over `flush_interval` seconds so a 150-event run
costs a few dozen round trips instead of 150 — and the sink re-sends its COMPLETE in-memory list at
`run.finished`, which back-fills anything a Store restart dropped. A failed flush is never fatal: the
batch stays pending and the final re-send closes the gap.

`sink.log(...)` is the other direction: a unified log line that is ALSO mirrored into the stream as
a `log` event so the browser timeline can show harness-side detail (bounded to 500 chars/value).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from faultline_common.log import get_logger, truncate
from faultline_common.schemas import Event

from . import config
from .store import RunStore

_log = get_logger(config.SVC)

TERMINAL_STATUSES = {"ok", "error", "truncated", "unevaluated", "interrupted"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bound(value: Any, cap: int = config.LOG_VALUE_CAP) -> Any:
    """Bound one payload value so mirrored log events can never blow up the record."""
    if isinstance(value, str):
        return truncate(value, cap)
    if isinstance(value, dict):
        return {k: _bound(v, cap) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [_bound(v, cap) for v in list(value)[:50]]
    return value


class EventSink:
    """Owns the RunRecord for the lifetime of a run; appends events to the Store as they happen."""

    def __init__(self, store: RunStore, record: dict[str, Any], *,
                 flush_interval: float = 0.0, clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.record = record
        self.run_id: str = record["run_id"]
        self.step: int | None = None
        self.flush_interval = float(flush_interval)
        self._clock = clock
        self._pending: list[dict[str, Any]] = []
        self._last_flush: float | None = None
        self.flush_failures = 0
        record.setdefault("events", [])

    # ------------------------------------------------------------------ core
    def emit(self, type: str, data: dict[str, Any] | None = None, step: int | None = None) -> dict[str, Any]:
        ev = Event(
            id=len(self.record["events"]),
            ts=now_iso(),
            run_id=self.run_id,
            type=type,  # type: ignore[arg-type]
            step=self.step if step is None else step,
            data=data or {},
        ).model_dump()
        self.record["events"].append(ev)
        self._pending.append(ev)
        self.flush()
        if type != "log":
            _log.info(
                type,
                "event",
                run_id=self.run_id,
                step=ev["step"],
                event_id=ev["id"],
                keys=sorted((data or {}).keys()) or None,
            )
        return ev

    # ------------------------------------------------------------------ persistence
    def flush(self, force: bool = False) -> int:
        """Send pending events to the Store. Returns how many were sent (0 = coalesced or failed)."""
        if not self._pending:
            return 0
        now = self._clock()
        if not force and self._last_flush is not None and (now - self._last_flush) < self.flush_interval:
            return 0
        batch = list(self._pending)
        try:
            self.store.append_events(self.run_id, batch)
        except Exception as exc:  # noqa: BLE001 - a store blip must not kill the episode
            self.flush_failures += 1
            _log.warn("store.append_failed", f"{type(exc).__name__}: {exc}", run_id=self.run_id,
                      pending=len(batch), failures=self.flush_failures)
            return 0
        self._pending = self._pending[len(batch):]
        self._last_flush = now
        return len(batch)

    def persist(self) -> None:
        """Compatibility alias: force the pending batch out now."""
        self.flush(force=True)

    def resend_all(self) -> int:
        """Re-send the COMPLETE in-memory event list (idempotent) so a Store restart self-heals."""
        events = list(self.record.get("events") or [])
        try:
            result = self.store.append_events(self.run_id, events)
        except Exception as exc:  # noqa: BLE001
            _log.error("store.resend_failed", f"{type(exc).__name__}: {exc}", run_id=self.run_id,
                       events=len(events))
            return 0
        self._pending = []
        self._last_flush = self._clock()
        appended = int((result or {}).get("appended") or 0) if isinstance(result, dict) else 0
        _log.info("store.resend", "full event list re-sent", run_id=self.run_id,
                  events=len(events), backfilled=appended or None)
        return appended

    def update(self, **fields: Any) -> None:
        """Merge scalar fields into the record and the run row (status, usage, evaluation, …)."""
        for k, v in fields.items():
            self.record[k] = v
        self.flush(force=True)
        try:
            self.store.update(self.run_id, **fields)
        except Exception as exc:  # noqa: BLE001
            _log.warn("store.update_failed", f"{type(exc).__name__}: {exc}", run_id=self.run_id,
                      keys=sorted(fields.keys()))

    def set_status(self, status: str, **fields: Any) -> None:
        self.update(status=status, **fields)
        _log.info("run.status", status, run_id=self.run_id, **{k: _bound(v) for k, v in fields.items()})

    # ------------------------------------------------------------------ unified log mirror
    def log(self, lvl: str, ev: str, msg: str = "", **kw: Any) -> None:
        """Unified JSON log line + a bounded `log` event in the stream (PLAN.md §2.3/§2.5)."""
        bounded = {k: _bound(v) for k, v in kw.items() if v is not None}
        # callers may pass their own step/run_id; they win, and must not collide with ours
        step = bounded.pop("step", self.step)
        run_id = bounded.pop("run_id", self.run_id)
        rec = getattr(_log, lvl if lvl in {"debug", "info", "warn", "error"} else "info")(
            ev, msg, run_id=run_id, step=step, **bounded
        )
        if not rec:  # filtered out by LOG_LEVEL
            return
        self.emit("log", {"svc": config.SVC, "lvl": lvl, "ev": ev, "msg": msg, **bounded})

    def debug(self, ev: str, msg: str = "", **kw: Any) -> None:
        self.log("debug", ev, msg, **kw)

    def info(self, ev: str, msg: str = "", **kw: Any) -> None:
        self.log("info", ev, msg, **kw)

    def warn(self, ev: str, msg: str = "", **kw: Any) -> None:
        self.log("warn", ev, msg, **kw)

    def error(self, ev: str, msg: str = "", **kw: Any) -> None:
        self.log("error", ev, msg, **kw)

"""Unified JSON-lines logging used by every Faultline service.

One line per event on stdout so Modal captures it (`modal app logs <app> -e local`).
Shape (stable, keep in sync with apps/web/src/lib/log.ts):

    {"ts": ISO8601Z, "svc": "harness|sandbox-env|web|script", "lvl": "debug|info|warn|error",
     "run_id"?: str, "episode_id"?: str, "step"?: int, "ev": "dotted.event.name", "msg": str, ...extras}

Set LOG_LEVEL=debug to include argument/output dumps (callers truncate them first).
"""

from __future__ import annotations

import contextvars
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}

# Correlation ids that services set once per request/episode so every line carries them.
ctx_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
ctx_episode_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("episode_id", default=None)
ctx_step: contextvars.ContextVar[int | None] = contextvars.ContextVar("step", default=None)
ctx_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _min_level() -> int:
    return _LEVELS.get(os.environ.get("LOG_LEVEL", "info").lower(), 20)


def truncate(s: Any, limit: int = 2000) -> Any:
    """Bound a string for log/event payloads, marking truncation explicitly."""
    if not isinstance(s, str) or len(s) <= limit:
        return s
    return s[:limit] + f"…[truncated {len(s) - limit} chars]"


def log_event(svc: str, ev: str, msg: str = "", lvl: str = "info", **extras: Any) -> dict[str, Any]:
    """Emit one JSON log line. Returns the record (handy for mirroring into event streams)."""
    if _LEVELS.get(lvl, 20) < _min_level():
        return {}
    rec: dict[str, Any] = {"ts": _now(), "svc": svc, "lvl": lvl}
    for key, var in (("run_id", ctx_run_id), ("episode_id", ctx_episode_id), ("step", ctx_step), ("request_id", ctx_request_id)):
        val = var.get()
        if val is not None:
            rec[key] = val
    rec["ev"] = ev
    rec["msg"] = msg
    for k, v in extras.items():
        if v is not None:
            rec[k] = v
    sys.stdout.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return rec


class Logger:
    """Tiny bound logger: `log = get_logger("sandbox-env"); log.info("tool.result", "ok", tool="read_file")`."""

    def __init__(self, svc: str):
        self.svc = svc

    def debug(self, ev: str, msg: str = "", **kw: Any) -> dict[str, Any]:
        return log_event(self.svc, ev, msg, "debug", **kw)

    def info(self, ev: str, msg: str = "", **kw: Any) -> dict[str, Any]:
        return log_event(self.svc, ev, msg, "info", **kw)

    def warn(self, ev: str, msg: str = "", **kw: Any) -> dict[str, Any]:
        return log_event(self.svc, ev, msg, "warn", **kw)

    def error(self, ev: str, msg: str = "", **kw: Any) -> dict[str, Any]:
        return log_event(self.svc, ev, msg, "error", **kw)

    class _Timer:
        def __init__(self, outer: "Logger", ev: str, **kw: Any):
            self.outer, self.ev, self.kw = outer, ev, kw
            self.t0 = time.perf_counter()

        def __enter__(self) -> "Logger._Timer":
            return self

        def __exit__(self, *exc: Any) -> None:
            dur = int((time.perf_counter() - self.t0) * 1000)
            if exc[0] is not None:
                self.outer.error(self.ev, f"failed: {exc[1]}", dur_ms=dur, **self.kw)
            else:
                self.outer.info(self.ev, "ok", dur_ms=dur, **self.kw)

    def timed(self, ev: str, **kw: Any) -> "Logger._Timer":
        return Logger._Timer(self, ev, **kw)


def get_logger(svc: str) -> Logger:
    return Logger(svc)

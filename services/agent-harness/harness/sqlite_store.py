"""SQLite engine behind the harness `Store` (PLAN.md §2.9, ARCHITECTURE.md §4).

This module is plain Python: no Modal, no network, no FastAPI. `modal_app.Store` is a thin
`@app.cls(max_containers=1)` wrapper that owns one instance of `SqliteStore` per container and
exposes each method as a `@modal.method`, so everything here is unit-testable on a temp dir.

Shape (ARCHITECTURE §4.3):
  * the hot database lives on the container's own disk (`/tmp/faultline.sqlite3`) with ordinary
    SQLite settings — WAL, synchronous=NORMAL, foreign_keys=ON — so none of the FUSE questions a
    Modal Volume raises (locking, mmap, a background commit capturing a half-written file) apply;
  * a checkpoint is `VACUUM INTO <snapshot>.tmp` → `os.replace` → `volume.commit()`. Only complete
    files are ever renamed into place, so a snapshot is always openable;
  * `@modal.enter` restores the snapshot into /tmp and runs migrations (`PRAGMA user_version` gates
    `migrations/000N_*.sql`);
  * every write marks the DB dirty; `checkpoint()` is called after each `run.finished`, every
    `checkpoint_interval_s` while dirty (background thread), and from `@modal.exit`.

`events` is the source of truth; `messages` / `blocks` / `llm_calls` are a projection written in the
SAME transaction as the event insert (§4.5), and `append_events` is idempotent on `(run_id, seq)` —
`run_episode` re-sends its whole in-memory event list at `run.finished`, which back-fills anything a
Store restart lost, and the projection is skipped for rows that were already there.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from faultline_common.log import get_logger

from . import config

log = get_logger(config.SVC)

# `Image.add_local_python_source` ships only the package's .py files — verified on Modal 1.5.5: the
# container had harness/*.py and NO harness/migrations/. So modal_app.py copies the directory in
# explicitly (outside the package, where the python-source mount cannot shadow it) and points here.
MIGRATIONS_DIR = pathlib.Path(
    os.environ.get("FAULTLINE_MIGRATIONS_DIR") or (pathlib.Path(__file__).resolve().parent / "migrations")
)

# Identity is scoping, not authentication (PLAN.md §1 non-goal / §2.9.1): `u_` + a uuid4.
USER_ID_RE = re.compile(r"^u_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Requests that carry no X-Faultline-User (the CLI, smoke scripts) are still persisted, under one
# well-known anonymous id, so a run always has a conversation and the FKs hold.
ANON_USER_ID = "u_00000000-0000-4000-8000-000000000000"

TERMINAL_STATUSES = {"ok", "error", "truncated", "unevaluated", "interrupted"}

# Columns of `runs` that finish_run()/create_run() may set directly.
RUN_COLUMNS = {
    "conversation_id", "user_id", "scenario_id", "model", "seed", "max_steps", "task_prompt",
    "episode_id", "status", "score", "error", "created_at", "started_at", "finished_at",
    "steps", "summary", "input_tokens", "output_tokens",
}
# Anything else a caller passes (usage detail, error_class, interruptions, worker_generation …)
# is preserved verbatim in `extra_json` and merged back into the record on read, so a RunRecord
# never loses a field this store does not know about.
_RESERVED = RUN_COLUMNS | {"run_id", "id", "evaluation", "events", "usage"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    """`<prefix>_<hex ms timestamp><12 hex random>` — sortable, no extra dependency (§4.3)."""
    return f"{prefix}_{int(time.time() * 1000):x}{secrets.token_hex(6)}"


def valid_user_id(value: str | None) -> bool:
    return bool(value) and bool(USER_ID_RE.match(value or ""))


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)


def _unjson(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):  # pragma: no cover - only a corrupted row gets here
        return None


class SqliteStore:
    """The single writer. One connection, one lock; every op is sub-millisecond on local disk."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        snapshot_path: str | os.PathLike[str] | None = None,
        commit: Callable[[], None] | None = None,
        reload: Callable[[], None] | None = None,
        checkpoint_interval_s: float = 15.0,
    ):
        self.db_path = str(db_path)
        self.snapshot_path = str(snapshot_path) if snapshot_path else None
        self._commit_volume = commit
        self._reload_volume = reload
        self.checkpoint_interval_s = checkpoint_interval_s
        self._lock = threading.RLock()
        self._ckpt_lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._dirty = False
        self._last_checkpoint_at: str | None = None
        self._last_checkpoint_error: str | None = None
        self._restored_from_snapshot = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> "SqliteStore":
        with self._lock:
            if self._conn is not None:
                return self
            if self.db_path not in (":memory:", ""):
                pathlib.Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._restore()
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            if self.db_path not in (":memory:", ""):
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            applied = self._migrate()
            self._assert_schema()
            log.info(
                "store.open",
                "sqlite ready",
                db=self.db_path,
                snapshot=self.snapshot_path,
                restored=self._restored_from_snapshot,
                user_version=self.user_version(),
                migrations_applied=applied or None,
            )
        return self

    def _restore(self) -> None:
        """Copy the Volume snapshot onto local disk, unless a hot DB is already there."""
        if not self.snapshot_path or self.db_path in (":memory:", ""):
            return
        if os.path.exists(self.db_path):
            return
        if self._reload_volume is not None:
            try:
                self._reload_volume()
            except Exception as exc:  # noqa: BLE001 - a fresh Volume has nothing to reload
                log.warn("store.reload_failed", f"{type(exc).__name__}: {exc}")
        if not os.path.exists(self.snapshot_path):
            log.info("store.restore", "no snapshot on the volume; starting empty",
                     snapshot=self.snapshot_path)
            return
        pathlib.Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.snapshot_path, self.db_path)
        self._restored_from_snapshot = True
        log.info("store.restore", "snapshot copied to local disk", snapshot=self.snapshot_path,
                 db=self.db_path, bytes=os.path.getsize(self.db_path))

    def _migrate(self) -> list[str]:
        version = self.user_version()
        applied: list[str] = []
        for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            n = int(path.name.split("_", 1)[0])
            if n <= version:
                continue
            assert self._conn is not None
            self._conn.executescript(path.read_text())
            self._conn.execute(f"PRAGMA user_version = {n}")
            applied.append(path.name)
        return applied

    def _assert_schema(self) -> None:
        """Fail at @modal.enter, not on the first request.

        A missing migrations directory used to leave an EMPTY database that answered every call with
        `no such table` 500s; crashing here makes that a container-start failure instead.
        """
        assert self._conn is not None
        tables = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"users", "conversations", "runs", "events", "messages", "blocks", "llm_calls"} - tables
        if missing:
            raise RuntimeError(
                f"store schema is incomplete: missing {sorted(missing)}; "
                f"migrations dir {MIGRATIONS_DIR} contains "
                f"{sorted(p.name for p in MIGRATIONS_DIR.glob('*')) if MIGRATIONS_DIR.is_dir() else 'NOTHING (dir absent)'}"
            )

    def user_version(self) -> int:
        assert self._conn is not None
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        self.stop_autocheckpoint()
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ plumbing
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.open()
        assert self._conn is not None
        return self._conn

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            conn = self.conn
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
                conn.commit()
                self._dirty = True
            except BaseException:
                conn.rollback()
                raise
            finally:
                cur.close()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ users
    def upsert_user(self, user_id: str, user_agent: str | None = None) -> dict[str, Any]:
        ts = now_iso()
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO users (id, created_at, last_seen_at, user_agent) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at, "
                "user_agent=COALESCE(excluded.user_agent, users.user_agent)",
                (user_id, ts, ts, user_agent),
            )
        row = self._one("SELECT * FROM users WHERE id=?", (user_id,))
        n = self._one("SELECT COUNT(*) AS n FROM conversations WHERE user_id=? AND archived_at IS NULL",
                      (user_id,))
        assert row is not None
        return {**dict(row), "conversations": int(n["n"] if n else 0)}

    # ------------------------------------------------------------------ conversations
    def create_conversation(self, user_id: str, scenario_id: str, title: str | None = None,
                            user_agent: str | None = None) -> dict[str, Any]:
        self.upsert_user(user_id, user_agent)
        cid = new_id("c")
        ts = now_iso()
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO conversations (id, user_id, scenario_id, title, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (cid, user_id, scenario_id, (title or scenario_id).strip()[:200] or scenario_id, ts, ts),
            )
        log.info("conversation.created", "conversation created", conversation_id=cid,
                 user_id=user_id, scenario_id=scenario_id)
        return self._conversation(cid) or {}

    def _conversation(self, conversation_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM conversations WHERE id=?", (conversation_id,))
        return dict(row) if row else None

    def _last_run(self, conversation_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM runs WHERE conversation_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (conversation_id,),
        )
        return self._run_summary(row) if row else None

    @staticmethod
    def _run_summary(row: sqlite3.Row, n_events: int | None = None) -> dict[str, Any]:
        """RunSummary (schemas.py keys on `id`; ARCHITECTURE §5.1 and the CLI key on `run_id`)."""
        out = {
            "id": row["id"],
            "run_id": row["id"],
            "status": row["status"],
            "scenario_id": row["scenario_id"],
            "model": row["model"],
            "score": row["score"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
        }
        if n_events is not None:
            out["events"] = n_events
        return out

    def list_conversations(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM conversations WHERE user_id=? AND archived_at IS NULL "
            "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (user_id, int(limit)),
        )
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "scenario_id": r["scenario_id"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "last_run": self._last_run(r["id"]),
            }
            for r in rows
        ]

    def get_conversation(self, conversation_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        conv = self._conversation(conversation_id)
        if conv is None or (user_id is not None and conv["user_id"] != user_id):
            return None
        runs = [
            self._run_summary(r, n_events=int(r["n_events"]))
            for r in self._query(
                "SELECT r.*, (SELECT COUNT(*) FROM events e WHERE e.run_id=r.id) AS n_events "
                "FROM runs r WHERE r.conversation_id=? ORDER BY r.created_at, r.rowid",
                (conversation_id,),
            )
        ]
        return {"conversation": conv, "runs": runs, "messages": self._messages(conversation_id)}

    def _messages(self, conversation_id: str) -> list[dict[str, Any]]:
        messages = [dict(r) for r in self._query(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY seq", (conversation_id,)
        )]
        by_id = {m["id"]: m for m in messages}
        for m in messages:
            m["blocks"] = []
        if not messages:
            return messages
        placeholders = ",".join("?" for _ in messages)
        rows = self._query(
            f"SELECT * FROM blocks WHERE message_id IN ({placeholders}) ORDER BY message_id, seq",
            tuple(m["id"] for m in messages),
        )
        for b in rows:
            by_id[b["message_id"]]["blocks"].append(
                {
                    "id": b["id"],
                    "message_id": b["message_id"],
                    "seq": b["seq"],
                    "type": b["type"],
                    "text": b["text"],
                    "tool_name": b["tool_name"],
                    "tool_use_id": b["tool_use_id"],
                    "input": _unjson(b["input_json"]),
                    "is_error": None if b["is_error"] is None else bool(b["is_error"]),
                    "exit_code": b["exit_code"],
                    "duration_ms": b["duration_ms"],
                    "fault": _unjson(b["fault_json"]),
                    "truncated": bool(b["truncated"]),
                }
            )
        return messages

    def update_conversation(self, conversation_id: str, user_id: str | None = None, *,
                            title: str | None = None, archived: bool | None = None) -> dict[str, Any] | None:
        conv = self._conversation(conversation_id)
        if conv is None or (user_id is not None and conv["user_id"] != user_id):
            return None
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            sets.append("title=?")
            params.append(title.strip()[:200] or conv["title"])
        if archived is not None:
            sets.append("archived_at=?")
            params.append(now_iso() if archived else None)
        if sets:
            sets.append("updated_at=?")
            params.append(now_iso())
            params.append(conversation_id)
            with self._tx() as cur:
                cur.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id=?", tuple(params))
        return self._conversation(conversation_id)

    def delete_conversation(self, conversation_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        """Soft archive — nothing is ever hard-deleted (ARCHITECTURE §4.3 'Ops')."""
        conv = self.update_conversation(conversation_id, user_id, archived=True)
        return None if conv is None else {"archived": True, "id": conv["id"]}

    # ------------------------------------------------------------------ runs
    def create_run(self, record: dict[str, Any]) -> dict[str, Any]:
        rec = dict(record or {})
        run_id = rec.get("run_id") or rec.get("id") or new_id("r")
        existing = self.get_run(run_id)
        if existing is not None:
            # Idempotent: a re-create (a replayed spawn, a re-import) must not mint a second
            # conversation for a run that already has one.
            return existing
        user_id = rec.get("user_id") or ANON_USER_ID
        scenario_id = rec.get("scenario_id") or ""
        self.upsert_user(user_id)
        conversation_id = rec.get("conversation_id")
        if not conversation_id or self._conversation(conversation_id) is None:
            conversation_id = self.create_conversation(
                user_id, scenario_id, rec.get("title") or scenario_id
            )["id"]
        extra = {k: v for k, v in rec.items() if k not in _RESERVED}
        usage = rec.get("usage") or {}
        with self._tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO runs (id, conversation_id, user_id, scenario_id, model, seed, "
                "max_steps, task_prompt, episode_id, status, score, evaluation_json, input_tokens, "
                "output_tokens, error, created_at, started_at, finished_at, steps, summary, extra_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, conversation_id, user_id, scenario_id,
                    rec.get("model") or config.DEFAULT_MODEL, rec.get("seed"),
                    int(rec.get("max_steps") or config.DEFAULT_MAX_STEPS),
                    rec.get("task_prompt") or "", rec.get("episode_id"),
                    rec.get("status") or "queued",
                    rec.get("score") if rec.get("score") is not None
                    else (rec.get("evaluation") or {}).get("score"),
                    _json(rec.get("evaluation")),
                    int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
                    rec.get("error"), rec.get("created_at") or now_iso(), rec.get("started_at"),
                    rec.get("finished_at"), rec.get("steps"), rec.get("summary"),
                    _json(extra) if extra else None,
                ),
            )
        log.info("run.created", "run row stored", run_id=run_id, conversation_id=conversation_id,
                 user_id=user_id, status=rec.get("status"))
        return self.get_run(run_id) or {}

    def finish_run(self, run_id: str, fields: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        """Merge scalar fields into a run row (status, finished_at, evaluation, usage, …).

        Named `finish_run` because that is what PLAN.md §3.2 calls it, but it is the general
        update: `RunStore.update()` routes here so `api.py` and `loop.py` keep one verb.
        """
        merged: dict[str, Any] = {**(fields or {}), **kw}
        row = self._one("SELECT * FROM runs WHERE id=?", (run_id,))
        if row is None:
            raise KeyError(run_id)
        sets: list[str] = []
        params: list[Any] = []
        # ONE accumulated extra_json for the whole update. Writing `extra_json=?` once per unknown
        # key made `UPDATE … SET extra_json=?, extra_json=?` — which SQLite accepts and resolves as
        # last-write-wins, silently dropping every earlier key. That was invisible until a single
        # update carried several of them (status + error_class + interruptions + worker_generation).
        extra: dict[str, Any] = dict(_unjson(row["extra_json"]) or {})
        extra_dirty = False
        for key, value in merged.items():
            if key in ("run_id", "id", "events"):
                continue
            if key == "evaluation":
                sets.append("evaluation_json=?")
                params.append(_json(value))
                if isinstance(value, dict) and value.get("score") is not None:
                    sets.append("score=?")
                    params.append(value.get("score"))
                continue
            if key == "usage" and isinstance(value, dict):
                sets += ["input_tokens=?", "output_tokens=?"]
                params += [int(value.get("input_tokens") or 0), int(value.get("output_tokens") or 0)]
                extra["usage"] = value
                extra_dirty = True
                continue
            if key in RUN_COLUMNS:
                sets.append(f"{key}=?")
                params.append(value)
                continue
            extra[key] = value
            extra_dirty = True
        if extra_dirty:
            sets.append("extra_json=?")
            params.append(_json(extra))
        if sets:
            params.append(run_id)
            with self._tx() as cur:
                cur.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", tuple(params))
            self._touch_conversation(run_id)
        if merged.get("status") in TERMINAL_STATUSES:
            self.checkpoint()
        return self.get_run(run_id) or {}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM runs WHERE id=?", (run_id,))
        if row is None:
            return None
        extra = _unjson(row["extra_json"]) or {}
        usage = extra.pop("usage", None) or {}
        record: dict[str, Any] = {
            "run_id": row["id"],
            "status": row["status"],
            "scenario_id": row["scenario_id"],
            "model": row["model"],
            "seed": row["seed"],
            "max_steps": row["max_steps"],
            "episode_id": row["episode_id"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "events": [self._event(r) for r in self._query(
                "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))],
            "evaluation": _unjson(row["evaluation_json"]),
            "usage": {
                "input_tokens": int(usage.get("input_tokens") or row["input_tokens"] or 0),
                "output_tokens": int(usage.get("output_tokens") or row["output_tokens"] or 0),
                **({"cache_read_input_tokens": usage["cache_read_input_tokens"]}
                   if usage.get("cache_read_input_tokens") else {}),
                **({"cache_creation_input_tokens": usage["cache_creation_input_tokens"]}
                   if usage.get("cache_creation_input_tokens") else {}),
            },
            "error": row["error"],
            "conversation_id": row["conversation_id"],
            "user_id": row["user_id"],
            "task_prompt": row["task_prompt"],
            "steps": row["steps"],
            "summary": row["summary"],
            "score": row["score"],
            # PLAN.md §2.11 additions. Defaulted here (not only when a run happens to have set
            # them) so every GET /runs/{id} answers the taxonomy's questions — including the 21
            # runs imported from before the taxonomy existed.
            "error_class": None,
            "interruptions": [],
            "worker_generation": 1,
        }
        record.update(extra)
        return record

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["seq"],
            "ts": row["ts"],
            "run_id": row["run_id"],
            "type": row["type"],
            "step": row["step"],
            "data": _unjson(row["data"]) or {},
        }

    def list_runs(self, limit: int = 50, user_id: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT r.*, (SELECT COUNT(*) FROM events e WHERE e.run_id=r.id) AS n_events FROM runs r ")
        params: list[Any] = []
        if user_id:
            sql += "WHERE r.user_id=? "
            params.append(user_id)
        sql += "ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?"
        params.append(int(limit))
        return [
            {**self._run_summary(r, n_events=int(r["n_events"])), "conversation_id": r["conversation_id"]}
            for r in self._query(sql, tuple(params))
        ]

    def events_after(self, run_id: str, after: int = -1, limit: int = 2000) -> dict[str, Any]:
        """The SSE tail: everything with `seq > after`, plus enough state to close the stream."""
        row = self._one("SELECT status FROM runs WHERE id=?", (run_id,))
        if row is None:
            return {"run_id": run_id, "exists": False, "status": None, "events": []}
        rows = self._query(
            "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
            (run_id, int(after), int(limit)),
        )
        return {
            "run_id": run_id,
            "exists": True,
            "status": row["status"],
            "events": [self._event(r) for r in rows],
        }

    # ------------------------------------------------------------------ events + projection
    def append_events(self, run_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Insert events idempotently on (run_id, seq) and project them into the transcript.

        A row that was already there is skipped entirely — insert AND projection — which is what
        makes `run_episode`'s full re-send at `run.finished` a no-op for everything already stored.
        """
        row = self._one("SELECT conversation_id FROM runs WHERE id=?", (run_id,))
        if row is None:
            raise KeyError(run_id)
        conversation_id = row["conversation_id"]
        appended = 0
        finished = False
        with self._tx() as cur:
            for event in events or []:
                seq = event.get("id")
                if seq is None:
                    continue
                cur.execute(
                    "INSERT OR IGNORE INTO events (run_id, seq, ts, type, step, data) VALUES (?,?,?,?,?,?)",
                    (run_id, int(seq), event.get("ts") or now_iso(), event.get("type") or "log",
                     event.get("step"), _json(event.get("data") or {}) or "{}"),
                )
                if cur.rowcount == 0:  # already stored: never project twice
                    continue
                appended += 1
                if event.get("type") == "run.finished":
                    finished = True
                self._project(cur, conversation_id, run_id, event)
            if appended:
                cur.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), conversation_id))
        total = self._one("SELECT COUNT(*) AS n FROM events WHERE run_id=?", (run_id,))
        if finished:
            self.checkpoint(force=True)
        return {"run_id": run_id, "appended": appended, "count": int(total["n"]) if total else 0}

    # --- projection helpers (ARCHITECTURE §4.5) -------------------------------------------------
    def _message(self, cur: sqlite3.Cursor, conversation_id: str, run_id: str, role: str,
                 step: int | None, ts: str) -> str:
        if step is None:
            found = cur.execute(
                "SELECT id FROM messages WHERE run_id=? AND role=? AND step IS NULL LIMIT 1",
                (run_id, role),
            ).fetchone()
        else:
            found = cur.execute(
                "SELECT id FROM messages WHERE run_id=? AND role=? AND step=? LIMIT 1",
                (run_id, role, int(step)),
            ).fetchone()
        if found:
            return found["id"]
        seq_row = cur.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        mid = new_id("m")
        cur.execute(
            "INSERT INTO messages (id, conversation_id, run_id, seq, role, step, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (mid, conversation_id, run_id, int(seq_row["n"]), role, step, ts),
        )
        return mid

    @staticmethod
    def _block(cur: sqlite3.Cursor, message_id: str, btype: str, **cols: Any) -> None:
        seq_row = cur.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM blocks WHERE message_id=?", (message_id,)
        ).fetchone()
        cur.execute(
            "INSERT INTO blocks (id, message_id, seq, type, text, tool_name, tool_use_id, input_json, "
            "is_error, exit_code, duration_ms, fault_json, truncated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id("b"), message_id, int(seq_row["n"]), btype, cols.get("text"),
                cols.get("tool_name"), cols.get("tool_use_id"), _json(cols.get("input")),
                None if cols.get("is_error") is None else int(bool(cols.get("is_error"))),
                cols.get("exit_code"), cols.get("duration_ms"), _json(cols.get("fault")),
                int(bool(cols.get("truncated"))),
            ),
        )

    def _project(self, cur: sqlite3.Cursor, conversation_id: str, run_id: str,
                 event: dict[str, Any]) -> None:
        etype = event.get("type") or ""
        data = event.get("data") or {}
        step = event.get("step")
        ts = event.get("ts") or now_iso()

        if etype == "run.started":
            cur.execute("UPDATE runs SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?",
                        (ts, run_id))
        elif etype == "episode.reset":
            task_prompt = str(data.get("task_prompt") or "")
            cur.execute("UPDATE runs SET episode_id=?, task_prompt=? WHERE id=?",
                        (data.get("episode_id"), task_prompt, run_id))
            mid = self._message(cur, conversation_id, run_id, "user", None, ts)
            self._block(cur, mid, "text", text=task_prompt)
        elif etype in ("turn.text", "turn.thinking"):
            mid = self._message(cur, conversation_id, run_id, "assistant", step, ts)
            self._block(cur, mid, "thinking" if etype == "turn.thinking" else "text",
                        text=str(data.get("text") or ""))
        elif etype == "tool.call":
            mid = self._message(cur, conversation_id, run_id, "assistant", step, ts)
            self._block(cur, mid, "tool_use", tool_name=data.get("tool"),
                        tool_use_id=data.get("tool_use_id"), input=data.get("input") or {})
        elif etype == "tool.result":
            # Anthropic requires tool results in a USER turn; one per step, exactly like the
            # messages=[…] array the model saw.
            mid = self._message(cur, conversation_id, run_id, "user", step, ts)
            output = str(data.get("output") or "")
            self._block(
                cur, mid, "tool_result",
                text=output,
                tool_name=data.get("tool"),
                tool_use_id=data.get("tool_use_id"),
                is_error=bool(data.get("is_error")),
                exit_code=_exit_code_of(output),
                duration_ms=data.get("duration_ms"),
                fault=data.get("fault"),
                truncated="…[truncated" in output,
            )
        elif etype == "llm.call":
            usage = data.get("usage") or {}
            cur.execute(
                "INSERT OR IGNORE INTO llm_calls (id, run_id, step, attempt, model, request_id, "
                "stop_reason, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                "started_at, duration_ms, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id("l"), run_id, int(step or 0), int(data.get("attempt") or 1),
                    str(data.get("model") or ""), data.get("request_id"), data.get("stop_reason"),
                    usage.get("input_tokens"), usage.get("output_tokens"),
                    usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"),
                    ts, data.get("duration_ms"), data.get("error"),
                ),
            )
            cur.execute(
                "UPDATE runs SET input_tokens=input_tokens+?, output_tokens=output_tokens+? WHERE id=?",
                (int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), run_id),
            )
        elif etype == "episode.evaluated":
            cur.execute("UPDATE runs SET score=?, evaluation_json=? WHERE id=?",
                        (data.get("score"), _json(data), run_id))
        elif etype == "run.finished":
            usage = data.get("usage") or {}
            # The full usage (both cache counters) goes in with the status flip, not in the separate
            # finish_run() that follows: the SSE stream closes the instant the status is terminal, so
            # a client that fetches GET /runs/{id} on the `done` frame would otherwise race the
            # update and see token counts without the cache columns.
            row = cur.execute("SELECT extra_json FROM runs WHERE id=?", (run_id,)).fetchone()
            extra = dict((_unjson(row["extra_json"]) if row is not None else None) or {})
            if usage:
                extra["usage"] = usage
            cur.execute(
                "UPDATE runs SET status=?, finished_at=?, error=?, steps=COALESCE(?, steps), "
                "input_tokens=?, output_tokens=?, extra_json=? WHERE id=?",
                (
                    data.get("status") or "ok", ts, data.get("error"), data.get("steps"),
                    int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
                    _json(extra) if extra else None, run_id,
                ),
            )
        # fault.fired / workspace.diff / log / interruption / run.resumed / episode.sandbox:
        # event row only — the UI reads those straight from the event log (§4.5).

    def _touch_conversation(self, run_id: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE conversations SET updated_at=? WHERE id=(SELECT conversation_id FROM runs WHERE id=?)",
                (now_iso(), run_id),
            )

    def llm_calls(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT * FROM llm_calls WHERE run_id=? ORDER BY step, attempt", (run_id,))]

    # ------------------------------------------------------------------ checkpoint
    def checkpoint(self, force: bool = False) -> dict[str, Any]:
        """VACUUM INTO a temp file on the Volume, rename it into place, commit (ARCHITECTURE §4.3)."""
        if not self.snapshot_path:
            self._dirty = False
            return self.health()
        # Only the VACUUM touches the database, so the connection lock is held for that alone —
        # writing ~1 MB across the FUSE mount and committing the Volume takes seconds, and an SSE
        # poll must not queue behind it. `_ckpt_lock` keeps two checkpoints off the same temp file.
        with self._ckpt_lock:
            tmp = f"{self.snapshot_path}.tmp"
            t0 = time.perf_counter()
            try:
                with self._lock:
                    if not self._dirty and not force:
                        return self.health()
                    pathlib.Path(self.snapshot_path).parent.mkdir(parents=True, exist_ok=True)
                    if os.path.exists(tmp):
                        os.remove(tmp)
                    self._vacuum_into(tmp)
                    # Cleared inside the lock: no write can slip between the VACUUM and this line,
                    # so "not dirty" always means "everything committed is in the snapshot".
                    self._dirty = False
                vacuum_ms = int((time.perf_counter() - t0) * 1000)
                os.replace(tmp, self.snapshot_path)
                if self._commit_volume is not None:
                    self._commit_volume()
                self._last_checkpoint_at = now_iso()
                self._last_checkpoint_error = None
                log.info("store.checkpoint", "snapshot committed", snapshot=self.snapshot_path,
                         bytes=os.path.getsize(self.snapshot_path), vacuum_ms=vacuum_ms,
                         dur_ms=int((time.perf_counter() - t0) * 1000))
            except Exception as exc:  # noqa: BLE001 - a failed checkpoint must not fail the run
                self._dirty = True  # unsaved writes remain unsaved; try again on the next tick
                self._last_checkpoint_error = f"{type(exc).__name__}: {exc}"[:300]
                log.error("store.checkpoint_failed", self._last_checkpoint_error)
                with contextlib.suppress(OSError):
                    if os.path.exists(tmp):
                        os.remove(tmp)
        return self.health()

    def _vacuum_into(self, target: str) -> None:
        try:
            self.conn.execute("VACUUM INTO ?", (target,))
        except sqlite3.Error:
            # Older SQLite builds reject a bound parameter as the VACUUM target.
            self.conn.execute("VACUUM INTO '{}'".format(target.replace("'", "''")))

    def start_autocheckpoint(self) -> None:
        """Checkpoint every `checkpoint_interval_s` while there are unsaved writes."""
        if self._thread is not None or not self.snapshot_path:
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(self.checkpoint_interval_s):
                if self._dirty:
                    self.checkpoint()

        self._thread = threading.Thread(target=_loop, name="store-checkpoint", daemon=True)
        self._thread.start()

    def stop_autocheckpoint(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    # ------------------------------------------------------------------ health
    def health(self) -> dict[str, Any]:
        try:
            runs = self._one("SELECT COUNT(*) AS n FROM runs")
            events = self._one("SELECT COUNT(*) AS n FROM events")
            convos = self._one("SELECT COUNT(*) AS n FROM conversations")
            ok = True
        except sqlite3.Error as exc:  # pragma: no cover - only a broken DB gets here
            log.error("store.health_failed", f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200],
                    "last_checkpoint_at": self._last_checkpoint_at}
        return {
            "ok": ok,
            "last_checkpoint_at": self._last_checkpoint_at,
            "last_checkpoint_error": self._last_checkpoint_error,
            "dirty": self._dirty,
            "db_path": self.db_path,
            "snapshot_path": self.snapshot_path,
            "restored_from_snapshot": self._restored_from_snapshot,
            "user_version": self.user_version(),
            "runs": int(runs["n"]) if runs else 0,
            "events": int(events["n"]) if events else 0,
            "conversations": int(convos["n"]) if convos else 0,
        }


def _exit_code_of(output: str) -> int | None:
    """run_command results carry `exit_code` inside the JSON output; blocks store it as a column."""
    if not output or not output.lstrip().startswith("{"):
        return None
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return None
    code = payload.get("exit_code") if isinstance(payload, dict) else None
    return int(code) if isinstance(code, int) else None

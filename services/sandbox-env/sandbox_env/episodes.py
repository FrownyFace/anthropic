"""Episode lifecycle: reset / observe / delete, plus the ledger.

All state lives in `modal.Dict("faultline-episodes")` keyed by episode_id, so the FastAPI
containers stay stateless and any of them can serve any tool call. The value is a plain dict
(Modal pickles it) shaped like:

    {episode_id, scenario_id, seed, sandbox_id, created_at, step,
     fault_plan: FaultPlan, fault_hits: [int|None],   # remaining hits per fault, plan order
     ledger: [LedgerEntry], baseline: {path: sha256}, done}

The *fault plan and the ledger never enter the sandbox*. That is the whole trust boundary: the
agent's shell cannot read what is about to fail to it, and cannot edit the record used to grade it.
"""

from __future__ import annotations

import io
import os
import tarfile
import time
from datetime import datetime, timezone
from typing import Any

from faultline_common.log import get_logger
from faultline_common.schemas import (
    FaultFired,
    FaultPlan,
    FileDiff,
    FileEntry,
    InterruptionReport,
    LedgerEntry,
    ObserveResponse,
    ResetResponse,
)

from . import faults, scenarios, workspace
from .paths import WORKSPACE, normalize_rel
from .scenarios import ScenarioBundle
from .util import args_digest, new_id, now_iso
from .workspace import SKIP_DIRS, Workspace

log = get_logger("sandbox-env")

EPISODES_DICT = os.environ.get("FAULTLINE_EPISODES_DICT", "faultline-episodes")
DIFF_CAP = 6_000
#: …and a cap on how MANY files get a diff. DIFF_CAP bounds one diff; nothing bounded the count,
#: and the agent controls it (`for i in $(seq 500); do echo x > f$i; done`). Two things break at
#: that point: the observe response the harness puts into an event and streams to the browser grows
#: without limit, and the helper is invoked with the whole path list in ONE argv token, which Linux
#: caps at 128 KiB — so a big enough change set turns observe into an ESANDBOX. The `files` list
#: still reports every change; only the unified diffs are capped.
DIFF_FILES_CAP = int(os.environ.get("EPISODE_DIFF_FILES_CAP", "50"))

#: Cost guardrail. An episode whose owner crashed (harness killed, browser closed, script ^C'd)
#: leaves a running Modal Sandbox behind. Three independent things reclaim it, in increasing order
#: of desperation: `DELETE /episodes/{id}` (the normal path), this TTL sweep (runs on every reset,
#: so the system cleans itself as long as anyone keeps using it), and `modal run …::reap` (manual).
#: The sandbox's own `timeout`/`idle_timeout` are the backstop under all three.
#: Default 1800 s == `FAULTLINE_SANDBOX_TIMEOUT`, so the sweep and the sandbox expire together.
EPISODE_TTL_S = int(os.environ.get("EPISODE_TTL_S", "1800"))
#: terminated episodes are kept this long so a late `GET /episodes/{id}` still answers, then dropped
EPISODE_PURGE_S = int(os.environ.get("EPISODE_PURGE_S", "86400"))
#: never let a sweep turn into an unbounded scan of the Dict on the reset path
EPISODE_SWEEP_MAX = int(os.environ.get("EPISODE_SWEEP_MAX", "200"))
#: …and never let one sweep turn into an unbounded number of Modal round trips either. The scan is
#: one `items()` call, but every expiry costs a terminate + a Dict write (~0.3 s) and every purge a
#: pop. A backlog of 200 expired episodes would therefore add a minute or more to the `POST
#: /episodes` that triggered it — and reset is a web request, hard-capped at 150 s by Modal
#: (PLAN.md §2.3). The sweep is idempotent, so a budget just means the next one finishes the job.
EPISODE_SWEEP_MAX_ACTIONS = int(os.environ.get("EPISODE_SWEEP_MAX_ACTIONS", "25"))
#: how often a container may sweep from the reset path (see `sweep_quietly`)
EPISODE_SWEEP_INTERVAL_S = int(os.environ.get("EPISODE_SWEEP_INTERVAL_S", "300"))


class EpisodeNotFound(KeyError):
    pass


# --------------------------------------------------------------------------- state store


class MemoryStore:
    """modal.Dict-compatible in-memory store (tests, and `python -m sandbox_env` dry runs)."""

    def __init__(self) -> None:
        self._d: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def put(self, key: str, value: Any) -> None:
        self._d[key] = value

    def contains(self, key: str) -> bool:
        return key in self._d

    def pop(self, key: str, *default: Any) -> Any:
        return self._d.pop(key, *default)

    def keys(self):
        return list(self._d.keys())

    def items(self):
        return list(self._d.items())


_STORE: Any | None = None


def store() -> Any:
    """The episode store. Lazily binds the Modal Dict so imports work without Modal."""
    global _STORE
    if _STORE is None:
        import modal

        _STORE = modal.Dict.from_name(EPISODES_DICT, create_if_missing=True)
        log.info("state.bound", "episode dict bound", dict_name=EPISODES_DICT)
    return _STORE


def set_store(s: Any) -> None:
    """Swap the store (tests)."""
    global _STORE
    _STORE = s


def load(episode_id: str) -> dict[str, Any]:
    ep = store().get(episode_id)
    if not ep:
        raise EpisodeNotFound(episode_id)
    return ep


def save(ep: dict[str, Any]) -> None:
    store().put(ep["episode_id"], ep)


def workspace_for(ep: dict[str, Any]) -> Workspace:
    return workspace.open_workspace(ep["sandbox_id"])


# --------------------------------------------------------------------------- fixture packing


def build_workspace_tar(bundle: ScenarioBundle) -> tuple[bytes, list[str]]:
    """Materialise the episode's starting tree as one tar: fixture + setup overlays - sticky faults.

    Doing the overlay writes and the sticky deletions *before* upload means the sandbox only ever
    sees the final state — there is no window in which the agent could observe the pristine file
    that is supposed to be missing, and reset costs one upload instead of one RPC per edit.
    """
    files: dict[str, bytes] = {}
    root = bundle.fixture_dir
    if not root.is_dir():
        raise FileNotFoundError(f"fixture directory not found: {root}")
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        parts = rel.split("/")
        if any(part in SKIP_DIRS for part in parts) or rel.endswith(".pyc"):
            continue
        files[rel] = p.read_bytes()

    for op in bundle.setup:
        files[normalize_rel(op.path)] = op.content

    removed: list[str] = []
    for f in bundle.fault_plan.faults:
        if f.kind == "missing_file" and f.mode == "sticky":
            rel = normalize_rel(f.path)
            files.pop(rel, None)
            removed.append(rel)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for rel, data in sorted(files.items()):
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue(), removed


# --------------------------------------------------------------------------- reset


def reset(scenario_id: str, seed: int | None = None) -> ResetResponse:
    bundle = scenarios.get_bundle(scenario_id)
    episode_id = new_id("ep")
    tar, sticky_removed = build_workspace_tar(bundle)

    # Reclaim abandoned episodes *before* provisioning another sandbox, so a caller that resets in
    # a loop bounds the fleet instead of growing it. Never allowed to fail a reset.
    sweep_quietly()

    ws = workspace.create_workspace()
    try:
        ws.upload_tar(tar, WORKSPACE)
        ws.snapshot_baseline()
        baseline_raw = ws.sha_map(WORKSPACE)
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt/CancelledError between "sandbox
        # created" and "episode recorded" is exactly the window where a sandbox becomes a stray
        # nobody can name any more.
        _terminate_quietly(ws, episode_id, "reset failed before the episode was recorded")
        raise

    baseline = {rel: meta["sha256"] for rel, meta in baseline_raw.items()}
    ep: dict[str, Any] = {
        "episode_id": episode_id,
        "scenario_id": bundle.id,
        "seed": seed,
        "sandbox_id": ws.sandbox_id,
        "created_at": now_iso(),
        "step": 0,
        "fault_plan": bundle.fault_plan.model_dump(),
        "fault_hits": faults.initial_hits(bundle.fault_plan),
        "ledger": [],
        "baseline": baseline,
        "sticky_removed": sticky_removed,
        "max_steps": bundle.public.max_steps,
        "workspace_root": WORKSPACE,
        "done": False,
    }
    try:
        save(ep)
    except BaseException:
        # The Dict write is the moment the sandbox becomes reachable by id. If it fails, the
        # sandbox exists and nothing in the system knows about it — terminate it here or it is
        # only reclaimable by `reap`.
        _terminate_quietly(ws, episode_id, "episode could not be recorded")
        raise

    files = [
        FileEntry(path=rel, size=int(meta.get("size", 0)), sha256=meta["sha256"], status="unchanged")
        for rel, meta in sorted(baseline_raw.items())
    ]
    log.info(
        "episode.reset",
        "episode provisioned",
        episode_id=episode_id,
        scenario_id=bundle.id,
        seed=seed,
        sandbox_id=ws.sandbox_id,
        files=len(files),
        sticky_removed=sticky_removed or None,
        faults=len(bundle.fault_plan.faults),
    )
    return ResetResponse(
        episode_id=episode_id,
        scenario=bundle.public,
        workspace_root=WORKSPACE,
        files=files,
        sandbox_id=ws.sandbox_id,
    )


# --------------------------------------------------------------------------- observe


def faults_fired(ep: dict[str, Any]) -> list[FaultFired]:
    """Only what already happened. Pending faults stay in the Dict, invisible to everyone."""
    out: list[FaultFired] = []
    for entry in ep.get("ledger", []):
        f = entry.get("fault")
        if f:
            out.append(FaultFired.model_validate(f))
    return out


def observe(episode_id: str, ws: Workspace | None = None) -> ObserveResponse:
    ep = load(episode_id)
    baseline: dict[str, str] = ep.get("baseline", {})
    if ep.get("done") and ep.get("terminated"):
        # sandbox is gone; report the last known tree without touching Modal
        return ObserveResponse(
            episode_id=episode_id,
            step=ep.get("step", 0),
            files=[FileEntry(path=p, size=0, sha256=s, status="unchanged") for p, s in sorted(baseline.items())],
            diffs=[],
            faults_fired=faults_fired(ep),
            done=True,
        )
    ws = ws or workspace_for(ep)
    cur = ws.sha_map(WORKSPACE)

    files: list[FileEntry] = []
    changed: list[str] = []
    for rel in sorted(set(cur) | set(baseline)):
        meta = cur.get(rel)
        base_sha = baseline.get(rel)
        if meta is None:
            files.append(FileEntry(path=rel, size=0, sha256=base_sha or "", status="deleted"))
            changed.append(rel)
        elif base_sha is None:
            files.append(FileEntry(path=rel, size=int(meta.get("size", 0)), sha256=meta["sha256"], status="added"))
            changed.append(rel)
        elif meta["sha256"] != base_sha:
            files.append(FileEntry(path=rel, size=int(meta.get("size", 0)), sha256=meta["sha256"], status="modified"))
            changed.append(rel)
        else:
            files.append(FileEntry(path=rel, size=int(meta.get("size", 0)), sha256=meta["sha256"], status="unchanged"))

    diffed = changed[:DIFF_FILES_CAP] if DIFF_FILES_CAP > 0 else changed
    diffs = [FileDiff(**d) for d in ws.diffs(diffed, DIFF_CAP)] if diffed else []
    log.info(
        "episode.observe",
        "workspace observed",
        episode_id=episode_id,
        step=ep.get("step", 0),
        files=len(files),
        changed=len(changed),
        diffed=len(diffs),
        diffs_capped=True if len(diffed) < len(changed) else None,
        faults_fired=len(faults_fired(ep)),
    )
    return ObserveResponse(
        episode_id=episode_id,
        step=ep.get("step", 0),
        files=files,
        diffs=diffs,
        faults_fired=faults_fired(ep),
        done=bool(ep.get("done", False)),
    )


# --------------------------------------------------------------------------- ledger


def append_ledger(ep: dict[str, Any], entry: LedgerEntry) -> None:
    ep.setdefault("ledger", []).append(entry.model_dump())


def ledger_entries(ep: dict[str, Any]) -> list[LedgerEntry]:
    return [LedgerEntry.model_validate(e) for e in ep.get("ledger", [])]


def fault_plan_of(ep: dict[str, Any]) -> FaultPlan:
    return FaultPlan.model_validate(ep.get("fault_plan") or {})


# --------------------------------------------------------------------------- interruptions
#
# The harness is the only component that knows it *stopped listening*. When its worker is killed or
# its HTTP request is aborted mid-call, sandbox-env happily finishes the work and writes an `ok` row
# — which is the truth about the sandbox and a lie about the episode, because the agent never
# received that answer and has to treat the write as "may or may not have landed". `POST
# /episodes/{id}/interruptions` is how the harness tells us, and `interrupted: true` is how the
# grader learns to apply the `ack_lost` rule (GRADING.md `verified_before_rewrite`) to a real failure.


def _row_touches(entry: dict[str, Any], path: str) -> bool:
    """GRADING.md `touches`, against a raw ledger row."""
    if entry.get("tool") == "run_command":
        return faults.touches(path, "run_command", {"command": entry.get("command") or ""})
    return faults.normalize_token(entry.get("path") or "") == faults.normalize_token(path)


def match_interrupted(rows: list[dict[str, Any]], report: InterruptionReport) -> int | None:
    """Index of the most recent row the report could be about, or None.

    `outcome in {ok, ack_lost}` is the whole point: those are the calls whose side effect really
    happened. A row that already failed at the boundary (`short_circuit`) or in the sandbox
    (`error`) needs no annotation — the agent got a definite answer for it.

    Already-`interrupted` rows are eligible on purpose, so a harness that retries the report is
    idempotent instead of walking backwards and marking a second, unrelated call.
    """
    for i in range(len(rows) - 1, -1, -1):
        row_ = rows[i]
        if row_.get("tool") != report.tool:
            continue
        if row_.get("outcome") not in ("ok", "ack_lost"):
            continue
        if report.args_digest:
            if row_.get("args_digest") != report.args_digest:
                continue
        elif report.path:
            if not _row_touches(row_, report.path):
                continue
        return i
    return None


def report_interruption(episode_id: str, report: InterruptionReport) -> tuple[LedgerEntry, bool]:
    """Mark the call the harness never heard back from. Returns (row, matched).

    When nothing matches, the call never reached this service at all (killed in the client, or the
    connection died before the request landed). That is still a fact about the episode, so it is
    recorded as an annotation row — `outcome: error`, `origin: real`, `interrupted: true` — rather
    than dropped, otherwise the evidence and the UI would show a step with no trace whatsoever.
    """
    ep = load(episode_id)
    rows: list[dict[str, Any]] = ep.setdefault("ledger", [])
    idx = match_interrupted(rows, report)

    if idx is not None:
        rows[idx]["interrupted"] = True
        save(ep)
        entry = LedgerEntry.model_validate(rows[idx])
        log.warn(
            "episode.interrupted",
            f"{report.tool} at step {entry.step} was interrupted ({report.code})",
            episode_id=episode_id, tool=report.tool, path=report.path, step=entry.step,
            outcome=entry.outcome, code=report.code, layer=report.layer, matched=True,
        )
        return entry, True

    entry = LedgerEntry(
        step=int(ep.get("step", 0)),
        ts=now_iso(),
        tool=report.tool,
        args_digest=report.args_digest
        or args_digest({"tool": report.tool, "path": report.path, "at": report.at}),
        path=report.path,
        command=None,
        mutating=faults.is_mutating(report.tool, {"path": report.path or ""}),
        fault=None,
        outcome="error",
        exit_code=None,
        duration_ms=0,
        origin="real",
        error_code=report.code,
        interrupted=True,
    )
    append_ledger(ep, entry)
    save(ep)
    log.warn(
        "episode.interrupted",
        f"{report.tool} never reached the sandbox ({report.code}); annotated the ledger",
        episode_id=episode_id, tool=report.tool, path=report.path, step=entry.step,
        code=report.code, layer=report.layer, matched=False,
    )
    return entry, False


# --------------------------------------------------------------------------- delete


def _terminate_quietly(ws: Workspace, episode_id: str, why: str) -> None:
    """Best-effort terminate on an error path. Never raises: the original error must win."""
    sandbox_id = getattr(ws, "sandbox_id", "?")
    try:
        ws.terminate()
        log.warn("episode.abandoned", why, episode_id=episode_id, sandbox_id=sandbox_id,
                 terminated=True)
    except BaseException as exc:  # noqa: BLE001
        log.error("episode.abandon_failed", f"{why}; terminate also failed: {exc}",
                  episode_id=episode_id, sandbox_id=sandbox_id, terminated=False)


def delete(episode_id: str) -> bool:
    """Terminate the sandbox and mark the episode done. Idempotent."""
    try:
        ep = load(episode_id)
    except EpisodeNotFound:
        return False
    if not ep.get("terminated"):
        try:
            workspace_for(ep).terminate()
        except Exception as exc:  # noqa: BLE001 - a dead sandbox is still a successful delete
            log.warn("episode.terminate_failed", str(exc), episode_id=episode_id, sandbox_id=ep.get("sandbox_id"))
    ep["done"] = True
    ep["terminated"] = True
    ep["finished_at"] = now_iso()
    save(ep)
    log.info(
        "episode.deleted",
        "sandbox terminated",
        episode_id=episode_id,
        sandbox_id=ep.get("sandbox_id"),
        step=ep.get("step", 0),
    )
    return True


# --------------------------------------------------------------------------- lifecycle / ops
#
# Everything below is cost control. A Modal Sandbox costs money for as long as it runs, and the
# thing that keeps it alive — an episode nobody will ever DELETE — is invisible unless we look.


def age_s(ep: dict[str, Any], now: float | None = None) -> float:
    """Seconds since reset. An unparsable/missing `created_at` reads as 0 (never expire it)."""
    raw = ep.get("created_at")
    if not raw:
        return 0.0
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    return max(0.0, now - ts.timestamp())


def episode_ids(limit: int = EPISODE_SWEEP_MAX) -> list[str]:
    """Ids currently in the store, newest-first is not guaranteed by modal.Dict — order is its own."""
    try:
        keys = list(store().keys())
    except Exception as exc:  # noqa: BLE001 - an ops listing must never take the service down
        log.warn("episode.keys_failed", str(exc))
        return []
    return keys[:limit] if limit and limit > 0 else keys


def iter_episodes(limit: int = EPISODE_SWEEP_MAX) -> list[tuple[str, dict[str, Any]]]:
    """Every episode record, in as few round trips as the store allows.

    `modal.Dict.items()` streams the whole Dict in ONE call; `keys()` + `get()` per id costs an
    RPC each (~50 ms), which turned a 125-episode Dict into a 6 s scan on the reset path — measured,
    not guessed (runs/20260912T230019Z_perf: reset p50 6.7 s before this, 1.9 s after).
    """
    st = store()
    items = getattr(st, "items", None)
    if callable(items):
        try:
            out: list[tuple[str, dict[str, Any]]] = []
            for key, value in items():
                if isinstance(value, dict):
                    out.append((str(key), value))
                if limit and len(out) >= limit:
                    break
            return out
        except Exception as exc:  # noqa: BLE001 - fall back to the slow path rather than fail
            log.warn("episode.items_failed", str(exc))
    pairs: list[tuple[str, dict[str, Any]]] = []
    for eid in episode_ids(limit):
        ep = st.get(eid)
        if isinstance(ep, dict):
            pairs.append((eid, ep))
    return pairs


def row(ep: dict[str, Any], *, probe: bool = False, now: float | None = None) -> dict[str, Any]:
    """One ops row: who it is, how old, and whether its sandbox is still burning money.

    `alive` is None when we did not look. A terminated episode answers False without a probe —
    the whole point of recording `terminated` is to avoid an RPC per row on the common case.
    """
    terminated = bool(ep.get("terminated"))
    alive: bool | None = False if terminated else None
    if probe and not terminated:
        try:
            alive = workspace_for(ep).alive()
        except Exception as exc:  # noqa: BLE001
            log.debug("episode.probe_failed", str(exc), episode_id=ep.get("episode_id"))
            alive = False
    return {
        "episode_id": ep.get("episode_id"),
        "scenario_id": ep.get("scenario_id"),
        "created_at": ep.get("created_at"),
        "age_s": round(age_s(ep, now), 1),
        "step": ep.get("step", 0),
        "sandbox_id": ep.get("sandbox_id"),
        "alive": alive,
        "terminated": terminated,
        "done": bool(ep.get("done")),
        "score": ep.get("score"),
        "finished_at": ep.get("finished_at"),
    }


def list_episodes(probe: bool = True, limit: int = EPISODE_SWEEP_MAX) -> dict[str, Any]:
    """Ops listing for `GET /episodes`: id, scenario, created_at, sandbox alive?"""
    now = datetime.now(timezone.utc).timestamp()
    rows = [row(ep, probe=probe, now=now) for _eid, ep in iter_episodes(limit)]
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    live = sum(1 for r in rows if r["alive"] is True)
    return {
        "count": len(rows),
        "live": live,
        "probed": probe,
        "ttl_s": EPISODE_TTL_S,
        "purge_s": EPISODE_PURGE_S,
        "episodes": rows,
    }


def sweep(ttl_s: int | None = None, purge_s: int | None = None,
          limit: int = EPISODE_SWEEP_MAX,
          max_actions: int = EPISODE_SWEEP_MAX_ACTIONS) -> dict[str, Any]:
    """Terminate episodes older than the TTL; drop long-dead records from the Dict.

    Deliberately cheap and idempotent: it is called on the reset path, so it must cost at most a
    Dict scan plus `max_actions` terminates/purges, and it must never raise. `max_actions <= 0`
    lifts the budget (the out-of-band `sweep` Modal function, which has its own 300 s timeout).
    """
    ttl = EPISODE_TTL_S if ttl_s is None else ttl_s
    purge = EPISODE_PURGE_S if purge_s is None else purge_s
    now = datetime.now(timezone.utc).timestamp()
    expired: list[str] = []
    purged: list[str] = []
    errors: list[str] = []
    scanned = 0
    budget_left = True

    for eid, ep in iter_episodes(limit):
        if max_actions > 0 and len(expired) + len(purged) >= max_actions:
            budget_left = False
            break
        try:
            scanned += 1
            age = age_s(ep, now)
            if not ep.get("terminated") and ttl > 0 and age > ttl:
                if delete(eid):
                    expired.append(eid)
            elif ep.get("terminated") and purge > 0 and age > purge:
                store().pop(eid, None)
                purged.append(eid)
        except Exception as exc:  # noqa: BLE001 - one bad episode must not stop the sweep
            errors.append(f"{eid}: {type(exc).__name__}: {exc}")

    if expired or purged or errors:
        log.info(
            "episode.sweep",
            f"expired {len(expired)}, purged {len(purged)}",
            scanned=scanned, ttl_s=ttl, purge_s=purge,
            expired=expired or None, purged=purged or None, errors=errors or None,
            budget_exhausted=None if budget_left else True,
        )
    return {"scanned": scanned, "ttl_s": ttl, "purge_s": purge,
            "expired": expired, "purged": purged, "errors": errors,
            "budget_exhausted": not budget_left}


#: wall-clock of this container's last sweep (monotonic). Per container on purpose: the sweep is
#: idempotent, so N containers sweeping independently is harmless, and a shared lock in the Dict
#: would cost the very round trip the throttle exists to avoid.
_last_sweep_at: float = 0.0


def reset_sweep_throttle() -> None:
    """Forget the last sweep time (tests, and the out-of-band `sweep` Modal function)."""
    global _last_sweep_at
    _last_sweep_at = 0.0


def sweep_quietly(force: bool = False) -> dict[str, Any]:
    """`sweep()` on the reset path: throttled, and unable to break its caller.

    Throttled because reset is the latency-critical call in the system (the browser is watching)
    and the sweep is pure housekeeping: paying for it on *every* reset put ~4.5 s on the p50 with a
    125-episode Dict. Once per EPISODE_SWEEP_INTERVAL_S per container is plenty — an abandoned
    sandbox is reclaimed within the interval, and the sandbox's own idle_timeout is the backstop.
    """
    global _last_sweep_at
    now = time.monotonic()
    if not force and _last_sweep_at and (now - _last_sweep_at) < EPISODE_SWEEP_INTERVAL_S:
        return {"scanned": 0, "expired": [], "purged": [], "errors": [], "skipped": "throttled"}
    _last_sweep_at = now
    try:
        return sweep()
    except Exception as exc:  # noqa: BLE001
        log.warn("episode.sweep_failed", str(exc))
        return {"scanned": 0, "expired": [], "purged": [], "errors": [str(exc)]}


def active_sandboxes(ttl_s: int | None = None, limit: int = 0) -> dict[str, dict[str, Any]]:
    """`{sandbox_id: {episode_id, scenario_id, age_s, step}}` for episodes still in use.

    "In use" == not terminated, not done, and younger than the TTL. This is the set `reap` must not
    touch: killing one of these is exactly what ended run `r_ccda8780cbee` at step 4 with a
    mislabelled EINTERNAL and a null score.

    `limit=0` (scan everything) is deliberate and is NOT the sweep's bound. modal.Dict hands out its
    keys in its own order, so a bounded scan of a store that holds more records than the bound
    silently omits arbitrary episodes — and an omitted episode is an *unprotected* one. The store
    keeps terminated records for EPISODE_PURGE_S (24 h), so it passes 200 records in normal use:
    with the old default of EPISODE_SWEEP_MAX this function would have started handing `reap` a
    partial protection list, i.e. reintroduced exactly the incident it exists to prevent. The scan
    is one `items()` round trip, and `reap` is an out-of-band function, not a hot path.
    """
    ttl = EPISODE_TTL_S if ttl_s is None else ttl_s
    now = datetime.now(timezone.utc).timestamp()
    out: dict[str, dict[str, Any]] = {}
    for _eid, ep in iter_episodes(limit):
        if ep.get("terminated") or ep.get("done"):
            continue
        age = age_s(ep, now)
        if ttl <= 0 or age <= ttl:
            sid = ep.get("sandbox_id")
            if sid:
                out[str(sid)] = {
                    "episode_id": ep.get("episode_id"),
                    "scenario_id": ep.get("scenario_id"),
                    "age_s": round(age, 1),
                    "step": ep.get("step", 0),
                }
    return out


def active_sandbox_ids(ttl_s: int | None = None, limit: int = 0) -> set[str]:
    """Sandbox ids belonging to episodes that are still in use (see `active_sandboxes`)."""
    return set(active_sandboxes(ttl_s=ttl_s, limit=limit))


def mark_terminated(sandbox_ids: set[str], reason: str = "reaped",
                    limit: int = 0) -> list[str]:
    """Record that these sandboxes are gone, so `GET /episodes` stops claiming they are alive."""
    touched: list[str] = []
    if not sandbox_ids:
        return touched
    for eid, ep in iter_episodes(limit):
        if ep.get("terminated"):
            continue
        if str(ep.get("sandbox_id")) in sandbox_ids:
            ep["terminated"] = True
            ep["done"] = True
            ep["finished_at"] = now_iso()
            ep["terminated_by"] = reason
            save(ep)
            touched.append(eid)
    if touched:
        log.info("episode.marked_terminated", f"{len(touched)} episode(s) {reason}",
                 episodes=touched, reason=reason)
    return touched

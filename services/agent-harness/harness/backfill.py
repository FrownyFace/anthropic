"""Provenance backfill for runs stored before the taxonomy existed (PLAN.md §2.11).

Pure decision logic: no I/O, no clock, no Modal, no Store. `modal_app.py::backfill_provenance`
reads the records, applies what `plan_for()` returns, and writes the result back. Keeping the
rules here is what makes them unit-testable against real, captured run records.

Two things are wrong with every run recorded before `error_class` existed:

* it ended **`ok` with `score: null`** — but "ok" is supposed to imply a score
  (`docs/error-taxonomy.md`), so the UI shows a green run with no grade and no explanation;
* it carries **no `error_class`**, so nothing downstream can say *why* it has no score, let
  alone whether the failure was simulated or real.

The specimen is run `r_ccda8780cbee`: a concurrent `reap` terminated its Modal Sandbox at step 4,
the harness mislabelled every following tool error `EINTERNAL`, the model flailed for four more
steps, and the run ended `ok`/`score: null`. It must read `interrupted` · `real/sandbox/ESANDBOX`.

Rules (a candidate is only ever a run that ended `ok` with no score and an `evaluate` error):

1. **the events show the sandbox itself died** — some `tool.result` came back `is_error` with a
   sandbox-loss message ("Task has already finished", `NotFoundError`, "sandbox unavailable", …)
   → `interrupted`, `real/sandbox/ESANDBOX`, plus an `Interruption` for the call that found it
   gone. The loop really was cut short; the steps after it could not have executed.
2. **otherwise** the loop ran to the end and only `evaluate` failed → `unevaluated`, classified
   by the same `classify.evaluation_error_class` a live run uses, so a backfilled record is
   indistinguishable from one written today.

Nothing here rewrites history: the plan carries ONE new append-only `log` event
(`ev: "provenance.backfilled"`) that makes the change visible in the stream and in the UI.
"""

from __future__ import annotations

from typing import Any

from .classify import error_class, evaluation_error_class, looks_like_sandbox_loss
from .interrupts import interruption, primary_path

#: Tool names the shared schema allows on an `Interruption` (`submit` is harness-local, not a gym tool).
TOOL_NAMES = {"run_command", "read_file", "write_file", "list_dir"}

#: `log` event marker. One per backfilled run; also the idempotency witness in the event stream.
MARKER = "provenance.backfilled"

#: Runs recovered from the pre-Store Modal Dict belong to no browser identity. They get a synthetic
#: owner rather than being folded into the anonymous user, so "imported" stays visible in the data.
LEGACY_USER_ID = "u_legacy"
LEGACY_TITLE = "imported (pre-Store)"

#: Fields copied verbatim from a legacy record after its events are replayed (the event replay
#: itself sets some of them; this makes the ones it cannot derive match the original exactly).
LEGACY_FIELDS = ("status", "finished_at", "error", "steps", "summary", "episode_id",
                 "evaluation", "usage", "task_prompt")


def score_of(record: dict[str, Any]) -> float | None:
    """The run's grade, from either the column or the embedded evaluation."""
    score = record.get("score")
    if score is None:
        score = (record.get("evaluation") or {}).get("score")
    return score


def already_backfilled(record: dict[str, Any]) -> bool:
    return any(
        (event.get("data") or {}).get("ev") == MARKER
        for event in record.get("events") or []
        if event.get("type") == "log"
    )


def is_candidate(record: dict[str, Any]) -> bool:
    """A pre-taxonomy run that claims `ok` but was never graded because `evaluate` failed.

    Deliberately narrow. A run that ended `ok` **with** a score is a real success and is left
    alone; a run that already ended `error`/`truncated`/`interrupted`/`unevaluated` already says
    what happened. Only the "green run with no grade" case is a lie that needs correcting.
    """
    if str(record.get("status") or "") != "ok":
        return False
    if score_of(record) is not None:
        return False
    if already_backfilled(record):
        return False
    return "evaluate" in str(record.get("error") or "").lower()


def _call_input(events: list[dict[str, Any]], tool_use_id: str | None) -> dict[str, Any]:
    if not tool_use_id:
        return {}
    for event in events:
        data = event.get("data") or {}
        if event.get("type") == "tool.call" and data.get("tool_use_id") == tool_use_id:
            return dict(data.get("input") or {})
    return {}


def sandbox_loss(record: dict[str, Any]) -> dict[str, Any] | None:
    """The first tool call that found the sandbox gone, or None if the loop never saw one.

    Only `is_error` results count. The run-level `error` is deliberately NOT evidence: on a
    candidate it is always the `evaluate` failure, which is exactly what distinguishes "the
    sandbox died mid-run" (rule 1) from "the loop finished and only grading failed" (rule 2).
    Requiring `is_error` also stops a *file the agent happened to read* that contains one of
    these phrases from being read as infrastructure failure.
    """
    events = list(record.get("events") or [])
    for event in events:
        if event.get("type") != "tool.result":
            continue
        data = event.get("data") or {}
        if not data.get("is_error"):
            continue
        output = str(data.get("output") or "")
        if not looks_like_sandbox_loss(output):
            continue
        tool = str(data.get("tool") or "")
        args = _call_input(events, data.get("tool_use_id"))
        return {
            "step": event.get("step"),
            "event_id": event.get("id"),
            "at": event.get("ts"),
            "tool_use_id": data.get("tool_use_id"),
            "tool": tool if tool in TOOL_NAMES else None,
            "path": primary_path(tool, args) if tool in TOOL_NAMES else None,
            "detail": output[:200],
        }
    return None


def next_seq(record: dict[str, Any]) -> int:
    """One past the highest event id — the append-only stream never reuses a sequence number."""
    ids = [int(e.get("id") or 0) for e in record.get("events") or [] if e.get("id") is not None]
    return (max(ids) + 1) if ids else 0


def marker_event(run_id: str, *, at: str, seq: int, from_status: str, to_status: str,
                 klass: dict[str, Any]) -> dict[str, Any]:
    """The ONE event the backfill appends, in the unified log shape (PLAN.md §2.5)."""
    return {
        "id": seq,
        "ts": at,
        "run_id": run_id,
        "type": "log",
        "step": None,
        "data": {
            "svc": "harness",
            "lvl": "warn",
            "ev": MARKER,
            "msg": f"status {from_status} -> {to_status} (failure provenance backfilled)",
            "from_status": from_status,
            "to_status": to_status,
            "error_class": klass,
        },
    }


def plan_for(record: dict[str, Any], *, at: str) -> dict[str, Any] | None:
    """What this run needs, or None if it is already honest. `at` is the caller's clock."""
    if not is_candidate(record):
        return None

    run_id = str(record.get("run_id") or record.get("id") or "")
    from_status = str(record.get("status") or "")
    loss = sandbox_loss(record)

    if loss is not None:
        to_status = "interrupted"
        klass = error_class(
            "real", "sandbox", "ESANDBOX", outcome_known=True, side_effect_applied=False,
            detail=(f"the Modal Sandbox was gone from step {loss['step']}; "
                    f"the run could not continue: {loss['detail']}"),
        )
        intr: dict[str, Any] | None = interruption(
            layer="sandbox", code="ESANDBOX", step=loss["step"],
            tool_use_id=loss["tool_use_id"], tool=loss["tool"], path=loss["path"],
            outcome_known=True, planned=False, resumed=False, worker_generation=1,
            at=loss["at"] or at,
            detail=("backfilled from the event log: the Modal Sandbox was terminated or became "
                    "unreachable mid-run; the remaining steps could not have executed"),
        )
    else:
        to_status = "unevaluated"
        klass = evaluation_error_class(str(record.get("error") or ""))
        intr = None

    interruptions = list(record.get("interruptions") or [])
    if intr is not None:
        interruptions.append(intr)

    fields: dict[str, Any] = {"status": to_status, "error_class": klass}
    if intr is not None:
        fields["interruptions"] = interruptions

    return {
        "run_id": run_id,
        "from_status": from_status,
        "to_status": to_status,
        "error_class": klass,
        "interruption": intr,
        "fields": fields,
        "event": marker_event(run_id, at=at, seq=next_seq(record), from_status=from_status,
                              to_status=to_status, klass=klass),
    }


# --------------------------------------------------------------------------- applying it

# `store` below is anything exposing the Store verbs as plain calls: a `SqliteStore` (tests) or a
# `ModalStoreClient` (production, which forwards each verb to the single deployed container).


def import_record(store: Any, record: dict[str, Any], *, chunk: int = 50) -> dict[str, Any]:
    """Copy one pre-Store run into the Store with its original ids, events and timestamps.

    Idempotent, and the run row is asked about FIRST: `create_run` is itself idempotent, but a
    conversation minted before it would be orphaned by the re-import — a replay used to leave a
    stray "imported" conversation behind on every pass (the Store phase hit the same trap and
    fixed it inside `create_run`; calling `create_conversation` directly walks straight back into
    it). A run already in the Store also keeps the owner it has: only genuinely absent runs are
    attributed to `u_legacy`.
    """
    run_id = str(record.get("run_id") or record.get("id") or "")
    events = list(record.get("events") or [])
    existing = store.get_run(run_id)
    if existing is None:
        conversation = store.create_conversation(LEGACY_USER_ID, record.get("scenario_id") or "",
                                                 LEGACY_TITLE)
        store.create_run({**{k: v for k, v in record.items() if k != "events"},
                          "user_id": LEGACY_USER_ID, "conversation_id": conversation["id"]})
    else:
        conversation = {"id": existing.get("conversation_id")}
    appended = 0
    for i in range(0, len(events), chunk):
        appended += int((store.append_events(run_id, events[i:i + chunk]) or {}).get("appended") or 0)
    # The event replay already reconstructed most of the row; this pins the fields it cannot
    # derive (and re-pins the rest) to exactly what the original record said.
    fields = {k: record[k] for k in LEGACY_FIELDS if record.get(k) is not None}
    if fields:
        store.finish_run(run_id, fields)
    return {"run_id": run_id, "conversation_id": conversation["id"], "events": len(events),
            "appended": appended, "user_id": LEGACY_USER_ID,
            "already_present": existing is not None}


def apply_plan(store: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Append the marker event, then correct the row — in that order, deliberately.

    If the process dies between the two, the run is still `ok` and is therefore still a candidate
    next time, and re-appending the same `(run_id, seq)` is a no-op. The reverse order would leave
    a corrected run with nothing in its stream to say so, and nothing would ever go back for it.
    """
    store.append_events(plan["run_id"], [plan["event"]])
    return store.finish_run(plan["run_id"], plan["fields"]) or {}

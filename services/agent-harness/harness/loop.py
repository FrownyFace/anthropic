"""The model loop: one episode from reset to evaluate, and back again after a real interruption.

Runs inside the Modal function `run_episode` — the only process in Faultline that ever sees
ANTHROPIC_API_KEY. It is deliberately synchronous: a Modal function has no ambient event loop and
the whole loop is a sequence of blocking round trips (model -> tool -> observe -> model).

Shape of one step
    1. client.messages.create(...)               -> text blocks + tool_use blocks
    2. emit turn.text for prose
    3. for each tool_use, in order: tool.call -> execute over MCP -> observe -> tool.result
       (+ fault.fired for every new entry in observe().faults_fired, + workspace.diff after mutations)
    4. all tool_results for the turn go back in ONE user message (a missing one is a 400)

Every `tool.result` carries provenance the agent never sees (PLAN.md §2.11, docs/error-taxonomy.md):
`outcome` (executed | failed | not_executed | unknown), `error_class` when it failed, `attempts`
(harness-level retries — only a connect failure is ever retried) and `sandbox {id, alive}`.

Stops on `submit`, on end_turn without tool use, at max_steps (`truncated`), or the moment the
Modal Sandbox is gone (`interrupted` — there is nothing left to act on, so the model is not given
four more steps to flail in). Evaluates unless the run was interrupted, and always DELETEs the
episode in a finally block.

Resume. `run_episode` is registered with `modal.Retries`, so a worker that dies (OOM, kill, or the
scenario's own `worker_crash` trigger) is re-invoked with the SAME (run_id, req). The new worker
rebuilds the conversation from the persisted events, reports the call that was in flight to the gym,
hands the agent a synthetic `EHARNESS` result whose outcome is *unknown*, and carries on.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from typing import Any, Callable

from faultline_common.log import ctx_episode_id, ctx_run_id, ctx_step, get_logger, truncate

from . import config
from .classify import (
    classify_result,
    error_class,
    evaluation_error_class,
    ledger_side_effect,
    normalize_fault,
    run_error_class,
    short_id,
)
from .events import EventSink, now_iso
from .gym_client import GymClient
from .interrupts import (
    HarnessFaultPlan,
    align_ledger,
    harness_result_data,
    has_progress,
    interruption,
    plan_resume,
    primary_path,
    replay_call_counts,
)
from .mcp_client import SyncMCPClient, ToolCallResult, tool_error_json, transport_kind_of
from .prompts import SUBMIT_TOOL, apply_cache_breakpoint, build_task_message, system_blocks

log = get_logger(config.SVC)

# GRADING.md "mutating" definition, kept verbatim so harness and grader agree on what a mutation is.
MUTATING_RE = re.compile(
    r"(>>?|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\btouch\b|\bmkdir\b|\btruncate\b|\bpatch\b"
    r"|\bgit\s+(apply|checkout|reset|restore))"
)
PY_OPEN_WRITE_RE = re.compile(r"open\s*\([^)]*['\"][wa]")

CODE_TO_KIND = {"ENOENT": "missing_file", "EACCES": "denied_write", "ETIMEDOUT": "ack_lost"}
RETRYABLE_EXC_NAMES = {"RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError"}
THINKING_CAP = 8_000  # chars of summarised thinking kept in one `turn.thinking` event
TOOL_NAMES = {"run_command", "read_file", "write_file", "list_dir"}
#: every status a finished run can carry (schemas.RunStatus minus queued/running)
TERMINAL_STATUSES = {"ok", "error", "truncated", "unevaluated", "interrupted"}
LEDGER_RESOLUTION_CAP = 50


# --------------------------------------------------------------------------- pure helpers


def is_mutating(tool: str, args: dict[str, Any] | None) -> bool:
    """Does this tool call intend to change the workspace? (GRADING.md ledger semantics)"""
    if tool == "write_file":
        return True
    if tool != "run_command":
        return False
    command = (args or {}).get("command") or ""
    if MUTATING_RE.search(command):
        return True
    if "-c" in command and "python" in command and PY_OPEN_WRITE_RE.search(command):
        return True
    return False


def call_summary(tool: str, args: dict[str, Any] | None) -> str:
    """Short human label for logs/CLI: the command, or the path."""
    a = args or {}
    if tool == "run_command":
        return truncate(str(a.get("command", "")), 200)
    if tool == "submit":
        return truncate(str(a.get("summary", "")), 200)
    return str(a.get("path", ""))


def is_retryable_model_error(exc: BaseException) -> bool:
    """Retry rate limits, connection blips and 5xx; fail fast on 4xx (bad key, bad model, bad args)."""
    name = type(exc).__name__
    if name in RETRYABLE_EXC_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status >= 500


def new_faults(observed: list[dict[str, Any]], seen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """faults_fired is append-only; take the suffix, then guard with a key set for safety."""
    if len(observed) <= len(seen):
        return []
    seen_keys = {(f.get("step"), f.get("kind"), f.get("path"), f.get("mode")) for f in seen}
    return [
        f
        for f in observed[len(seen) :]
        if (f.get("step"), f.get("kind"), f.get("path"), f.get("mode")) not in seen_keys
    ]


def pick_fault(fired: list[dict[str, Any]], error_code: str | None) -> dict[str, Any] | None:
    """The fired fault that explains THIS call (all of `fired` is still emitted as fault.fired).

    A delta can carry more than one row — the gym records a fault for every call it intercepts,
    including ones the harness does not observe immediately — so taking `fired[0]` blindly lets a
    stale row relabel an unrelated failure (an EACCES reported as a missing file). Only a row whose
    kind matches the code the agent actually got may explain an error; a result that is not an error
    (a shell short-circuited into `exit 1`) carries no code, and the newest row is the explanation.
    """
    if not fired:
        return None
    if not error_code:
        return fired[0]
    want = CODE_TO_KIND.get(error_code)
    if not want:
        return None  # no injected fault produces EINVAL/ESANDBOX/…; do not invent one
    return next((f for f in fired if f.get("kind") == want), None)


def inferred_fault(step: int, error_code: str | None, args: dict[str, Any] | None) -> dict[str, Any] | None:
    """Fallback used only when observe() is unavailable; ground truth is the ledger delta."""
    kind = CODE_TO_KIND.get(error_code or "")
    if not kind:
        return None
    return {
        "step": step,
        "kind": kind,
        "path": (args or {}).get("path"),
        "mode": "transient",
        "inferred": True,
    }


def block_attr(block: Any, name: str, default: Any = None) -> Any:
    """Content blocks are pydantic objects from the SDK and plain dicts in tests."""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def request_id_of(obj: Any) -> str | None:
    """Anthropic's `request-id` response header, which the SDK hangs off the parsed object.

    Present as `_request_id` on a Message and on APIStatusError; absent on connection errors and on
    any test double, so this is best-effort and never raises.
    """
    for attr in ("_request_id", "request_id"):
        value = getattr(obj, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def usage_from(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    return {
        "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
        # Both cache counters: `read` proves the moving breakpoint (prompts.apply_cache_breakpoint)
        # is hitting, `creation` proves it is writing. schemas.Usage declares both.
        "cache_read_input_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
        "cache_creation_input_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
    }


def hard_exit(code: int = 137) -> None:  # pragma: no cover - the process is gone after this
    """Die the way a kill/OOM does: no finally blocks, no teardown, nothing flushed.

    The `worker_crash` harness fault must be a REAL worker death, otherwise the resume path it is
    meant to exercise would never run. Injected as `crash=` so tests can observe the call instead
    of losing the interpreter.
    """
    os._exit(code)


# --------------------------------------------------------------------------- model call


def anthropic_default_headers(env: dict[str, str] | None = None) -> dict[str, str]:
    """Headers every model call carries.

    The key in `anthropic-secret` is org-scoped, not workspace-scoped, so the API requires an
    `anthropic-workspace-id` header naming the workspace to bill/attribute to (a 400 otherwise).
    The secret ships that id as ANTHROPIC_WORKSPACE; it is never logged unmasked.
    """
    env = os.environ if env is None else env
    ws = (env.get("ANTHROPIC_WORKSPACE") or "").strip()
    return {"anthropic-workspace-id": ws} if ws else {}


def default_anthropic_client() -> Any:
    import anthropic

    return anthropic.Anthropic(default_headers=anthropic_default_headers())


def create_message(
    client: Any,
    *,
    model: str,
    system: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    sink: EventSink,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = config.MODEL_RETRIES,
) -> Any:
    """messages.create with a typed retry chain (rate limit / 5xx / connection)."""
    # Two cache breakpoints: the static one on the system block, and a moving one on the last block
    # of the newest user turn so the GROWING conversation caches too (see prompts.py for why the
    # system breakpoint alone never writes on Haiku 4.5).
    apply_cache_breakpoint(messages)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": config.MAX_TOKENS,
        "system": system,
        "tools": tools,
        "messages": messages,
    }
    thinking = config.thinking_for(model)
    if thinking is not None:
        kwargs["thinking"] = thinking
    delay = 1.0
    last: BaseException | None = None
    for attempt in range(1, retries + 2):
        t0 = time.perf_counter()
        try:
            resp = client.messages.create(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - classified immediately below
            last = exc
            # One `llm.call` per attempt, failures included, so the browser can show a retry
            # (ARCHITECTURE.md §5.1 / schemas.Event). Usage is unknown for a failed call.
            sink.emit(
                "llm.call",
                {
                    "attempt": attempt,
                    "model": model,
                    "stop_reason": None,
                    "request_id": request_id_of(exc),
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "duration_ms": int((time.perf_counter() - t0) * 1000),
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                },
            )
            if not is_retryable_model_error(exc) or attempt == retries + 1:
                raise
            sink.warn(
                "model.retry",
                f"{type(exc).__name__}: {exc}",
                attempt=attempt,
                model=model,
                backoff_s=delay,
            )
            sleep(delay)
            delay *= 2
            continue
        dur = int((time.perf_counter() - t0) * 1000)
        u = usage_from(resp)
        sink.info(
            "model.call",
            "assistant turn",
            model=model,
            stop_reason=getattr(resp, "stop_reason", None),
            dur_ms=dur,
            input_tokens=u.get("input_tokens"),
            output_tokens=u.get("output_tokens"),
            cache_read_input_tokens=u.get("cache_read_input_tokens") or None,
            attempt=attempt if attempt > 1 else None,
        )
        sink.emit(
            "llm.call",
            {
                "attempt": attempt,
                "model": model,
                "stop_reason": getattr(resp, "stop_reason", None),
                "request_id": request_id_of(resp),
                "usage": u,
                "duration_ms": dur,
            },
        )
        return resp
    raise last  # pragma: no cover - loop always returns or raises


# --------------------------------------------------------------------------- the loop


def run_episode_sync(
    run_id: str,
    req: dict[str, Any],
    *,
    store: Any = None,
    gym: Any = None,
    mcp_factory: Callable[[str], Any] | None = None,
    anthropic_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    crash: Callable[[int], None] = hard_exit,
) -> dict[str, Any]:
    """Run (or resume) one episode end to end. Returns the final RunRecord dict."""
    from .store import RunStore

    store = store or RunStore()
    req = dict(req or {})
    scenario_id = req.get("scenario_id") or ""
    model = req.get("model") or config.model_default()
    seed = req.get("seed")

    record = store.get(run_id)
    resume = None
    if record is not None:
        if record.get("status") in TERMINAL_STATUSES:
            # A Modal retry of a run that already finished (or a replayed spawn): do not re-run it.
            log.info("run.already_finished", str(record.get("status")), run_id=run_id,
                     steps=record.get("steps"))
            return record
        if has_progress(record):
            resume = plan_resume(record, build_task_message=build_task_message)
    if record is None:
        # No row yet (a `modal run` smoke, or a Store that lost the queued record): create one so
        # append_events has a parent. create_run provisions the conversation/user if needed.
        record = store.create(
            {
                "run_id": run_id,
                "status": "queued",
                "scenario_id": scenario_id,
                "model": model,
                "seed": seed,
                "max_steps": req.get("max_steps") or config.DEFAULT_MAX_STEPS,
                "episode_id": None,
                "task_prompt": req.get("task_prompt"),
                "created_at": now_iso(),
                "finished_at": None,
                "events": [],
                "evaluation": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "error": None,
                "conversation_id": req.get("conversation_id"),
                "user_id": req.get("user_id"),
            }
        )
    record["model"] = model
    record["scenario_id"] = scenario_id
    sink = EventSink(store, record, flush_interval=config.EVENT_FLUSH_S)

    ctx_run_id.set(run_id)
    ctx_episode_id.set(None)  # containers are reused across runs; never inherit the last episode
    ctx_step.set(None)

    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    status = "running"
    error: str | None = None
    run_class: dict[str, Any] | None = None
    episode_id: str | None = None
    sandbox_id: str | None = None
    sandbox_alive = True
    seen_faults: list[dict[str, Any]] = []
    interruptions: list[dict[str, Any]] = list(record.get("interruptions") or [])
    tool_calls: list[dict[str, Any]] = []
    mcp: Any = None
    owns_gym = gym is None
    gym = gym or GymClient(run_id=run_id)
    t_start = time.time()
    max_steps = int(req.get("max_steps") or record.get("max_steps") or config.DEFAULT_MAX_STEPS)
    step = 0
    worker_generation = int(record.get("worker_generation") or 1) + (1 if resume else 0)
    harness_plan = HarnessFaultPlan()

    def observe() -> dict[str, Any] | None:
        if not episode_id:
            return None
        try:
            return gym.observe(episode_id)
        except Exception as exc:  # noqa: BLE001 - observation is best-effort telemetry
            sink.warn("observe.failed", f"{type(exc).__name__}: {exc}", episode_id=episode_id)
            return None

    def sandbox_state() -> dict[str, Any]:
        return {"id": sandbox_id, "alive": sandbox_alive}

    def note_interruption(intr: dict[str, Any], *, step: int | None = None) -> dict[str, Any]:
        """Record one real interruption: in memory, in the stream, and in the run row NOW.

        Persisting immediately rather than only in the final update is not tidiness: the SSE
        stream closes the instant `run.finished` flips the status, so a client that fetches
        GET /runs/{id} on the `done` frame races the update that follows. Measured on the first
        live worker-crash run (r_065377940e9d): the transcript showed the interruption while the
        record still answered `interruptions: []` for about a second.
        """
        interruptions.append(intr)
        sink.emit("interruption", intr, step=step if step is not None else intr.get("step"))
        sink.update(interruptions=interruptions, worker_generation=worker_generation)
        return intr

    #: (interruption, tool, path) for calls whose answer we lost while the request was still being
    #: served. Reported to the gym at the END of the loop — see `flush_interruption_reports`.
    deferred_reports: list[tuple[dict[str, Any], str, str | None]] = []

    def report_interruption(record_: dict[str, Any], tool: str | None, path: str | None,
                            *, mutating: bool) -> bool:
        """Tell the gym the harness never saw this call's response (GRADING.md / schemas).

        Only for a call that could have CHANGED something. The gym marks the most recent matching
        `ok`/`ack_lost` row `interrupted`, and `verified_before_rewrite` then treats that row like
        an `ack_lost` — so reporting a lost *read* would invent a verification obligation out of a
        call that had no side effect to verify.
        """
        if not episode_id or tool not in TOOL_NAMES or not mutating:
            return False
        try:
            gym.report_interruption(
                episode_id,
                {"tool": tool, "path": path, "layer": record_.get("layer"),
                 "code": record_.get("code"), "at": record_.get("at") or now_iso()},
            )
            return True
        except Exception as exc:  # noqa: BLE001 - telemetry on the way to grading; never fatal
            sink.warn("gym.interruption_report_failed", f"{type(exc).__name__}: {exc}",
                      episode_id=episode_id, tool=tool, path=path)
            return False

    def flush_interruption_reports() -> None:
        """Tell the gym about the calls we lost — once the loop is over, never mid-flight.

        The request we stopped listening to is usually STILL being served (an `ack_lost` holds it
        for `delay_ms`), and sandbox-env keeps its episode in a modal.Dict that every writer
        read-modify-writes. Posting an interruption while that call is in flight is a second writer
        on the same record: one of the two ledger rows loses. Observed live on run r_80d6deed5a0b —
        the annotation row the gym appended was overwritten by the in-flight write's own row. By the
        end of the loop the call has certainly landed, and `evaluate` (which reads the ledger and
        grades `verified_before_rewrite` off it) has not run yet.
        """
        for intr, tool_, path_ in deferred_reports:
            report_interruption(intr, tool_, path_, mutating=True)
        deferred_reports.clear()

    def dispatch(name: str, args: dict[str, Any], *, abort_after_ms: int | None = None) -> ToolCallResult:
        """One MCP call, with the ONLY harness-level retry we allow: a failed connect.

        A connect failure provably never left this process, so re-sending it cannot duplicate a
        side effect. Every other failure (timeout, aborted stream, protocol error) has an unknown
        outcome and is handed to the agent exactly as it is — retrying those is the duplicate-write
        bug `verified_before_rewrite` is designed to catch.
        """
        attempts = 0
        while True:
            attempts += 1
            t0 = time.perf_counter()
            try:
                if abort_after_ms is None:
                    res = mcp.call(name, args, timeout=config.TOOL_TIMEOUT_S)
                else:
                    res = mcp.call(name, args, timeout=config.TOOL_TIMEOUT_S,
                                   abort_after_ms=abort_after_ms)
            except Exception as exc:  # noqa: BLE001 - a tool failure is never fatal
                kind = transport_kind_of(exc)
                res = ToolCallResult(
                    text=tool_error_json(f"{name}: {type(exc).__name__}: {exc}", "ETRANSPORT"),
                    is_error=True,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    error_code="ETRANSPORT",
                    transport_error=True,
                    transport_kind=kind,
                )
                sink.error("tool.exception", f"{type(exc).__name__}: {exc}", tool=name,
                           transport_kind=kind)
            res.attempts = attempts
            if res.transport_kind == "connect" and attempts < config.TOOL_MAX_ATTEMPTS:
                sink.warn("tool.retry", "connect failed; the request never left this worker",
                          tool=name, attempt=attempts, max_attempts=config.TOOL_MAX_ATTEMPTS)
                continue
            return res

    def dispatch_and_crash(name: str, args: dict[str, Any], trigger: dict[str, Any]) -> ToolCallResult:
        """`worker_crash`: dispatch on a worker thread, wait, then kill the process outright.

        The request is already at sandbox-env when we die, so the side effect lands and the ledger
        knows it — which is exactly the situation the agent must not resolve by guessing.
        """
        after_ms = int(trigger.get("after_ms") or 400)
        box: dict[str, Any] = {}

        def _work() -> None:
            try:
                box["res"] = mcp.call(name, args, timeout=config.TOOL_TIMEOUT_S)
            except BaseException as exc:  # noqa: BLE001
                box["exc"] = exc

        thread = threading.Thread(target=_work, name="faultline-tool-call", daemon=True)
        t0 = time.perf_counter()
        thread.start()
        time.sleep(after_ms / 1000.0)
        log.error(
            "harness.crash.planned",
            "killing this worker with the request in flight (scenario harness_fault)",
            run_id=run_id, episode_id=episode_id, step=step, tool=name,
            path=primary_path(name, args), after_ms=after_ms,
            worker_generation=worker_generation, exit_code=137, source=harness_plan.source,
        )
        crash(137)
        # Only reachable when `crash` is injected (tests): finish the call so the test can assert.
        thread.join(config.TOOL_TIMEOUT_S)
        if "exc" in box:
            raise box["exc"]
        res = box.get("res")
        if res is None:  # pragma: no cover - defensive
            res = ToolCallResult(
                text=tool_error_json(f"{name}: crash hook returned before the call did", "ETRANSPORT"),
                is_error=True, duration_ms=int((time.perf_counter() - t0) * 1000),
                error_code="ETRANSPORT", transport_error=True, transport_kind="protocol",
            )
        return res

    def emit_ledger_resolution(evaluation: dict[str, Any] | None) -> None:
        """Append-only echo of what the ledger knows about side effects (PLAN.md §2.11).

        Earlier events are never rewritten: this is one extra `log` event, `ev: ledger.resolution`,
        listing {tool_use_id, step, side_effect_applied} so the UI can upgrade
        `tool.result.data.error_class.side_effect_applied` from "unknown" to the truth.
        """
        ledger = list((evaluation or {}).get("ledger") or [])
        if not ledger or not tool_calls:
            return
        aligned = align_ledger(ledger, tool_calls)
        rows: list[dict[str, Any]] = []
        for row, target in zip(ledger, aligned):
            interrupted = bool(row.get("interrupted"))
            if str(row.get("outcome") or "") == "ok" and not interrupted:
                continue  # the events already say this one executed
            rows.append({
                "tool_use_id": target.get("tool_use_id"),
                "step": target.get("step") if target.get("step") is not None else row.get("step"),
                "ledger_step": row.get("step"),
                "tool": row.get("tool"),
                "path": row.get("path"),
                "outcome": row.get("outcome"),
                "origin": row.get("origin"),
                "error_code": row.get("error_code"),
                "interrupted": interrupted,
                "side_effect_applied": ledger_side_effect(row),
            })
        if not rows:
            return
        sink.info(
            "ledger.resolution",
            f"the ledger resolved the side effect of {len(rows)} call(s)",
            resolutions=rows[:LEDGER_RESOLUTION_CAP],
            rows=len(rows),
            matched=sum(1 for r in rows if r.get("tool_use_id")),
        )

    try:
        sink.set_status("running")

        if resume is None:
            sink.emit(
                "run.started",
                {
                    "scenario_id": scenario_id,
                    "model": model,
                    "seed": seed,
                    "max_steps": max_steps,
                    "anthropic_workspace": config.masked_workspace(),
                    "sandbox_env_url": config.sandbox_env_url(),
                    "worker_generation": worker_generation,
                },
            )

            # --------------------------------------------------------- reset
            reset = gym.reset(scenario_id, seed)
            episode_id = reset.get("episode_id")
            ctx_episode_id.set(episode_id)
            sandbox_id = short_id(reset.get("sandbox_id"))
            scenario = reset.get("scenario") or {}
            task_prompt = scenario.get("task_prompt") or req.get("task_prompt") or ""
            if not req.get("max_steps") and isinstance(scenario.get("max_steps"), int):
                max_steps = min(int(scenario["max_steps"]), config.HARD_MAX_STEPS)
            # PLAN.md §2.9: the run row must be self-describing before the first event of the episode.
            sink.update(episode_id=episode_id, max_steps=max_steps, task_prompt=task_prompt)
            sink.emit(
                "episode.reset",
                {
                    "episode_id": episode_id,
                    "files": reset.get("files") or [],
                    "task_prompt": task_prompt,
                    "scenario": scenario,
                    "workspace_root": reset.get("workspace_root", "/workspace"),
                    "sandbox_id": sandbox_id,
                    "attempt": 1,
                },
            )
            messages: list[Any] = [
                {
                    "role": "user",
                    "content": build_task_message(
                        task_prompt, reset.get("files"), reset.get("workspace_root", "/workspace")
                    ),
                }
            ]
        else:
            # --------------------------------------------------------- resume
            episode_id = resume.episode_id
            ctx_episode_id.set(episode_id)
            sandbox_id = short_id(resume.sandbox_id)
            scenario = resume.scenario
            task_prompt = resume.task_prompt
            max_steps = int(record.get("max_steps") or max_steps)
            messages = list(resume.messages)
            step = resume.step
            seen_faults = list(resume.seen_faults)
            usage.update({k: v for k, v in resume.usage.items() if k in usage})
            tool_calls = [
                {"tool_use_id": (e.get("data") or {}).get("tool_use_id"), "step": e.get("step"),
                 "tool": (e.get("data") or {}).get("tool"),
                 "input": (e.get("data") or {}).get("input") or {},
                 "path": primary_path(str((e.get("data") or {}).get("tool") or ""),
                                      (e.get("data") or {}).get("input") or {})}
                for e in (record.get("events") or [])
                if e.get("type") == "tool.call" and (e.get("data") or {}).get("tool") in TOOL_NAMES
            ]
            sink.update(worker_generation=worker_generation, episode_id=episode_id)
            sink.emit(
                "run.resumed",
                {
                    "worker_generation": worker_generation,
                    "resumed_from_event_id": resume.resumed_from_event_id,
                    "resumed_at": now_iso(),
                    "step": step,
                    **({"dangling_tool_use_id": resume.dangling[0]["tool_use_id"]}
                       if resume.dangling else {}),
                    **({"dropped_thinking_blocks": resume.dropped_thinking}
                       if resume.dropped_thinking else {}),
                },
            )
            log.warn("run.resumed", "a new worker picked this run up", run_id=run_id,
                     worker_generation=worker_generation, episode_id=episode_id, step=step,
                     dangling=len(resume.dangling))

        # ------------------------------------------------------------- harness fault plan
        # Precedence: what the caller asked for (ops/chaos testing) > what the scenario declares
        # (the contract) > a local mirror of the scenario file, used only while a gym that does not
        # publish `harness_faults` is deployed.
        raw_faults = [dict(f) for f in (req.get("harness_faults") or []) if isinstance(f, dict)]
        source = "request"
        if not raw_faults:
            raw_faults = list((scenario or {}).get("harness_faults") or [])
            source = "scenario"
        if not raw_faults:
            raw_faults = config.harness_faults_for(scenario_id)
            source = "fallback"
        harness_plan = HarnessFaultPlan.build(
            raw_faults, fired=record.get("harness_faults_fired"), source=source
        )
        if harness_plan:
            sink.info("harness_faults.armed", "scenario declares real interruptions",
                      faults=harness_plan.faults, source=source,
                      already_fired=sorted(harness_plan.fired) or None)
        if resume is not None:
            replay_call_counts(harness_plan, record)

        # ------------------------------------------------------------- resume: is the episode still there?
        if resume is not None:
            probe = observe() if episode_id else None
            if probe is None:
                detail = "the episode was gone when the worker resumed; nothing left to act on"
                run_class = error_class("real", "sandbox", "ESANDBOX", outcome_known=True,
                                        side_effect_applied=False, detail=detail)
                sandbox_alive = False
                sink.emit("episode.sandbox", {"sandbox_id": sandbox_id, "status": "terminated",
                                              "reason": detail, "step": step})
                note_interruption(
                    interruption(layer="sandbox", code="ESANDBOX", step=step,
                                 outcome_known=True, planned=False, resumed=False,
                                 worker_generation=worker_generation, at=now_iso(), detail=detail),
                    step=step,
                )
                status = "interrupted"
                sink.update(error_class=run_class)   # before the status flip races the SSE close
            else:
                sink.emit("episode.sandbox", {"sandbox_id": sandbox_id, "status": "alive",
                                              "reason": f"resumed by worker {worker_generation}",
                                              "step": step})
                pending: list[dict[str, Any]] = []
                for dangling in resume.dangling:
                    tool = str(dangling.get("tool") or "")
                    planned = any(
                        f.get("kind") == "worker_crash"
                        and harness_plan.matches(f, tool, dangling.get("input") or {})
                        for f in harness_plan.faults
                    )
                    intr = interruption(
                        layer="harness", code="EHARNESS", step=dangling.get("step"),
                        tool_use_id=dangling.get("tool_use_id"),
                        tool=tool if tool in TOOL_NAMES else None,
                        path=dangling.get("path"), outcome_known=False, planned=planned,
                        resumed=True, worker_generation=worker_generation, at=now_iso(),
                        detail="the previous worker died with this call in flight; "
                               "sandbox-env may or may not have applied it",
                    )
                    note_interruption(intr, step=dangling.get("step"))
                    reported = report_interruption(
                        intr, tool, dangling.get("path"),
                        mutating=is_mutating(tool, dangling.get("input") or {}),
                    )
                    data = harness_result_data(
                        str(dangling.get("tool_use_id") or ""), tool, dangling.get("path"),
                        sandbox=sandbox_state(),
                    )
                    data["reported_to_gym"] = reported
                    sink.emit("tool.result", data, step=dangling.get("step"))
                    pending.append({
                        "type": "tool_result",
                        "tool_use_id": dangling.get("tool_use_id"),
                        "content": data["output"],
                        "is_error": True,
                    })
                if pending:
                    # EVERY tool_result for one assistant turn must travel in ONE user message.
                    # If the dying worker had already recorded results for other calls in that same
                    # turn, plan_resume put them in a trailing user message; appending a second one
                    # makes the API reject the whole conversation ("tool_use ids were found without
                    # tool_result blocks immediately after"), which would fail the resume this
                    # scenario exists to exercise.
                    dangling_ids = {str(d.get("tool_use_id") or "") for d in resume.dangling}
                    tail = messages[-1] if messages else None
                    prev = messages[-2] if len(messages) > 1 else None
                    same_turn = (
                        isinstance(tail, dict) and tail.get("role") == "user"
                        and isinstance(tail.get("content"), list)
                        and all(isinstance(b, dict) and b.get("type") == "tool_result"
                                for b in tail["content"])
                        and isinstance(prev, dict) and prev.get("role") == "assistant"
                        and any(isinstance(b, dict) and b.get("type") == "tool_use"
                                and str(b.get("id")) in dangling_ids
                                for b in (prev.get("content") or []))
                    )
                    if same_turn:
                        tail["content"].extend(pending)
                    else:
                        messages.append({"role": "user", "content": pending})

        # ------------------------------------------------------------- tools
        if status != "interrupted":
            factory = mcp_factory or (lambda ep: SyncMCPClient(config.sandbox_env_url(), ep).open())
            mcp = factory(episode_id)
            tools = list(mcp.list_tools()) + [SUBMIT_TOOL]
            sink.info("tools.ready", "tool list built", tools=[t["name"] for t in tools])

            client = anthropic_client or default_anthropic_client()
            system = system_blocks()

        submitted = False
        while status != "interrupted":
            if step >= max_steps:
                status = "truncated"
                sink.warn("loop.truncated", "step budget exhausted", max_steps=max_steps)
                break
            step += 1
            sink.step = step
            ctx_step.set(step)

            resp = create_message(
                client, model=model, system=system, tools=tools, messages=messages, sink=sink, sleep=sleep
            )
            u = usage_from(resp)
            for k in usage:
                usage[k] += u.get(k, 0)
            record["usage"] = {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]}
            if usage["cache_read_input_tokens"]:
                record["usage"]["cache_read_input_tokens"] = usage["cache_read_input_tokens"]

            content = list(getattr(resp, "content", None) or [])
            tool_uses = []
            for block in content:
                btype = block_attr(block, "type")
                if btype == "text":
                    text = block_attr(block, "text", "") or ""
                    if text.strip():
                        sink.emit("turn.text", {"text": text}, step=step)
                elif btype == "tool_use":
                    tool_uses.append(block)
                elif btype in ("thinking", "redacted_thinking"):
                    thought = str(block_attr(block, "thinking", "") or "")
                    sink.debug("turn.thinking", "thinking block", chars=len(thought))
                    # The summarised thinking is part of the transcript the browser renders
                    # (schemas.Event `turn.thinking`). Redacted blocks carry no readable text, so
                    # they stay a log line only. Capped: a run record lives in one Dict value.
                    if thought.strip():
                        sink.emit("turn.thinking", {"text": truncate(thought, THINKING_CAP)}, step=step)

            messages.append({"role": "assistant", "content": content})

            if not tool_uses:
                status = "ok"
                sink.info(
                    "loop.end_turn",
                    "assistant finished without a tool call",
                    stop_reason=getattr(resp, "stop_reason", None),
                )
                break

            tool_results: list[dict[str, Any]] = []
            for block in tool_uses:
                name = block_attr(block, "name") or ""
                args = block_attr(block, "input") or {}
                tuid = block_attr(block, "id") or f"tu_{uuid.uuid4().hex[:8]}"
                mutating = is_mutating(name, args)
                sink.emit(
                    "tool.call",
                    {"tool": name, "input": args, "tool_use_id": tuid, "mutating": mutating},
                    step=step,
                )

                res: ToolCallResult | None = None
                if name == "submit":
                    summary = str(args.get("summary", "")).strip()
                    text, is_error, dur_ms, code = "submitted", False, 0, None
                    outcome, ec, attempts_n = "executed", None, 1
                    record["summary"] = truncate(summary, 4000)
                    submitted = True
                    fault = None
                    fired: list[dict[str, Any]] = []
                    obs = None
                else:
                    tool_calls.append({"tool_use_id": tuid, "step": step, "tool": name,
                                       "input": args, "path": primary_path(name, args)})
                    trigger = harness_plan.take(name, args) if harness_plan else None
                    if trigger is not None:
                        # Persist the dangling tool.call AND the "this trigger has fired" marker
                        # BEFORE inflicting the failure: the worker that takes over must find both,
                        # or it would resume and crash again on the retry of the very same call.
                        sink.flush(force=True)
                        sink.update(harness_faults_fired=sorted(harness_plan.fired),
                                    worker_generation=worker_generation)
                        sink.warn("harness_fault.firing", f"{trigger.get('kind')} on this call",
                                  tool=name, path=primary_path(name, args),
                                  after_ms=trigger.get("after_ms"), source=harness_plan.source)
                    if trigger is not None and trigger.get("kind") == "worker_crash":
                        res = dispatch_and_crash(name, args, trigger)
                    elif trigger is not None and trigger.get("kind") == "transport_abort":
                        res = dispatch(name, args,
                                       abort_after_ms=int(trigger.get("after_ms") or 400))
                    else:
                        res = dispatch(name, args)
                    text, is_error, dur_ms, code = res.text, res.is_error, res.duration_ms, res.error_code
                    attempts_n = res.attempts

                    # Observe after anything that can carry a fault: a mutation, a tool error, and
                    # a non-zero exit — because an injected fault short-circuits `run_command` into
                    # a NORMAL result with exit 1 (FAULTS.md). Skipping that third case left the
                    # gym's ledger entry unseen until some later observe, which then attached the
                    # fault to an innocent call.
                    exit_code = (res.structured or {}).get("exit_code")
                    failed_shell = isinstance(exit_code, int) and exit_code != 0
                    obs = observe() if (mutating or is_error or failed_shell) else None
                    raw_fired = new_faults(list((obs or {}).get("faults_fired") or []), seen_faults) if obs else []
                    fired = [f for f in (normalize_fault(f, step=step) for f in raw_fired) if f]
                    fault = pick_fault(fired, code) or (
                        normalize_fault(inferred_fault(step, code, args), step=step)
                        if (is_error and obs is None) else None
                    )
                    outcome, ec = classify_result(
                        is_error=is_error, code=code, output=text, fault=fault,
                        transport_kind=res.transport_kind, payload=res.structured,
                        observed=obs is not None,
                    )
                    if ec is not None and ec.get("code") == "ESANDBOX":
                        sandbox_alive = False

                sink.emit(
                    "tool.result",
                    {
                        "tool_use_id": tuid,
                        "tool": name,
                        "output": truncate(text, 8000),
                        "is_error": is_error,
                        "duration_ms": dur_ms,
                        "error_code": code,
                        "summary": call_summary(name, args),
                        "outcome": outcome,
                        "attempts": attempts_n,
                        "sandbox": sandbox_state(),
                        **({"error_class": ec} if ec else {}),
                        **({"fault": fault} if fault else {}),
                    },
                    step=step,
                )
                for f in fired:
                    seen_faults.append(f)
                    sink.emit("fault.fired", f, step=step)
                if mutating and obs:
                    sink.emit(
                        "workspace.diff",
                        {"files": obs.get("files") or [], "diffs": obs.get("diffs") or []},
                        step=step,
                    )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tuid,
                        "content": text or "(no output)",
                        "is_error": bool(is_error),
                    }
                )

                if res is not None and outcome == "unknown" and res.transport_kind in (
                        "timeout", "abort", "protocol"):
                    # OUR side lost the answer to a call that had already been sent. That is a real
                    # interruption, not just an error: the gym must mark the ledger row
                    # `interrupted` so `verified_before_rewrite` applies to it exactly as it does to
                    # `ack_lost` (FAULTS.md "harness faults", GRADING.md "Matching an interruption
                    # report"), and RunRecord.interruptions must list it.
                    # A failed *connect* is deliberately excluded: that request provably never left
                    # this worker, so reporting it would mark some OTHER, already-acknowledged row.
                    path_ = primary_path(name, args)
                    intr = interruption(
                        layer="transport", code=(ec or {}).get("code") or "ETRANSPORT", step=step,
                        tool_use_id=tuid, tool=name if name in TOOL_NAMES else None, path=path_,
                        outcome_known=False,
                        planned=bool(trigger and trigger.get("kind") == "transport_abort"),
                        resumed=False, worker_generation=worker_generation, at=now_iso(),
                        detail="the harness never received this call's response; sandbox-env may "
                               "have applied it",
                    )
                    note_interruption(intr, step=step)
                    if mutating:
                        deferred_reports.append((intr, name, path_))

                if res is not None and res.transport_kind == "abort" and hasattr(mcp, "reconnect"):
                    # A cancelled streamable-HTTP request can poison the session; rebuild it rather
                    # than turning every later call into a phantom transport failure.
                    try:
                        mcp.reconnect()
                        sink.info("mcp.reconnected", "session rebuilt after a client abort")
                    except Exception as exc:  # noqa: BLE001
                        sink.warn("mcp.reconnect_failed", f"{type(exc).__name__}: {exc}")

                if not sandbox_alive:
                    detail = (ec or {}).get("detail") or "the sandbox is gone"
                    sink.emit("episode.sandbox",
                              {"sandbox_id": sandbox_id, "status": "terminated",
                               "reason": detail, "step": step}, step=step)
                    note_interruption(
                        interruption(
                            layer="sandbox", code="ESANDBOX", step=step, tool_use_id=tuid,
                            tool=name if name in TOOL_NAMES else None,
                            path=primary_path(name, args), outcome_known=True, planned=False,
                            resumed=False, worker_generation=worker_generation, at=now_iso(),
                            detail="the Modal Sandbox was terminated or became unreachable; "
                                   "the remaining steps could not have executed",
                        ),
                        step=step,
                    )
                    run_class = ec
                    status = "interrupted"
                    sink.update(error_class=run_class)  # see note_interruption: beat the SSE close
                    sink.error("loop.interrupted", "sandbox lost; stopping the loop instead of "
                                                   "letting the model retry into a dead sandbox",
                               step=step, code="ESANDBOX")
                    break

            if status == "interrupted":
                break

            # ALL results for one assistant turn go back in ONE user message.
            messages.append({"role": "user", "content": tool_results})
            sink.flush(force=True)  # one Store round trip per step, not per event

            if submitted:
                status = "ok"
                sink.info("loop.submitted", "agent called submit", step=step)
                break

    except Exception as exc:  # noqa: BLE001 - any failure fails the run cleanly, with an event
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        run_class = run_error_class(exc, phase="reset" if episode_id is None else "loop")
        log.error("run.exception", error, run_id=run_id, episode_id=episode_id)
        try:
            sink.error("run.exception", error, error_class=run_class)
        except Exception:  # noqa: BLE001 # pragma: no cover
            pass
    finally:
        sink.step = None
        ctx_step.set(None)
        # ------------------------------------------------------------- evaluate
        # Ledger annotations first: the grader reads the ledger, so an interruption reported after
        # `evaluate` would never be scored (GRADING.md `verified_before_rewrite`).
        flush_interruption_reports()
        evaluation_status = "skipped"
        evaluation_error: str | None = None
        if status == "interrupted":
            sink.warn("evaluate.skipped", "the run was interrupted; grading a half-run would be a lie",
                      episode_id=episode_id)
        elif episode_id:
            try:
                evaluation = gym.evaluate(episode_id)
                record["evaluation"] = evaluation
                sink.emit("episode.evaluated", evaluation)
                if isinstance(evaluation, dict) and evaluation.get("score") is not None:
                    evaluation_status = "ok"
                else:
                    evaluation_status = "failed"
                    evaluation_error = "evaluate returned no score"
                    sink.error("evaluate.no_score", evaluation_error, episode_id=episode_id)
                emit_ledger_resolution(evaluation if isinstance(evaluation, dict) else None)
            except Exception as exc:  # noqa: BLE001 - grading failure must not lose the transcript
                evaluation_status = "failed"
                evaluation_error = f"{type(exc).__name__}: {exc}"
                msg = f"evaluate failed: {evaluation_error}"
                sink.error("evaluate.failed", msg, episode_id=episode_id)
                error = error or msg
        # ------------------------------------------------------------- teardown (always)
        if episode_id:
            try:
                gym.delete(episode_id)
                sink.info("episode.deleted", "sandbox terminated", episode_id=episode_id)
            except Exception as exc:  # noqa: BLE001
                sink.warn("episode.delete_failed", f"{type(exc).__name__}: {exc}", episode_id=episode_id)
        if mcp is not None:
            try:
                mcp.close()
            except Exception as exc:  # noqa: BLE001 # pragma: no cover
                sink.warn("mcp.close_failed", f"{type(exc).__name__}: {exc}")
        if owns_gym:
            try:
                gym.close()
            except Exception:  # noqa: BLE001 # pragma: no cover
                pass

        # ------------------------------------------------------------- status
        # docs/error-taxonomy.md: `ok` implies a score. A loop that finished but could not be graded
        # is `unevaluated` (not the agent's fault); a loop a real interruption ended is `interrupted`.
        if status == "interrupted":
            final_status = "interrupted"
            run_class = run_class or error_class(
                "real", "sandbox", "ESANDBOX", outcome_known=True, side_effect_applied=False,
                detail="the run was ended by a real interruption",
            )
        elif status == "error":
            final_status = "error"
            run_class = run_class or error_class(
                "real", "harness", "EHARNESS", detail=error or "the harness could not run the episode",
            )
        elif evaluation_status != "ok":
            final_status = "unevaluated"
            run_class = evaluation_error_class(evaluation_error or error or "evaluate did not run")
        else:
            final_status = status if status in ("ok", "truncated") else "ok"
            run_class = None

        duration_ms = int((time.time() - t_start) * 1000)
        finished_at = now_iso()
        sink.emit(
            "run.finished",
            {
                "status": final_status,
                "usage": {
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    **({"cache_read_input_tokens": usage["cache_read_input_tokens"]} if usage["cache_read_input_tokens"] else {}),
                    **({"cache_creation_input_tokens": usage["cache_creation_input_tokens"]} if usage["cache_creation_input_tokens"] else {}),
                },
                "duration_ms": duration_ms,
                "steps": step,
                "score": (record.get("evaluation") or {}).get("score"),
                "evaluation_status": evaluation_status,
                "worker_generation": worker_generation,
                "interruptions": len(interruptions),
                **({"evaluation_error": evaluation_error} if evaluation_error else {}),
                **({"error": error} if error else {}),
                # The live SSE viewer must be able to explain the ending without re-fetching /runs.
                **({"error_class": run_class}
                   if (run_class and final_status in ("unevaluated", "interrupted", "error")) else {}),
            },
        )
        # PLAN.md §2.9: re-send the COMPLETE in-memory event list — `append_events` is idempotent on
        # (run_id, seq), so a Store restart that dropped the tail of this run self-heals here — then
        # write the final scalars in one update.
        sink.resend_all()
        sink.update(
            status=final_status,
            finished_at=finished_at,
            error=error,
            steps=step,
            episode_id=episode_id,
            error_class=run_class if final_status in ("unevaluated", "interrupted", "error") else None,
            interruptions=interruptions,
            worker_generation=worker_generation,
            harness_faults_fired=sorted(harness_plan.fired),
            usage={
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cache_read_input_tokens": usage["cache_read_input_tokens"],
                "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
            },
            **({"summary": record["summary"]} if record.get("summary") else {}),
            **({"evaluation": record["evaluation"]} if record.get("evaluation") else {}),
        )
        log.info(
            "run.finished",
            final_status,
            run_id=run_id,
            episode_id=episode_id,
            steps=step,
            dur_ms=duration_ms,
            score=(record.get("evaluation") or {}).get("score"),
            evaluation_status=evaluation_status,
            worker_generation=worker_generation,
            interruptions=len(interruptions) or None,
            error_class=(run_class or {}).get("code"),
        )
    return record

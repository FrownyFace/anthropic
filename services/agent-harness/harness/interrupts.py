"""Real interruptions: planned chaos triggers, and picking a half-finished run back up.

Two halves, both pure enough to unit-test without Modal:

  * `HarnessFaultPlan` — the scenario's `harness_faults[]` (PLAN.md §2.11 / schemas.HarnessFault).
    These are *real* failures the harness inflicts on itself at a chosen call: `worker_crash` kills
    the process with the request already at sandbox-env, `transport_abort` cancels the in-flight
    HTTP request. They are deliberate (`planned: true`) but nothing about them is simulated: the
    worker really dies, the connection really goes away, and the recovery path is the real one.

  * `plan_resume` — rebuild the Anthropic conversation from the persisted event log so a fresh
    worker (Modal re-invokes `run_episode` with the SAME inputs after a crash) can continue the run
    instead of starting over. Whatever tool call was in flight when the old worker died becomes an
    `interruption` event plus a synthetic `EHARNESS` tool result whose outcome is *unknown*, which
    is exactly what the agent has to reason about.

Nothing here talks to the network; `loop.py` owns the I/O.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable

from faultline_common.schemas import Interruption

from .classify import HARNESS_INTERRUPTION_TEXT, error_class, label_for, normalize_fault

# --------------------------------------------------------------------------- path matching


def normalize_path(token: str) -> str:
    """GRADING.md token normalisation: strip quotes, `./` and a `/workspace/` prefix."""
    t = (token or "").strip().strip("'\"")
    if t.startswith("/workspace/"):
        t = t[len("/workspace/"):]
    while t.startswith("./"):
        t = t[2:]
    return t.rstrip("/")


def call_paths(tool: str, args: dict[str, Any] | None) -> set[str]:
    """Every workspace path this call plausibly touches (argv tokens for `run_command`)."""
    a = args or {}
    if tool == "run_command":
        command = str(a.get("command") or "")
        try:
            tokens = shlex.split(command)
        except ValueError:  # unbalanced quotes: fall back to whitespace
            tokens = command.split()
        return {normalize_path(t) for t in tokens if t and not t.startswith("-")}
    path = a.get("path")
    return {normalize_path(str(path))} if path else set()


def primary_path(tool: str, args: dict[str, Any] | None) -> str | None:
    a = args or {}
    if tool == "run_command":
        return None
    path = a.get("path")
    return normalize_path(str(path)) if path else None


# --------------------------------------------------------------------------- harness fault plan


def fault_key(fault: dict[str, Any]) -> str:
    """Stable identity of one harness-fault trigger, persisted so it fires at most once per run."""
    return f"{fault.get('kind')}:{fault.get('tool')}:{normalize_path(str(fault.get('path') or ''))}:{fault.get('nth', 1)}"


@dataclass
class HarnessFaultPlan:
    """Which planned interruption (if any) fires on this dispatch.

    `counts` tracks matching calls per trigger and is rebuilt from the event log on resume;
    `fired` is persisted on the run record so the worker that takes over after a crash does not
    crash again on the retry of the very same call.
    """

    faults: list[dict[str, Any]] = field(default_factory=list)
    fired: set[str] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=dict)
    source: str = "scenario"

    @classmethod
    def build(cls, raw: Any, *, fired: Any = None, source: str = "scenario") -> "HarnessFaultPlan":
        faults = [dict(f) for f in (raw or []) if isinstance(f, dict) and f.get("kind")]
        return cls(faults=faults, fired=set(fired or []), source=source)

    def __bool__(self) -> bool:
        return bool(self.faults)

    def matches(self, fault: dict[str, Any], tool: str, args: dict[str, Any] | None) -> bool:
        if str(fault.get("tool") or "write_file") != tool:
            return False
        want = normalize_path(str(fault.get("path") or ""))
        return not want or want in call_paths(tool, args)

    def observe(self, tool: str, args: dict[str, Any] | None) -> None:
        """Count a dispatch without firing (used to replay history on resume)."""
        for fault in self.faults:
            if self.matches(fault, tool, args):
                key = fault_key(fault)
                self.counts[key] = self.counts.get(key, 0) + 1

    def take(self, tool: str, args: dict[str, Any] | None) -> dict[str, Any] | None:
        """Count this dispatch and return the trigger that fires on it, if any."""
        hit: dict[str, Any] | None = None
        for fault in self.faults:
            if not self.matches(fault, tool, args):
                continue
            key = fault_key(fault)
            self.counts[key] = self.counts.get(key, 0) + 1
            if hit is None and key not in self.fired and self.counts[key] == int(fault.get("nth") or 1):
                hit = fault
                self.fired.add(key)
        return hit


# --------------------------------------------------------------------------- interruption records


def interruption(
    *,
    layer: str,
    code: str,
    step: int | None = None,
    tool_use_id: str | None = None,
    tool: str | None = None,
    path: str | None = None,
    outcome_known: bool = False,
    planned: bool = False,
    resumed: bool = False,
    worker_generation: int = 1,
    at: str,
    detail: str | None = None,
) -> dict[str, Any]:
    """An `Interruption` (schemas.Interruption): always real, always about a real component."""
    return Interruption(
        step=step,
        layer=layer,  # type: ignore[arg-type]
        code=code,  # type: ignore[arg-type]
        label=label_for("real", layer, code),
        tool_use_id=tool_use_id,
        tool=tool,  # type: ignore[arg-type]
        path=path,
        outcome_known=outcome_known,
        planned=planned,
        resumed=resumed,
        worker_generation=worker_generation,
        at=at,
        detail=detail,
    ).model_dump()


def harness_result_data(tool_use_id: str, tool: str | None, path: str | None,
                        *, duration_ms: int = 0, attempts: int = 1,
                        sandbox: dict[str, Any] | None = None) -> dict[str, Any]:
    """`tool.result` payload the resuming worker writes for the call that was in flight."""
    payload = {"error": HARNESS_INTERRUPTION_TEXT, "code": "EHARNESS"}
    if path:
        payload["path"] = path
    return {
        "tool_use_id": tool_use_id,
        "tool": tool or "run_command",
        "output": json.dumps(payload),
        "is_error": True,
        "duration_ms": duration_ms,
        "error_code": "EHARNESS",
        "outcome": "unknown",
        "error_class": error_class(
            "real", "harness", "EHARNESS", outcome_known=False,
            detail="written by the resuming worker; the ledger has the truth after the fact",
        ),
        "attempts": attempts,
        "sandbox": sandbox or {},
        "summary": path or "",
        "synthetic": True,
    }


# --------------------------------------------------------------------------- resume


@dataclass
class ResumePlan:
    """Everything the new worker needs to continue a run that lost its worker."""

    episode_id: str | None
    sandbox_id: str | None
    task_prompt: str
    scenario: dict[str, Any]
    files: list[dict[str, Any]]
    workspace_root: str
    messages: list[dict[str, Any]]
    step: int
    seen_faults: list[dict[str, Any]]
    usage: dict[str, int]
    dangling: list[dict[str, Any]]
    resumed_from_event_id: int
    dropped_thinking: int = 0


def _content_of(events: list[dict[str, Any]], step: int | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(assistant blocks, tool.call events) for one step, in emission order."""
    assistant: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("step") != step:
            continue
        data = ev.get("data") or {}
        if ev.get("type") == "turn.text" and str(data.get("text") or "").strip():
            assistant.append({"type": "text", "text": str(data["text"])})
        elif ev.get("type") == "tool.call":
            calls.append(ev)
            assistant.append({
                "type": "tool_use",
                "id": data.get("tool_use_id"),
                "name": data.get("tool"),
                "input": data.get("input") or {},
            })
    return assistant, calls


def plan_resume(record: dict[str, Any], *, build_task_message: Callable[..., str]) -> ResumePlan:
    """Rebuild the conversation (and the loop's counters) from the persisted event log.

    Thinking blocks are deliberately NOT replayed: the API requires the original signature with a
    replayed `thinking` block and the event log stores only the summarised text. The default model
    (claude-haiku-4-5) emits none, so this is a no-op on every scenario we ship; the count is
    surfaced so a Sonnet/Opus resume says out loud what it dropped.
    """
    events = list(record.get("events") or [])
    reset = next((e for e in events if e.get("type") == "episode.reset"), {}) or {}
    reset_data = reset.get("data") or {}
    scenario = reset_data.get("scenario") or {}
    task_prompt = str(reset_data.get("task_prompt") or record.get("task_prompt") or "")
    files = list(reset_data.get("files") or [])
    workspace_root = str(reset_data.get("workspace_root") or "/workspace")

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": build_task_message(task_prompt, files, workspace_root)}
    ]

    results: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.get("type") == "tool.result":
            data = ev.get("data") or {}
            if data.get("tool_use_id"):
                results[str(data["tool_use_id"])] = data

    steps = sorted({e.get("step") for e in events if isinstance(e.get("step"), int)})
    dangling: list[dict[str, Any]] = []
    dropped_thinking = sum(1 for e in events if e.get("type") == "turn.thinking")
    last_step = 0

    for step in steps:
        assistant, calls = _content_of(events, step)
        if not assistant:
            continue
        last_step = max(last_step, int(step))
        messages.append({"role": "assistant", "content": assistant})
        tool_results: list[dict[str, Any]] = []
        step_dangling: list[dict[str, Any]] = []
        for call in calls:
            data = call.get("data") or {}
            tuid = str(data.get("tool_use_id") or "")
            result = results.get(tuid)
            if result is None:
                step_dangling.append({
                    "tool_use_id": tuid,
                    "tool": data.get("tool"),
                    "input": data.get("input") or {},
                    "path": primary_path(str(data.get("tool") or ""), data.get("input") or {}),
                    "step": step,
                    "event_id": call.get("id"),
                })
                continue
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tuid,
                "content": str(result.get("output") or "(no output)"),
                "is_error": bool(result.get("is_error")),
            })
        if tool_results:
            messages.append({"role": "user", "content": tool_results})
        dangling.extend(step_dangling)

    seen_faults = [normalize_fault(e.get("data") or {}, step=e.get("step"))
                   for e in events if e.get("type") == "fault.fired"]
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    for ev in events:
        if ev.get("type") != "llm.call":
            continue
        for key, value in ((ev.get("data") or {}).get("usage") or {}).items():
            if key in usage and isinstance(value, int):
                usage[key] += value

    return ResumePlan(
        episode_id=record.get("episode_id") or reset_data.get("episode_id"),
        sandbox_id=reset_data.get("sandbox_id"),
        task_prompt=task_prompt,
        scenario=scenario,
        files=files,
        workspace_root=workspace_root,
        messages=messages,
        step=last_step,
        seen_faults=[f for f in seen_faults if f],
        usage=usage,
        dangling=dangling,
        resumed_from_event_id=int(events[-1].get("id") or 0) if events else -1,
        dropped_thinking=dropped_thinking,
    )


def replay_call_counts(plan: HarnessFaultPlan, record: dict[str, Any]) -> HarnessFaultPlan:
    """Re-count every tool call already in the log so `nth` keeps its meaning across a resume."""
    for ev in record.get("events") or []:
        if ev.get("type") != "tool.call":
            continue
        data = ev.get("data") or {}
        plan.observe(str(data.get("tool") or ""), data.get("input") or {})
    return plan


def has_progress(record: dict[str, Any] | None) -> bool:
    """True when a run already produced events beyond `run.started` — i.e. it can be resumed."""
    events = list((record or {}).get("events") or [])
    return any(e.get("type") not in ("run.started", "log") for e in events)


# --------------------------------------------------------------------------- ledger echo


_LEDGER_TOOLS = {"run_command", "read_file", "write_file", "list_dir"}


def align_ledger(ledger: list[dict[str, Any]], calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match the gym's ledger rows to this run's tool calls, in order.

    The ledger counts one row per MCP call the *gym* served and numbers them with its own step
    counter, which is not the harness's step (a model turn can issue several calls, and a call the
    harness abandoned mid-flight still reached the server). So the two are aligned greedily by
    order, requiring the tool name to agree and the path to agree when both sides know one.
    """
    out: list[dict[str, Any]] = []
    used: set[int] = set()
    for row in ledger or []:
        tool = row.get("tool")
        path = normalize_path(str(row.get("path") or "")) or None
        match_idx: int | None = None
        for i, call in enumerate(calls):
            if i in used or call.get("tool") != tool:
                continue
            call_path = call.get("path")
            if path and call_path and path != call_path:
                continue
            if path and not call_path and path not in call_paths(str(tool or ""), call.get("input")):
                continue
            match_idx = i
            break
        if match_idx is None:
            out.append({"tool_use_id": None, "step": row.get("step"), "tool": tool, "path": path})
            continue
        used.add(match_idx)
        call = calls[match_idx]
        out.append({"tool_use_id": call.get("tool_use_id"), "step": call.get("step"),
                    "tool": tool, "path": path})
    return out

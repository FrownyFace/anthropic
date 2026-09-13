"""Shared wire contracts for Faultline (pydantic v2).

Three consumers: services/sandbox-env (gym + MCP), services/agent-harness (model loop + API),
scripts/ (smoke + evidence). apps/web mirrors these in src/lib/types.ts — keep both in sync.

Sections: faults & scenarios · MCP tool I/O · ledger · gym REST · harness runs/events · persistence · health.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ----------------------------------------------------------------------------- faults & scenarios

FaultKind = Literal["missing_file", "denied_write", "ack_lost"]
FaultMode = Literal["sticky", "transient"]


class FaultSpec(BaseModel):
    """One injected failure. Lives ONLY in sandbox-env state (never inside the command sandbox).

    sticky    : applied at reset (e.g. the file is really deleted) and never lifted.
    transient : intercepted at the tool boundary for the first `hits` matching calls, then lifted.
    """

    kind: FaultKind
    path: str = Field(description="workspace-relative path the fault targets")
    mode: FaultMode = "transient"
    hits: int | None = Field(default=1, description="matching calls affected before the fault lifts (None = forever)")
    delay_ms: int = Field(default=3000, description="ack_lost only: how long to hold the response before failing it")


class FaultPlan(BaseModel):
    faults: list[FaultSpec] = Field(default_factory=list)


HarnessFaultKind = Literal["worker_crash", "transport_abort"]


class Scenario(BaseModel):
    """Public scenario fields (safe to show the agent and the browser)."""

    id: str
    title: str
    description: str = Field(description="one-paragraph human description shown in the UI")
    task_prompt: str = Field(description="what the agent is asked to do (goes into the user turn)")
    max_steps: int = 20
    fault_kinds: list[FaultKind] = Field(default_factory=list, description="which kinds are in play (not where/when)")
    harness_faults: list["HarnessFault"] = Field(default_factory=list, description="REAL interruptions the harness inflicts on itself (see HarnessFault)")
    checks: list["CheckSpec"] = Field(default_factory=list, description="what the grader will look for (public: id, description, weight)")
    faults_public: list["FaultPublic"] = Field(default_factory=list, description="fault classes in play (kind, layer, origin, description) — never paths, hits or timing")


class CheckSpec(BaseModel):
    id: str
    description: str
    weight: float = 1.0


class FaultPublic(BaseModel):
    kind: FaultKind | HarnessFaultKind
    origin: ErrorOrigin
    layer: ErrorLayer
    description: str


class HarnessFault(BaseModel):
    """A real interruption the harness inflicts on ITSELF at a chosen call (chaos trigger, real failure mode).

    worker_crash    : after dispatching the matching tool call and waiting `after_ms`, the run_episode
                      process exits hard (os._exit) — the request is already at sandbox-env, so the
                      side effect completes; a fresh worker resumes the run from the persisted events.
    transport_abort : the in-flight HTTP request is cancelled client-side after `after_ms`; the server
                      still completes it; the agent gets ETRANSPORT with outcome unknown.
    """

    kind: HarnessFaultKind
    tool: ToolName = "write_file"
    path: str
    nth: int = Field(default=1, ge=1, description="fire on the nth matching call")
    after_ms: int = Field(default=400, ge=0, description="delay after dispatch before interrupting (lets the request reach the server)")


# ----------------------------------------------------------------------------- MCP tool I/O

ToolName = Literal["run_command", "read_file", "write_file", "list_dir"]
ErrorCode = Literal[
    # OS/HTTP-style codes the AGENT sees (the same text whether injected or real):
    "ENOENT",      # no such file or directory
    "EACCES",      # permission denied
    "ETIMEDOUT",   # the response never arrived (ack lost); the operation MAY have completed
    "EINVAL",      # bad argument (path outside /workspace, bad mode, is-a-directory)
    # Real-failure codes (never injected; only the layer that really failed produces them):
    "ESANDBOX",    # the Modal Sandbox is gone/unavailable (terminated, not found, exec failed)
    "ETRANSPORT",  # harness <-> sandbox-env HTTP/MCP failure (connection reset, 5xx, client abort)
    "EHARNESS",    # the harness worker itself was interrupted while the call was in flight
    "EMODEL",      # Anthropic API failure that ended the turn
    "EGYM",        # gym control-plane (reset/observe/evaluate/delete) failure
    "EINTERNAL",   # unexpected exception inside sandbox-env (a bug; see detail)
    "ENOEPISODE",  # missing/unknown X-Faultline-Episode header
]

# Provenance: WHO caused a failure. The agent never sees this; ledger, events and UI do.
ErrorOrigin = Literal[
    "injected",  # intercepted at the tool boundary by the fault plan; nothing real failed
    "staged",    # the scenario really changed the world at reset (e.g. deleted a file); the error is a real OS error
    "real",      # unplanned failure of a real component (sandbox, transport, harness, model, gym, filesystem)
]
ErrorLayer = Literal["filesystem", "sandbox", "transport", "harness", "model", "gym", "boundary"]


class ErrorClass(BaseModel):
    """Differentiated classification of one failure, for humans and the UI (never shown to the agent).

    Fixed `label` vocabulary (apps/web renders these verbatim; see docs/error-taxonomy.md):
      injected/boundary  "simulated: missing file (file still on disk)"
                         "simulated: write denied (nothing written)"
                         "simulated: lost ack (write landed; response withheld)"
      staged/filesystem  "staged: file absent since reset"
      real/filesystem    "real: OS error in sandbox"
      real/sandbox       "real: sandbox terminated or unavailable"
      real/transport     "real: transport failure harness<->sandbox-env (outcome unknown)"
      real/harness       "real: harness worker interrupted mid-call (outcome unknown)"
      real/model         "real: model API error"
      real/gym           "real: gym control-plane failure"
      real/boundary      "real: internal error in sandbox-env"
    """

    origin: ErrorOrigin
    layer: ErrorLayer
    code: ErrorCode
    kind: FaultKind | None = Field(default=None, description="injected/staged only: which fault kind")
    label: str
    outcome_known: bool = Field(description="False when the side effect may or may not have happened (ack lost, transport, harness interruption)")
    side_effect_applied: bool | None = Field(default=None, description="ground truth from the ledger when known: did the write/command actually run?")
    detail: str | None = None


STDIO_CAP = 8_000  # chars per stream in run_command
CONTENT_CAP = 32_000  # chars in read_file


class RunCommandInput(BaseModel):
    command: str = Field(description="bash -lc <command>, cwd=/workspace")
    timeout_s: int = Field(default=30, ge=1, le=60)


class RunCommandOutput(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    truncated: bool = False


class ReadFileInput(BaseModel):
    path: str


class ReadFileOutput(BaseModel):
    path: str
    content: str
    size: int
    sha256: str
    truncated: bool = False


class WriteFileInput(BaseModel):
    path: str
    content: str
    mode: Literal["overwrite", "append"] = "overwrite"


class WriteFileOutput(BaseModel):
    path: str
    bytes_written: int
    sha256: str = Field(description="sha256 of the whole file after the write")


class ListDirInput(BaseModel):
    path: str = "."


class DirEntry(BaseModel):
    name: str
    type: Literal["file", "dir", "other"]
    size: int | None = None


class ListDirOutput(BaseModel):
    path: str
    entries: list[DirEntry]


class ToolError(BaseModel):
    """Returned (as the tool result text, is_error=True) for both real and injected failures.

    The agent cannot tell injected from real: same shape, same wording as the OS/HTTP layer would use.
    """

    error: str = Field(description="human-readable, OS/HTTP-style, e.g. 'cat: config/settings.json: No such file or directory'")
    code: ErrorCode
    path: str | None = None
    detail: str | None = None


# ----------------------------------------------------------------------------- ledger (private to sandbox-env; public subset via observe)


class FaultFired(BaseModel):
    step: int
    kind: FaultKind
    path: str
    mode: FaultMode
    origin: ErrorOrigin = Field(default="injected", description="injected (intercepted) or staged (sticky missing_file hit for real)")
    layer: ErrorLayer = Field(default="boundary", description="boundary for injected, filesystem for staged")
    description: str = Field(default="", description="one plain-language sentence for the UI, e.g. 'the write was applied but the acknowledgement was withheld'")


class LedgerEntry(BaseModel):
    step: int
    ts: str
    tool: ToolName
    args_digest: str = Field(description="sha256[:12] of canonical json args")
    path: str | None = Field(default=None, description="primary path the call touched, if determinable")
    command: str | None = Field(default=None, description="run_command only")
    mutating: bool = False
    fault: FaultFired | None = None
    outcome: Literal["ok", "error", "short_circuit", "ack_lost", "unknown"] = Field(description="unknown = the sandbox may or may not have applied it (WorkspaceError/timeout after dispatch)")
    exit_code: int | None = None
    duration_ms: int = 0
    origin: ErrorOrigin | None = Field(default=None, description="set when outcome != ok: injected | staged | real")
    error_code: ErrorCode | None = None
    interrupted: bool = Field(default=False, description="the harness reported (POST /episodes/{id}/interruptions) that it never received this call's response")


# ----------------------------------------------------------------------------- gym REST


class FileEntry(BaseModel):
    path: str
    size: int
    sha256: str
    status: Literal["added", "modified", "deleted", "unchanged"] = "unchanged"


class ResetRequest(BaseModel):
    scenario_id: str
    seed: int | None = None


class ResetResponse(BaseModel):
    episode_id: str
    scenario: Scenario
    workspace_root: str = "/workspace"
    files: list[FileEntry]
    sandbox_id: str | None = Field(default=None, description="for ops/evidence only; never given to the model")
    control_token: str | None = Field(default=None, description="per-episode bearer minted at reset; required as header X-Faultline-Control on observe/evaluate/delete/interruptions. Held by the harness only — never sent to the model or the browser, never listed by GET /episodes")


class FileDiff(BaseModel):
    path: str
    unified: str


class ObserveResponse(BaseModel):
    episode_id: str
    step: int
    files: list[FileEntry]
    diffs: list[FileDiff] = Field(default_factory=list)
    faults_fired: list[FaultFired] = Field(default_factory=list, description="only faults that already fired; pending ones stay hidden")
    done: bool = False


class Check(BaseModel):
    id: str
    ok: bool
    weight: float = 1.0
    detail: str = ""


class TestsResult(BaseModel):
    passed: int = 0
    failed: int = 0
    errors: int = 0
    output: str = ""


class EvaluateResponse(BaseModel):
    episode_id: str
    score: float = Field(ge=0, le=100, description="60*tests_pass + 40*weighted recovery checks")
    passed: bool = Field(description="hidden tests all pass")
    checks: list[Check]
    tests: TestsResult
    ledger: list[LedgerEntry] = Field(default_factory=list, description="full ledger, released only at evaluate time")


# ----------------------------------------------------------------------------- harness runs & events

ToolOutcome = Literal[
    "executed",      # the sandbox ran it and it succeeded
    "failed",        # the sandbox ran it and reported failure (non-zero exit, real ENOENT/EACCES)
    "not_executed",  # refused/short-circuited before the sandbox saw it (injected EACCES/ENOENT, EINVAL, ESANDBOX)
    "unknown",       # may or may not have run (ack lost, transport failure, harness interruption)
]

EventType = Literal[
    "run.started",
    "episode.reset",
    "turn.text",
    "tool.call",
    "tool.result",
    "fault.fired",
    "workspace.diff",
    "episode.evaluated",
    "run.finished",
    "log",
    "llm.call",
    "turn.thinking",
    "interruption",
    "run.resumed",
    "episode.sandbox",
]


class Event(BaseModel):
    """Append-only, single-writer (run_episode). `id` is the 0-based sequence within the run and is
    what SSE uses as the event id / Last-Event-ID.

    data shapes by type:
      run.started       {scenario_id, model, seed, max_steps, anthropic_workspace?: masked, sandbox_env_url, worker_generation}
      episode.reset     {episode_id, files: [FileEntry], task_prompt, sandbox_id (short), attempt (1 = first provisioning), scenario, workspace_root}
      turn.text         {text}
      tool.call         {tool, input, tool_use_id, mutating}
      tool.result       {tool_use_id, tool, output: str, is_error: bool, duration_ms, fault?: FaultFired,
                         outcome: executed|failed|not_executed|unknown, error_class?: ErrorClass (iff is_error),
                         error_code?: str (compat), attempts: int (harness retries of this call; 1 = none),
                         sandbox: {id: short, alive: bool}, summary?, synthetic?: bool (resume-generated), reported_to_gym?: bool}
      fault.fired       FaultFired
      workspace.diff    {files: [FileEntry], diffs: [FileDiff]}
      episode.evaluated EvaluateResponse
      run.finished      {status, usage: {input_tokens, output_tokens}, duration_ms, error?: str,
                         evaluation_status: ok|failed|skipped, evaluation_error?: str, steps, score?,
                         error_class?: ErrorClass (present whenever status is unevaluated|interrupted|error), worker_generation, interruptions}
      log               {svc, lvl, ev, msg, ...}   (unified log line mirrored into the stream)
      llm.call          {attempt, model, stop_reason?, request_id?, usage: Usage-shaped {input_tokens, output_tokens, cache_read_input_tokens?, cache_creation_input_tokens?}, duration_ms, error?}
      turn.thinking     {text}   (summarised thinking, models that expose it)
      interruption      Interruption   (a REAL interruption: origin is always "real")
      run.resumed       {worker_generation, resumed_from_event_id, dangling_tool_use_id?, resumed_at, step, dropped_thinking_blocks}
      episode.sandbox   {sandbox_id, status: alive|terminated|replaced, reason, step}   (the environment noticed the worker changed)
    Every tool.result with is_error carries data.error_class: ErrorClass; every fault.fired carries origin.
    """

    id: int
    ts: str
    run_id: str
    type: EventType
    step: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


RunStatus = Literal[
    "queued",
    "running",
    "ok",           # agent submitted / ended, evaluation succeeded
    "truncated",    # step budget exhausted (still evaluated)
    "unevaluated",  # loop finished but evaluate() failed — NOT ok, NOT an agent error
    "interrupted",  # a real interruption ended the run and no worker could continue it (sandbox gone, retries exhausted, ...)
    "error",        # the harness could not run the episode (model auth, gym reset failure, bug)
]


class Interruption(BaseModel):
    """A real interruption observed by the harness (origin is always real)."""

    step: int | None = None
    layer: ErrorLayer
    code: ErrorCode
    label: str
    tool_use_id: str | None = Field(default=None, description="the in-flight tool call, if any")
    tool: ToolName | None = None
    path: str | None = None
    outcome_known: bool = False
    planned: bool = Field(default=False, description="True when a HarnessFault in the scenario triggered it (still a real failure)")
    resumed: bool = Field(default=False, description="a new worker picked the run up afterwards")
    worker_generation: int = Field(default=1, description="which worker observed it")
    at: str
    detail: str | None = None

MODEL_ALLOWLIST = ["claude-haiku-4-5"]  # user decision 2026-09-12 21:30 EDT: the backend rejects every other model with 400 "model must be claude-haiku-4-5"
DEFAULT_MODEL = "claude-haiku-4-5"


class RunRequest(BaseModel):
    scenario_id: str
    model: str | None = Field(default=None, description="must be in MODEL_ALLOWLIST (only claude-haiku-4-5); None = the default; anything else -> 400 'model must be claude-haiku-4-5'")
    seed: int | None = None
    max_steps: int | None = Field(default=None, ge=1, le=40)
    task_prompt: str | None = Field(default=None, description="overrides the scenario task prompt (the user edited it in the composer); wins over the scenario default")
    harness_faults: list[HarnessFault] | None = Field(default=None, description="overrides the scenario harness_faults for this run (ops/testing)")


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class RunRecord(BaseModel):
    run_id: str
    status: RunStatus
    scenario_id: str
    model: str
    seed: int | None = None
    max_steps: int = 20
    episode_id: str | None = None
    created_at: str
    finished_at: str | None = None
    events: list[Event] = Field(default_factory=list)
    evaluation: EvaluateResponse | None = None
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None
    # additive (ARCHITECTURE.md §4 / PLAN.md §2.9): persistence + transcript grouping
    conversation_id: str | None = None
    user_id: str | None = None
    task_prompt: str | None = None
    steps: int | None = None
    summary: str | None = Field(default=None, description="the agent's submit summary")
    error_class: ErrorClass | None = Field(default=None, description="why status is error/unevaluated/interrupted")
    interruptions: list[Interruption] = Field(default_factory=list)
    worker_generation: int = Field(default=1, description="1 + number of resumes")


class InterruptionReport(BaseModel):
    """POST /episodes/{id}/interruptions — the harness tells the gym it never got a response to a call,
    so the ledger can mark that entry `interrupted` and the grader can require verification before
    the next write to the same path (same rule as ack_lost)."""

    tool: ToolName
    path: str | None = None
    args_digest: str | None = Field(default=None, description="sha256[:12] of canonical json args, to match the ledger row")
    layer: ErrorLayer
    code: ErrorCode
    at: str


# ----------------------------------------------------------------------------- persistence (harness Store; ARCHITECTURE.md §4)


class User(BaseModel):
    id: str = Field(description="u_<uuid4>, minted by the browser, sent as X-Faultline-User")
    created_at: str
    last_seen_at: str
    user_agent: str | None = None


class RunSummary(BaseModel):
    id: str
    status: RunStatus
    scenario_id: str
    model: str
    score: float | None = None
    created_at: str
    finished_at: str | None = None


class Conversation(BaseModel):
    id: str = Field(description="c_<hex ms><12 hex>")
    user_id: str
    scenario_id: str
    title: str
    created_at: str
    updated_at: str
    archived_at: str | None = None


class ConversationSummary(BaseModel):
    id: str
    title: str
    scenario_id: str
    created_at: str
    updated_at: str
    last_run: RunSummary | None = Field(default=None, description="newest run; `id` is the run id")


class Block(BaseModel):
    """One content block of a transcript message (Anthropic content-block shape, flattened).

    Wire form carries parsed objects (`input`, `fault`); the Store keeps them as JSON text columns.
    """

    id: str
    message_id: str | None = None
    seq: int
    type: Literal["text", "thinking", "tool_use", "tool_result"]
    text: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    input: dict[str, Any] | None = Field(default=None, description="tool_use input, parsed")
    is_error: bool | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    fault: FaultFired | None = None
    truncated: bool = False


class Message(BaseModel):
    id: str
    conversation_id: str
    run_id: str
    seq: int
    role: Literal["user", "assistant"]
    step: int | None = None
    created_at: str
    blocks: list[Block] = Field(default_factory=list)


class LlmCall(BaseModel):
    id: str
    run_id: str
    step: int
    attempt: int = 1
    model: str
    request_id: str | None = None
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    started_at: str
    duration_ms: int | None = None
    error: str | None = None


class ConversationDetail(BaseModel):
    conversation: Conversation
    runs: list[RunSummary] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)


class CreateConversationRequest(BaseModel):
    scenario_id: str
    title: str | None = None


class ConversationRunRequest(BaseModel):
    model: str | None = None
    seed: int | None = None
    max_steps: int | None = Field(default=None, ge=1, le=40)
    task_prompt: str | None = None


# ----------------------------------------------------------------------------- health


class Health(BaseModel):
    svc: Literal["harness", "sandbox-env", "web"]
    ok: bool
    version: str = "0.1.0"
    has_provider_key: bool = Field(default=False, description="MUST be false on every web-facing function")
    model_default: str | None = None
    sandbox_env_url: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

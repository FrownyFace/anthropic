/**
 * Wire contracts mirrored from packages/common/faultline_common/schemas.py.
 *
 * Keep field names identical to the pydantic models — the harness serialises those models
 * straight onto the wire. If a field is missing here it is simply not rendered; if a field is
 * added here it MUST be optional so an older harness still type-checks at runtime.
 */

export type FaultKind = 'missing_file' | 'denied_write' | 'ack_lost'
export type FaultMode = 'sticky' | 'transient'

export interface FaultSpec {
  kind: FaultKind
  path: string
  mode: FaultMode
  hits: number | null
  delay_ms: number
}

export type HarnessFaultKind = 'worker_crash' | 'transport_abort'

/**
 * A REAL interruption the harness inflicts on itself at a chosen call (chaos trigger).
 *   worker_crash    : the run_episode process exits hard after dispatching the call; the side
 *                     effect completes at sandbox-env and a fresh worker resumes the run.
 *   transport_abort : the in-flight HTTP request is cancelled client-side; the server still
 *                     completes it; the agent gets ETRANSPORT with outcome unknown.
 */
export interface HarnessFault {
  kind: HarnessFaultKind
  tool: ToolName
  path: string
  nth: number
  after_ms: number
}

export interface CheckSpec {
  id: string
  description: string
  weight: number
}

/** One fault class the catalogue publishes: what can fail and who causes it, never where or when. */
export interface FaultPublic {
  kind: FaultKind | HarnessFaultKind | string
  origin: ErrorOrigin
  layer: ErrorLayer
  /** One plain-language sentence for the UI. */
  description: string
}

export interface Scenario {
  id: string
  title: string
  description: string
  task_prompt: string
  max_steps: number
  fault_kinds: FaultKind[]
  /** Planned REAL interruptions (the UI may say "includes a real harness interruption"). Older harness: absent. */
  harness_faults?: HarnessFault[]
  /** What the grader will look for (public: id, description, weight). Older harness: absent. */
  checks?: CheckSpec[]
  /** Every fault class in play with its origin (staged / injected / real). Older harness: absent. */
  faults_public?: FaultPublic[]
}

export type ToolName = 'run_command' | 'read_file' | 'write_file' | 'list_dir'

/**
 * OS/HTTP-style codes the agent sees (identical whether injected or real) plus the real-failure
 * codes only the layer that really failed produces. Mirrors `ErrorCode` in schemas.py.
 */
export type ErrorCode =
  | 'ENOENT' // no such file or directory
  | 'EACCES' // permission denied
  | 'ETIMEDOUT' // the response never arrived (ack lost, or a real client timeout)
  | 'EINVAL' // bad argument (path outside /workspace, bad mode, is-a-directory)
  | 'ESANDBOX' // the sandbox is gone/unavailable
  | 'ETRANSPORT' // harness <-> sandbox-env HTTP/MCP failure
  | 'EHARNESS' // the harness worker itself was interrupted while the call was in flight
  | 'EMODEL' // Anthropic API failure that ended the turn
  | 'EGYM' // gym control-plane (reset/observe/evaluate/delete) failure
  | 'EINTERNAL' // unexpected exception inside sandbox-env
  | 'ENOEPISODE' // missing/unknown X-Faultline-Episode header

export const ERROR_CODES: readonly ErrorCode[] = [
  'ENOENT',
  'EACCES',
  'ETIMEDOUT',
  'EINVAL',
  'ESANDBOX',
  'ETRANSPORT',
  'EHARNESS',
  'EMODEL',
  'EGYM',
  'EINTERNAL',
  'ENOEPISODE',
]

/** WHO caused a failure. The agent never sees this; ledger, events and UI do. */
export type ErrorOrigin =
  | 'injected' // intercepted at the tool boundary by the fault plan; nothing real failed
  | 'staged' // the scenario really changed the world at reset; the error is a real OS error
  | 'real' // unplanned failure of a real component

export const ERROR_ORIGINS: readonly ErrorOrigin[] = ['injected', 'staged', 'real']

/** WHICH layer really failed. */
export type ErrorLayer = 'filesystem' | 'sandbox' | 'transport' | 'harness' | 'model' | 'gym' | 'boundary'

export const ERROR_LAYERS: readonly ErrorLayer[] = [
  'filesystem',
  'sandbox',
  'transport',
  'harness',
  'model',
  'gym',
  'boundary',
]

/**
 * Differentiated classification of one failure (docs/error-taxonomy.md). `label` is a fixed
 * string the UI renders verbatim.
 */
export interface ErrorClass {
  origin: ErrorOrigin
  layer: ErrorLayer
  code: ErrorCode
  /** injected/staged only: which fault kind */
  kind?: FaultKind | null
  label: string
  /** false when the side effect may or may not have happened (ack lost, transport, harness interruption) */
  outcome_known: boolean
  /** ground truth from the ledger when known: did the write/command actually run? */
  side_effect_applied?: boolean | null
  detail?: string | null
}

export interface ToolError {
  error: string
  code: ErrorCode
  path?: string | null
  detail?: string | null
}

export interface FaultFired {
  step: number
  kind: FaultKind
  path: string
  mode: FaultMode
  /** injected (intercepted) or staged (sticky missing_file hit for real). Schema default: injected. */
  origin?: ErrorOrigin
  /** boundary for injected, filesystem for staged. Schema default: boundary. */
  layer?: ErrorLayer
  /** One plain-language sentence for the UI. */
  description?: string
}

export type LedgerOutcome = 'ok' | 'error' | 'short_circuit' | 'ack_lost'

export interface LedgerEntry {
  step: number
  ts: string
  tool: ToolName
  args_digest: string
  path?: string | null
  command?: string | null
  mutating: boolean
  fault?: FaultFired | null
  outcome: LedgerOutcome
  exit_code?: number | null
  duration_ms: number
  /** set when outcome != ok: injected | staged | real */
  origin?: ErrorOrigin | null
  error_code?: ErrorCode | null
  /** the harness reported (POST /episodes/{id}/interruptions) that it never received this call's response */
  interrupted?: boolean
}

export type FileStatus = 'added' | 'modified' | 'deleted' | 'unchanged'

export interface FileEntry {
  path: string
  size: number
  sha256: string
  status: FileStatus
}

export interface FileDiff {
  path: string
  unified: string
}

export interface Check {
  id: string
  ok: boolean
  weight: number
  detail: string
}

export interface TestsResult {
  passed: number
  failed: number
  errors: number
  output: string
}

export interface EvaluateResponse {
  episode_id: string
  score: number
  passed: boolean
  checks: Check[]
  tests: TestsResult
  ledger: LedgerEntry[]
}

export type EventType =
  | 'run.started'
  | 'episode.reset'
  | 'turn.text'
  | 'tool.call'
  | 'tool.result'
  | 'fault.fired'
  | 'workspace.diff'
  | 'episode.evaluated'
  | 'run.finished'
  | 'log'
  | 'llm.call'
  | 'turn.thinking'
  | 'interruption'
  | 'run.resumed'
  | 'episode.sandbox'

/** Every SSE frame / RunRecord.events element. `id` is the 0-based sequence used as Last-Event-ID. */
export interface Event {
  id: number
  ts: string
  run_id: string
  type: EventType
  step?: number | null
  data: Record<string, unknown>
}

export type RunStatus =
  | 'queued'
  | 'running'
  | 'ok' // agent submitted / ended, evaluation succeeded
  | 'truncated' // step budget exhausted (still evaluated)
  | 'unevaluated' // loop finished but evaluate() failed — NOT ok, NOT an agent error
  | 'interrupted' // a real interruption ended the run without resume
  | 'error' // the harness could not run the episode (model auth, gym reset failure, bug)

/** A real interruption observed by the harness (origin is always real). */
export interface Interruption {
  step?: number | null
  layer: ErrorLayer
  code: ErrorCode
  label: string
  /** the in-flight tool call, if any */
  tool_use_id?: string | null
  tool?: ToolName | null
  path?: string | null
  outcome_known: boolean
  /** true when a HarnessFault in the scenario triggered it (still a real failure) */
  planned: boolean
  /** a new worker picked the run up afterwards */
  resumed: boolean
  /** which worker observed it */
  worker_generation: number
  at: string
  detail?: string | null
}

/** How a tool call actually ended, as the harness reports it on `tool.result.data.outcome`. */
export type ToolOutcome =
  | 'executed' // the sandbox ran it and it succeeded
  | 'failed' // the sandbox ran it and reported failure (non-zero exit, real ENOENT/EACCES)
  | 'not_executed' // refused/short-circuited before the sandbox saw it
  | 'unknown' // may or may not have run (ack lost, transport failure, harness interruption)

export interface Usage {
  input_tokens: number
  output_tokens: number
  /** Prompt-cache counters; older harness builds omit them. */
  cache_read_input_tokens?: number
  cache_creation_input_tokens?: number
}

export interface RunRecord {
  run_id: string
  status: RunStatus
  scenario_id: string
  model: string
  seed?: number | null
  max_steps: number
  episode_id?: string | null
  created_at: string
  finished_at?: string | null
  events: Event[]
  evaluation?: EvaluateResponse | null
  usage: Usage
  error?: string | null
  // additive (ARCHITECTURE.md §4): persistence + transcript grouping
  conversation_id?: string | null
  user_id?: string | null
  task_prompt?: string | null
  steps?: number | null
  /** The agent's submit summary. */
  summary?: string | null
  /** why status is error/unevaluated/interrupted */
  error_class?: ErrorClass | null
  /** every real interruption observed */
  interruptions?: Interruption[]
  /** 1 + number of resumes */
  worker_generation?: number
}

export interface Health {
  svc: 'harness' | 'sandbox-env' | 'web'
  ok: boolean
  version: string
  has_provider_key: boolean
  model_default?: string | null
  sandbox_env_url?: string | null
  detail: Record<string, unknown>
}

/** The only model this demo runs. The harness rejects anything else server-side (400). */
export const MODEL_ALLOWLIST = ['claude-haiku-4-5'] as const
export const DEFAULT_MODEL = 'claude-haiku-4-5'

/** `m` when it is on the allowlist, else null — so a harness `model_default` can never widen the choice. */
export function allowedModel(m: string | null | undefined): string | null {
  return typeof m === 'string' && (MODEL_ALLOWLIST as readonly string[]).includes(m) ? m : null
}

export type LogLevel = 'debug' | 'info' | 'warn' | 'error'

/** Unified log line — same field names as faultline_common.log. */
export interface LogLine {
  ts: string
  svc: string
  lvl: LogLevel
  ev: string
  msg: string
  run_id?: string
  episode_id?: string
  step?: number
  [k: string]: unknown
}

// ---------------------------------------------------------------- typed event payloads

export interface ToolCallData {
  tool: ToolName | string
  input: Record<string, unknown>
  tool_use_id: string
}

export interface ToolResultData {
  tool_use_id: string
  tool?: ToolName | string
  output: string
  is_error: boolean
  duration_ms: number
  fault?: FaultFired | null
  /** How the call actually ended (primary execution source when present). */
  outcome?: ToolOutcome
  /** Present iff is_error. */
  error_class?: ErrorClass | null
  /** compat: the bare code */
  error_code?: string | null
  /** harness retries of this call; 1 = none */
  attempts?: number
  sandbox?: { id: string; alive: boolean } | null
}

/** `run.finished.data.evaluation_status`: ok = graded, failed = evaluate() broke, skipped = never attempted. */
export type EvaluationStatus = 'ok' | 'failed' | 'skipped'

/** `run.resumed` — emitted by the worker that picked the run up after an interruption. */
export interface RunResumedData {
  worker_generation: number
  resumed_from_event_id: number
  dangling_tool_use_id?: string | null
  resumed_at: string
}

export type SandboxStatus = 'alive' | 'terminated' | 'replaced'

/** `episode.sandbox` — the environment noticed the worker changed / the sandbox went away. */
export interface EpisodeSandboxData {
  sandbox_id: string
  status: SandboxStatus
  reason?: string | null
  step?: number | null
}

// ---------------------------------------------------------------- persistence (harness Store; ARCHITECTURE.md §4)
// Mirrors of the persistence models in schemas.py. `input` / `fault` arrive parsed on the wire
// (the Store keeps them as JSON text columns).

export interface User {
  /** `u_<uuid4>`, minted by the browser and sent as `X-Faultline-User`. */
  id: string
  created_at: string
  last_seen_at: string
  user_agent?: string | null
}

export interface RunSummary {
  id: string
  status: RunStatus
  scenario_id: string
  model: string
  score: number | null
  created_at: string
  finished_at: string | null
}

export interface Conversation {
  /** `c_<hex ms><12 hex>` */
  id: string
  user_id: string
  scenario_id: string
  title: string
  created_at: string
  updated_at: string
  archived_at?: string | null
}

export interface ConversationSummary {
  id: string
  title: string
  scenario_id: string
  created_at: string
  updated_at: string
  /** Newest run; `id` is the run id. */
  last_run: RunSummary | null
}

export type BlockType = 'text' | 'thinking' | 'tool_use' | 'tool_result'

/** One content block of a transcript message (Anthropic content-block shape, flattened). */
export interface Block {
  id: string
  message_id?: string | null
  seq: number
  type: BlockType
  /** text / thinking / tool_result output (already capped upstream). */
  text?: string | null
  tool_name?: string | null
  /** tool_use and tool_result (join key). */
  tool_use_id?: string | null
  /** tool_use input, parsed. */
  input?: Record<string, unknown> | null
  is_error?: boolean | null
  exit_code?: number | null
  duration_ms?: number | null
  fault?: FaultFired | null
  truncated: boolean
  // Provenance a newer Store may project onto tool_result blocks (the schema's Block has none
  // today); read when present, never required.
  outcome?: ToolOutcome | null
  error_class?: ErrorClass | null
  attempts?: number | null
  sandbox?: { id: string; alive: boolean } | null
}

export type MessageRole = 'user' | 'assistant'

export interface Message {
  id: string
  conversation_id: string
  run_id: string
  /** Order within the conversation. */
  seq: number
  role: MessageRole
  /** Harness step that produced it (null for the task prompt). */
  step?: number | null
  created_at: string
  blocks: Block[]
}

export interface ConversationDetail {
  conversation: Conversation
  runs: RunSummary[]
  messages: Message[]
}

export interface CreateConversationRequest {
  scenario_id: string
  title?: string | null
}

export interface ConversationRunRequest {
  model?: string | null
  seed?: number | null
  max_steps?: number | null
  task_prompt?: string | null
}

// ---------------------------------------------------------------- harness API responses (ARCHITECTURE.md §5.1)
// Not pydantic models — plain dicts the routes return. Kept here so api.ts and the hooks share them.

export interface MeResponse {
  user_id: string
  conversations: number
}

export interface ConversationRunResponse {
  run_id: string
  conversation_id: string
}

export interface UpdateConversationRequest {
  title?: string
  archived?: boolean
}

export interface ArchiveResponse {
  archived: true
}

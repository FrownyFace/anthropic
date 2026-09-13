/**
 * Pure fold: Event[] -> ViewState.
 *
 * Everything the UI renders comes from here, so the same function drives a live SSE run and a
 * bundled replay, and the interesting derivations (which call a fault hit, whether the agent
 * recovered) are unit-testable without a browser.
 *
 * Derivations worth naming:
 *   - `mutating` / `read` classification and argv-token path matching approximate
 *     services/sandbox-env/GRADING.md so the badges in the UI line up with what the grader looks
 *     at. They are a live hint: the grader's checks (`episode.evaluated`) are authoritative.
 *   - `recovered`: a faulted call — or any call whose outcome is unknown (ack lost, transport
 *     failure, a worker interrupted mid-call) — is marked recovered when a later call on the same
 *     path succeeds AND a successful *read* of that path happened at or before that success.
 *     Retrying blind does not count — that is the behaviour the `lost-ack` and `worker-crash`
 *     scenarios grade (`verified_before_rewrite` treats ack-lost and interrupted rows alike). The UI
 *     labels this "read-back seen" and defers to the grader's checks once the evaluation exists.
 */

import type {
  Check,
  EpisodeSandboxData,
  ErrorClass,
  ErrorCode,
  ErrorLayer,
  ErrorOrigin,
  EvaluateResponse,
  EvaluationStatus,
  Event,
  FaultFired,
  FileDiff,
  FileEntry,
  Interruption,
  LogLine,
  RunRecord,
  RunResumedData,
  RunStatus,
  Scenario,
  ToolOutcome,
  Usage,
} from './types'
import { ERROR_CODES, ERROR_LAYERS, ERROR_ORIGINS } from './types'
import { taxonomyLabel } from './codes'

export const LOG_BUFFER_LIMIT = 1000

/** Every wire value of `RunStatus`; anything else on a record or a `run.finished` is ignored. */
export const RUN_STATUSES: readonly RunStatus[] = [
  'queued',
  'running',
  'ok',
  'truncated',
  'unevaluated',
  'interrupted',
  'error',
]

/** A run in one of these states will never produce another event. */
export const TERMINAL_STATUSES: readonly RunStatus[] = ['ok', 'error', 'truncated', 'unevaluated', 'interrupted']

export function asRunStatus(v: unknown): RunStatus | null {
  return typeof v === 'string' && (RUN_STATUSES as readonly string[]).includes(v) ? (v as RunStatus) : null
}

export function isTerminal(s: RunStatus | null | undefined): boolean {
  return !!s && TERMINAL_STATUSES.includes(s)
}

// --------------------------------------------------------------------------- view types

export interface ToolResultView {
  toolUseId: string
  output: string
  isError: boolean
  durationMs: number
  /** Parsed JSON payload when the tool returned structured output. */
  stdout: string | null
  stderr: string | null
  content: string | null
  exitCode: number | null
  errorText: string | null
  errorCode: ErrorCode | null
  truncated: boolean
  bytesWritten: number | null
  entries: { name: string; type: string; size?: number | null }[] | null
  /** `tool.result.data.error_class` — present iff is_error on a harness that classifies errors. */
  errorClass: ErrorClass | null
  /** `tool.result.data.outcome` — how the call actually ended, as the harness reports it. */
  outcome: ToolOutcome | null
  /** `tool.result.data.attempts` — harness retries of this call (1 = none). */
  attempts: number | null
  /** `tool.result.data.sandbox` — which sandbox answered and whether it was alive. */
  sandbox: { id: string; alive: boolean } | null
}

export interface ToolCallView {
  /** Global order across the run — the only reliable ordering when several calls share a step. */
  seq: number
  step: number
  toolUseId: string
  tool: string
  input: Record<string, unknown>
  /** Primary workspace path the call touches, if determinable. */
  path: string | null
  command: string | null
  mutating: boolean
  read: boolean
  result: ToolResultView | null
  fault: FaultFired | null
  recovered: boolean
}

export interface StepView {
  step: number
  /** Summarised thinking for the step (`turn.thinking`), several blocks joined; null if none. */
  thinking: string | null
  texts: string[]
  calls: ToolCallView[]
}

/** One `messages.create` round trip (`llm.call`). Retries share a step with `attempt > 1`. */
export interface LlmCallView {
  step: number
  attempt: number
  model: string
  stopReason: string | null
  usage: Usage
  durationMs: number | null
  requestId: string | null
  error: string | null
}

export interface ViewState {
  runId: string | null
  status: RunStatus
  scenarioId: string | null
  model: string | null
  seed: number | null
  maxSteps: number
  episodeId: string | null
  taskPrompt: string | null
  anthropicWorkspace: string | null
  startedAt: string | null
  finishedAt: string | null
  /** Highest step seen so far. */
  step: number
  /**
   * Token totals. Accumulated from `llm.call` while the run is in flight (last attempt per step,
   * so a retried request is not double counted); `run.finished` and the RunRecord always win.
   */
  usage: Usage
  /** Every LLM round trip seen, in event order. */
  llmCalls: LlmCallView[]
  steps: StepView[]
  files: FileEntry[]
  diffs: FileDiff[]
  faults: FaultFired[]
  evaluation: EvaluateResponse | null
  logs: LogLine[]
  error: string | null
  durationMs: number | null
  lastEventId: number
  eventCount: number
  /** Every real interruption (`interruption` events; the RunRecord's list wins on seed). */
  interruptions: Interruption[]
  /** Every `run.resumed` (a fresh worker picked the run up), in order. */
  resumes: RunResumedData[]
  /** 1 + number of resumes (highest worker_generation seen). */
  workerGeneration: number
  /** Run-level classification: `run.finished.data.error_class` or `RunRecord.error_class`. */
  errorClass: ErrorClass | null
  /** The public scenario, when `episode.reset` carried it. */
  scenario: Scenario | null
  /** `episode.sandbox` notices (sandbox alive / terminated / replaced), in order. */
  sandboxEvents: EpisodeSandboxData[]
  /** Short sandbox id last seen (episode.reset / episode.sandbox / tool.result.sandbox). */
  sandboxId: string | null
  /** Last reported liveness; null until something reports it. */
  sandboxAlive: boolean | null
  /** `episode.reset.data.attempt` — >1 when the harness re-provisioned the workspace. */
  resetAttempt: number | null
  /** `run.finished.data.evaluation_status` / `evaluation_error`. */
  evaluationStatus: EvaluationStatus | null
  evaluationError: string | null
}

export function initialState(): ViewState {
  return {
    runId: null,
    status: 'queued',
    scenarioId: null,
    model: null,
    seed: null,
    maxSteps: 20,
    episodeId: null,
    taskPrompt: null,
    anthropicWorkspace: null,
    startedAt: null,
    finishedAt: null,
    step: 0,
    usage: { input_tokens: 0, output_tokens: 0 },
    llmCalls: [],
    steps: [],
    files: [],
    diffs: [],
    faults: [],
    evaluation: null,
    logs: [],
    error: null,
    durationMs: null,
    lastEventId: -1,
    eventCount: 0,
    interruptions: [],
    resumes: [],
    workerGeneration: 1,
    errorClass: null,
    scenario: null,
    sandboxEvents: [],
    sandboxId: null,
    sandboxAlive: null,
    resetAttempt: null,
    evaluationStatus: null,
    evaluationError: null,
  }
}

// --------------------------------------------------------------------------- path / verb helpers

/** `"./src/x.py"`, `'/workspace/src/x.py'`, `"src/x.py"` all normalise to `src/x.py`. */
export function normalisePath(raw: string): string {
  let s = raw.trim()
  if (s.length >= 2 && ((s[0] === '"' && s.endsWith('"')) || (s[0] === "'" && s.endsWith("'")))) {
    s = s.slice(1, -1)
  }
  s = s.replace(/^\/workspace\//, '')
  s = s.replace(/^\.\//, '')
  s = s.replace(/^\/+/, '')
  // `ls src/ratelimiter/` and `read_file src/ratelimiter` name the same thing.
  if (s.length > 1) s = s.replace(/\/+$/, '')
  return s
}

/** Cheap argv split: whitespace, honouring single/double quotes. Good enough for token matching. */
export function argvTokens(command: string): string[] {
  const out: string[] = []
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(command)) !== null) {
    out.push(m[1] ?? m[2] ?? m[3] ?? '')
  }
  return out
}

const MUTATING_RE =
  /(>>?|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\btouch\b|\bmkdir\b|\btruncate\b|\bpatch\b|\bgit\s+(apply|checkout|reset|restore))/

const PY_WRITE_RE = /python[0-9.]*\s+.*-c[\s\S]*open\([\s\S]*['"][wa]\+?['"]/

const READ_VERB_RE =
  /\b(cat|head|tail|grep|rg|wc|diff|less|cmp|sha256sum|md5sum|ls|stat|find|pytest|nl|od)\b|\bsed\s+-n\b|python[0-9.]*\s+-m\s+pytest/

export function commandIsMutating(command: string): boolean {
  return MUTATING_RE.test(command) || PY_WRITE_RE.test(command)
}

export function commandIsRead(command: string): boolean {
  return !commandIsMutating(command) && READ_VERB_RE.test(command)
}

/** Does this call touch `path`? Direct for path tools; argv-token match for run_command. */
export function callTouches(call: Pick<ToolCallView, 'tool' | 'command' | 'path'>, path: string): boolean {
  const target = normalisePath(path)
  if (call.tool === 'run_command') {
    if (!call.command) return false
    return argvTokens(call.command).some((t) => {
      const n = normalisePath(t)
      return n === target || n.endsWith(`/${target}`)
    })
  }
  return call.path !== null && call.path === target
}

function inputPath(tool: string, input: Record<string, unknown>): string | null {
  const raw = input?.path
  if (typeof raw === 'string' && raw.length > 0) return normalisePath(raw)
  if (tool === 'list_dir') return '.'
  return null
}

export interface CallClassification {
  path: string | null
  command: string | null
  mutating: boolean
  read: boolean
}

/**
 * Path / verb classification of a tool call from its name and input. Exported so the persisted
 * transcript path (transcript.ts) classifies exactly as the live fold does.
 */
export function classifyCall(tool: string, input: Record<string, unknown>): CallClassification {
  return classify(tool, input)
}

function classify(tool: string, input: Record<string, unknown>): CallClassification {
  if (tool === 'run_command') {
    const command = typeof input?.command === 'string' ? (input.command as string) : null
    return {
      path: null,
      command,
      mutating: command ? commandIsMutating(command) : false,
      read: command ? commandIsRead(command) : false,
    }
  }
  if (tool === 'write_file') {
    return { path: inputPath(tool, input), command: null, mutating: true, read: false }
  }
  if (tool === 'read_file' || tool === 'list_dir') {
    return { path: inputPath(tool, input), command: null, mutating: false, read: true }
  }
  // `submit` and anything else the harness adds locally.
  return { path: inputPath(tool, input), command: null, mutating: false, read: false }
}

// --------------------------------------------------------------------------- result parsing

export function parseToolResult(
  toolUseId: string,
  output: string,
  isError: boolean,
  durationMs: number,
): ToolResultView {
  const base: ToolResultView = {
    toolUseId,
    output: output ?? '',
    isError,
    durationMs,
    stdout: null,
    stderr: null,
    content: null,
    exitCode: null,
    errorText: null,
    errorCode: null,
    truncated: false,
    bytesWritten: null,
    entries: null,
    errorClass: null,
    outcome: null,
    attempts: null,
    sandbox: null,
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(output)
  } catch {
    if (isError) base.errorText = output
    return base
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return base
  const o = parsed as Record<string, unknown>
  if (typeof o.stdout === 'string') base.stdout = o.stdout
  if (typeof o.stderr === 'string') base.stderr = o.stderr
  if (typeof o.content === 'string') base.content = o.content
  if (typeof o.exit_code === 'number') base.exitCode = o.exit_code
  if (typeof o.truncated === 'boolean') base.truncated = o.truncated
  if (typeof o.bytes_written === 'number') base.bytesWritten = o.bytes_written
  if (typeof o.error === 'string') base.errorText = o.error
  if (typeof o.code === 'string' && (ERROR_CODES as readonly string[]).includes(o.code)) {
    base.errorCode = o.code as ErrorCode
  }
  if (Array.isArray(o.entries)) {
    base.entries = o.entries as ToolResultView['entries']
  }
  return base
}

// --------------------------------------------------------------------------- recovery

function flatten(steps: StepView[]): ToolCallView[] {
  const all: ToolCallView[] = []
  for (const s of steps) all.push(...s.calls)
  all.sort((a, b) => a.seq - b.seq)
  return all
}

/** A call the agent could not know the outcome of: a fault hit it, or the outcome is unknown. */
function inDoubt(c: ToolCallView): boolean {
  if (c.fault) return true
  const r = c.result
  return !!r && (r.outcome === 'unknown' || r.errorClass?.outcome_known === false)
}

/**
 * Recompute `recovered` for every faulted or outcome-unknown call. Cheap (a run is tens of calls)
 * and keeps the flag correct as later events arrive.
 */
export function applyRecovery(steps: StepView[]): StepView[] {
  const all = flatten(steps)
  const recoveredIds = new Set<string>()

  for (const c of all) {
    if (!inDoubt(c)) continue
    const path = normalisePath(c.fault?.path || c.path || '')
    if (!path) continue
    const later = all.filter((x) => x.seq > c.seq)
    const successes = later.filter((x) => x.result && !x.result.isError && callTouches(x, path))
    if (successes.length === 0) continue
    const firstSuccessSeq = successes[0]!.seq
    const verified = later.some(
      (x) => x.seq <= firstSuccessSeq && x.read && x.result && !x.result.isError && callTouches(x, path),
    )
    if (verified) recoveredIds.add(c.toolUseId)
  }

  if (recoveredIds.size === 0) {
    return steps.map((s) =>
      s.calls.some((c) => c.recovered) ? { ...s, calls: s.calls.map((c) => ({ ...c, recovered: false })) } : s,
    )
  }
  return steps.map((s) => ({
    ...s,
    calls: s.calls.map((c) => {
      const next = recoveredIds.has(c.toolUseId)
      return c.recovered === next ? c : { ...c, recovered: next }
    }),
  }))
}

// --------------------------------------------------------------------------- the fold

function upsertStep(steps: StepView[], step: number): { steps: StepView[]; idx: number } {
  const idx = steps.findIndex((s) => s.step === step)
  if (idx >= 0) return { steps, idx }
  const next = [...steps, { step, thinking: null, texts: [], calls: [] }].sort((a, b) => a.step - b.step)
  return { steps: next, idx: next.findIndex((s) => s.step === step) }
}

// --------------------------------------------------------------------------- usage

function asUsage(v: unknown, fallback: Usage = { input_tokens: 0, output_tokens: 0 }): Usage {
  const o = asRecord(v)
  const out: Usage = {
    input_tokens: num(o.input_tokens, fallback.input_tokens),
    output_tokens: num(o.output_tokens, fallback.output_tokens),
  }
  const cr = typeof o.cache_read_input_tokens === 'number' ? o.cache_read_input_tokens : fallback.cache_read_input_tokens
  const cc =
    typeof o.cache_creation_input_tokens === 'number'
      ? o.cache_creation_input_tokens
      : fallback.cache_creation_input_tokens
  if (cr !== undefined) out.cache_read_input_tokens = cr
  if (cc !== undefined) out.cache_creation_input_tokens = cc
  return out
}

/**
 * Sum token usage over LLM calls, counting only the LAST attempt of each step so a request that
 * was retried after a 429/5xx is not billed twice in the header.
 */
export function sumLlmUsage(calls: LlmCallView[]): Usage {
  const lastPerStep = new Map<number, LlmCallView>()
  for (const c of calls) {
    const prev = lastPerStep.get(c.step)
    if (!prev || c.attempt >= prev.attempt) lastPerStep.set(c.step, c)
  }
  const out: Usage = { input_tokens: 0, output_tokens: 0 }
  let cr = 0
  let cc = 0
  let sawCache = false
  for (const c of lastPerStep.values()) {
    out.input_tokens += c.usage.input_tokens
    out.output_tokens += c.usage.output_tokens
    if (typeof c.usage.cache_read_input_tokens === 'number') {
      cr += c.usage.cache_read_input_tokens
      sawCache = true
    }
    if (typeof c.usage.cache_creation_input_tokens === 'number') {
      cc += c.usage.cache_creation_input_tokens
      sawCache = true
    }
  }
  if (sawCache) {
    out.cache_read_input_tokens = cr
    out.cache_creation_input_tokens = cc
  }
  return out
}

function usageIsEmpty(u: Usage | null | undefined): boolean {
  return !u || ((u.input_tokens ?? 0) === 0 && (u.output_tokens ?? 0) === 0)
}

function asRecord(v: unknown): Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

function num(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback
}

function str(v: unknown): string | null {
  return typeof v === 'string' ? v : null
}

function asOrigin(v: unknown): ErrorOrigin | null {
  return typeof v === 'string' && (ERROR_ORIGINS as readonly string[]).includes(v) ? (v as ErrorOrigin) : null
}

function asLayer(v: unknown): ErrorLayer | null {
  return typeof v === 'string' && (ERROR_LAYERS as readonly string[]).includes(v) ? (v as ErrorLayer) : null
}

function asCode(v: unknown): ErrorCode | null {
  return typeof v === 'string' && (ERROR_CODES as readonly string[]).includes(v) ? (v as ErrorCode) : null
}

function asFault(v: unknown): FaultFired | null {
  const o = asRecord(v)
  if (typeof o.kind !== 'string' || typeof o.path !== 'string') return null
  const fault: FaultFired = {
    step: num(o.step, 0),
    kind: o.kind as FaultFired['kind'],
    path: o.path,
    mode: (o.mode as FaultFired['mode']) ?? 'transient',
  }
  const origin = asOrigin(o.origin)
  const layer = asLayer(o.layer)
  if (origin) fault.origin = origin
  if (layer) fault.layer = layer
  if (typeof o.description === 'string' && o.description.length > 0) fault.description = o.description
  return fault
}

/** Structural check for an ErrorClass payload; anything short of (origin, layer, code, label) is ignored. */
export function asErrorClass(v: unknown): ErrorClass | null {
  const o = asRecord(v)
  const origin = asOrigin(o.origin)
  const layer = asLayer(o.layer)
  const code = asCode(o.code)
  if (!origin || !layer || !code || typeof o.label !== 'string') return null
  return {
    origin,
    layer,
    code,
    kind: typeof o.kind === 'string' ? (o.kind as ErrorClass['kind']) : null,
    label: o.label,
    // Unknown execution is not failed execution: only an explicit `true` counts as known.
    outcome_known: o.outcome_known === true,
    side_effect_applied: typeof o.side_effect_applied === 'boolean' ? o.side_effect_applied : null,
    detail: str(o.detail),
  }
}

/** Structural check for an Interruption payload (layer + code required); the event's step is the fallback. */
export function asInterruption(v: unknown, fallbackStep: number | null): Interruption | null {
  const o = asRecord(v)
  const layer = asLayer(o.layer)
  const code = asCode(o.code)
  if (!layer || !code) return null
  return {
    step: typeof o.step === 'number' ? o.step : fallbackStep,
    layer,
    code,
    label: str(o.label) ?? '',
    tool_use_id: str(o.tool_use_id),
    tool: str(o.tool) as Interruption['tool'],
    path: str(o.path),
    outcome_known: o.outcome_known === true,
    planned: o.planned === true,
    resumed: o.resumed === true,
    worker_generation: Math.max(1, Math.trunc(num(o.worker_generation, 1))),
    at: str(o.at) ?? '',
    detail: str(o.detail),
  }
}

export function asToolOutcome(v: unknown): ToolOutcome | null {
  return v === 'executed' || v === 'failed' || v === 'not_executed' || v === 'unknown' ? v : null
}

function asSandboxRef(v: unknown): { id: string; alive: boolean } | null {
  const o = asRecord(v)
  if (typeof o.id !== 'string') return null
  return { id: o.id, alive: o.alive !== false }
}

function asScenario(v: unknown): Scenario | null {
  const o = asRecord(v)
  if (typeof o.id !== 'string' || typeof o.title !== 'string') return null
  return o as unknown as Scenario
}

function sameInterruption(a: Interruption, b: Interruption): boolean {
  if (a.tool_use_id && b.tool_use_id) return a.tool_use_id === b.tool_use_id
  return a.at === b.at && a.code === b.code && (a.step ?? null) === (b.step ?? null)
}

function nextSeq(steps: StepView[]): number {
  let max = -1
  for (const s of steps) for (const c of s.calls) if (c.seq > max) max = c.seq
  return max + 1
}

/**
 * `log` event `ev: "ledger.resolution"` (emitted once, after grading): the ledger's ground truth
 * for calls whose outcome the agent never learned — an ack-lost write, or a call a dead worker
 * left in flight. Folds `side_effect_applied` onto the matching call's error class (creating a
 * minimal one when the record carries none) so an "unknown" status can add "the ledger later
 * confirmed it had landed". The call's outcome stays what the agent saw: a later event is
 * folded, an earlier one is never rewritten, and the stream stays append-only.
 *
 *   resolutions[]: {tool_use_id, step, ledger_step, tool, path, outcome, origin?, error_code?,
 *                   interrupted, side_effect_applied}
 */
export function applyLedgerResolution(steps: StepView[], raw: unknown): StepView[] {
  if (!Array.isArray(raw)) return steps
  const byCall = new Map<string, { applied: boolean; interrupted: boolean; code: ErrorCode | null }>()
  for (const r of raw) {
    const o = asRecord(r)
    if (typeof o.tool_use_id !== 'string' || typeof o.side_effect_applied !== 'boolean') continue
    byCall.set(o.tool_use_id, { applied: o.side_effect_applied, interrupted: o.interrupted === true, code: asCode(o.error_code) })
  }
  if (byCall.size === 0) return steps
  return steps.map((s) => {
    if (!s.calls.some((c) => c.result !== null && byCall.has(c.toolUseId))) return s
    return {
      ...s,
      calls: s.calls.map((c) => {
        const res = byCall.get(c.toolUseId)
        if (!res || !c.result) return c
        const existing = c.result.errorClass
        const origin: ErrorOrigin = existing?.origin ?? (res.interrupted ? 'real' : (c.fault?.origin ?? 'injected'))
        const layer: ErrorLayer = existing?.layer ?? (res.interrupted ? 'harness' : (c.fault?.layer ?? 'boundary'))
        const code: ErrorCode = existing?.code ?? (res.interrupted ? 'EHARNESS' : (c.result.errorCode ?? res.code ?? 'ETIMEDOUT'))
        const errorClass: ErrorClass = existing
          ? { ...existing, side_effect_applied: res.applied }
          : {
              origin,
              layer,
              code,
              kind: c.fault?.kind ?? null,
              label: taxonomyLabel(origin, c.fault?.kind ?? null, layer) ?? `${origin}: ${code}`,
              outcome_known: false,
              side_effect_applied: res.applied,
              detail: null,
            }
        return { ...c, result: { ...c.result, errorClass } }
      }),
    }
  })
}

/** Fold one event into the view state. Pure: never mutates `state`. */
export function reduce(state: ViewState, ev: Event): ViewState {
  if (!ev || typeof ev.type !== 'string') return state
  const data = asRecord(ev.data)
  const step = typeof ev.step === 'number' ? ev.step : state.step
  const next: ViewState = {
    ...state,
    runId: state.runId ?? str(ev.run_id),
    lastEventId: typeof ev.id === 'number' && ev.id > state.lastEventId ? ev.id : state.lastEventId,
    eventCount: state.eventCount + 1,
    step: Math.max(state.step, typeof ev.step === 'number' ? ev.step : 0),
  }

  switch (ev.type) {
    case 'run.started': {
      next.status = 'running'
      next.scenarioId = str(data.scenario_id) ?? next.scenarioId
      next.model = str(data.model) ?? next.model
      next.seed = typeof data.seed === 'number' ? data.seed : next.seed
      next.maxSteps = num(data.max_steps, next.maxSteps)
      next.anthropicWorkspace = str(data.anthropic_workspace) ?? next.anthropicWorkspace
      next.startedAt = state.startedAt ?? str(ev.ts)
      return next
    }

    case 'episode.reset': {
      next.episodeId = str(data.episode_id) ?? next.episodeId
      next.taskPrompt = str(data.task_prompt) ?? next.taskPrompt
      if (Array.isArray(data.files)) next.files = data.files as FileEntry[]
      if (next.status === 'queued') next.status = 'running'
      next.scenario = asScenario(data.scenario) ?? next.scenario
      const sandboxId = str(data.sandbox_id)
      if (sandboxId) {
        next.sandboxId = sandboxId
        next.sandboxAlive = true
      }
      if (typeof data.attempt === 'number') next.resetAttempt = data.attempt
      return next
    }

    case 'turn.text': {
      const text = str(data.text)
      if (!text || text.trim().length === 0) return next
      const { steps, idx } = upsertStep(state.steps, step)
      const target = steps[idx]!
      const updated = [...steps]
      updated[idx] = { ...target, texts: [...target.texts, text] }
      next.steps = updated
      return next
    }

    case 'turn.thinking': {
      const text = str(data.text)
      if (!text || text.trim().length === 0) return next
      const { steps, idx } = upsertStep(state.steps, step)
      const target = steps[idx]!
      const updated = [...steps]
      updated[idx] = { ...target, thinking: target.thinking ? `${target.thinking}\n\n${text}` : text }
      next.steps = updated
      return next
    }

    case 'llm.call': {
      const view: LlmCallView = {
        step,
        attempt: Math.max(1, Math.trunc(num(data.attempt, 1))),
        model: str(data.model) ?? state.model ?? 'unknown',
        stopReason: str(data.stop_reason),
        usage: asUsage(data.usage),
        durationMs: typeof data.duration_ms === 'number' ? data.duration_ms : null,
        requestId: str(data.request_id),
        error: str(data.error),
      }
      // Same (step, attempt) twice means a replayed frame: replace rather than append.
      const i = state.llmCalls.findIndex((c) => c.step === view.step && c.attempt === view.attempt)
      const llmCalls = i >= 0 ? state.llmCalls.map((c, j) => (j === i ? view : c)) : [...state.llmCalls, view]
      next.llmCalls = llmCalls
      // `run.finished` (or a terminal record) is the authoritative total; before that, keep a
      // running sum so the header moves while the run is in flight.
      if (!isTerminal(state.status)) next.usage = sumLlmUsage(llmCalls)
      return next
    }

    case 'tool.call': {
      const tool = str(data.tool) ?? 'unknown'
      const toolUseId = str(data.tool_use_id) ?? `${ev.id}`
      const input = asRecord(data.input)
      const { steps, idx } = upsertStep(state.steps, step)
      const target = steps[idx]!
      if (target.calls.some((c) => c.toolUseId === toolUseId)) {
        next.steps = steps
        return next
      }
      const cls = classify(tool, input)
      const call: ToolCallView = {
        seq: nextSeq(steps),
        step,
        toolUseId,
        tool,
        input,
        path: cls.path,
        command: cls.command,
        mutating: cls.mutating,
        read: cls.read,
        result: null,
        fault: null,
        recovered: false,
      }
      const updated = [...steps]
      updated[idx] = { ...target, calls: [...target.calls, call] }
      next.steps = applyRecovery(updated)
      return next
    }

    case 'tool.result': {
      const toolUseId = str(data.tool_use_id)
      if (!toolUseId) return next
      const isError = data.is_error === true
      const result = parseToolResult(
        toolUseId,
        str(data.output) ?? '',
        isError,
        num(data.duration_ms, 0),
      )
      result.errorClass = isError ? asErrorClass(data.error_class) : null
      result.outcome = asToolOutcome(data.outcome)
      result.attempts = typeof data.attempts === 'number' ? Math.max(1, Math.trunc(data.attempts)) : null
      result.sandbox = asSandboxRef(data.sandbox)
      if (result.sandbox) {
        next.sandboxId = result.sandbox.id
        next.sandboxAlive = result.sandbox.alive
      }
      const fault = asFault(data.fault)
      // Count the fault whether or not its call is known (a synthesised call after an SSE gap
      // must not undercount the header's faults stat).
      if (fault && !state.faults.some((f) => f.step === fault.step && f.path === fault.path && f.kind === fault.kind)) {
        next.faults = [...state.faults, fault]
      }
      let found = false
      const updated = state.steps.map((s) => {
        if (!s.calls.some((c) => c.toolUseId === toolUseId)) return s
        found = true
        return {
          ...s,
          calls: s.calls.map((c) =>
            c.toolUseId === toolUseId ? { ...c, result, fault: fault ?? c.fault } : c,
          ),
        }
      })
      if (!found) {
        // A result without its call (resumed mid-run, or events dropped): synthesise the call so
        // the transcript still shows the output rather than silently losing it.
        const tool = str(data.tool) ?? 'unknown'
        const { steps, idx } = upsertStep(state.steps, step)
        const target = steps[idx]!
        const cls = classify(tool, {})
        const synth: ToolCallView = {
          seq: nextSeq(steps),
          step,
          toolUseId,
          tool,
          input: {},
          path: cls.path,
          command: cls.command,
          mutating: cls.mutating,
          read: cls.read,
          result,
          fault,
          recovered: false,
        }
        const s2 = [...steps]
        s2[idx] = { ...target, calls: [...target.calls, synth] }
        next.steps = applyRecovery(s2)
        return next
      }
      next.steps = applyRecovery(updated)
      return next
    }

    case 'fault.fired': {
      const fault = asFault(data) ?? asFault(data.fault)
      if (!fault) return next
      const known = state.faults.some(
        (f) => f.step === fault.step && f.path === fault.path && f.kind === fault.kind,
      )
      next.faults = known ? state.faults : [...state.faults, fault]
      // `fault.step` is sandbox-env's LEDGER index (it counts MCP calls; the grader scores against
      // it) and is the dedupe key above. The harness turn the fault belongs to is the event's own
      // `step`, so attach by that — and by `tool_use_id` when the payload names one — never by
      // the ledger index, which routinely names a later or non-existent turn.
      const atStep = typeof ev.step === 'number' ? ev.step : fault.step
      const targetCall = str(data.tool_use_id)
      const updated = state.steps.map((s) => {
        if (s.step !== atStep) return s
        let attached = false
        const calls = s.calls.map((c) => {
          if (attached || c.fault) return c
          if (targetCall) {
            if (c.toolUseId !== targetCall) return c
          } else if (!callTouches(c, fault.path) && c.path !== normalisePath(fault.path)) {
            return c
          }
          attached = true
          return { ...c, fault }
        })
        return attached ? { ...s, calls } : s
      })
      next.steps = applyRecovery(updated)
      return next
    }

    case 'workspace.diff': {
      if (Array.isArray(data.files)) next.files = data.files as FileEntry[]
      if (Array.isArray(data.diffs)) next.diffs = data.diffs as FileDiff[]
      return next
    }

    case 'episode.evaluated': {
      const checks = Array.isArray(data.checks) ? (data.checks as Check[]) : []
      next.evaluation = {
        episode_id: str(data.episode_id) ?? state.episodeId ?? '',
        score: num(data.score, 0),
        passed: data.passed === true,
        checks,
        tests: asRecord(data.tests) as unknown as EvaluateResponse['tests'],
        ledger: Array.isArray(data.ledger) ? (data.ledger as EvaluateResponse['ledger']) : [],
      }
      return next
    }

    case 'run.finished': {
      // A missing or unrecognised status keeps the previous one; it never defaults to success.
      next.status = asRunStatus(data.status) ?? state.status
      next.usage = asUsage(data.usage, state.usage)
      next.durationMs = typeof data.duration_ms === 'number' ? data.duration_ms : state.durationMs
      next.error = str(data.error) ?? state.error
      next.finishedAt = str(ev.ts) ?? state.finishedAt
      next.errorClass = asErrorClass(data.error_class) ?? state.errorClass
      const es = str(data.evaluation_status)
      if (es === 'ok' || es === 'failed' || es === 'skipped') next.evaluationStatus = es
      next.evaluationError = str(data.evaluation_error) ?? state.evaluationError
      return next
    }

    case 'interruption': {
      const it = asInterruption(data, typeof ev.step === 'number' ? ev.step : null)
      if (!it) return next
      const i = state.interruptions.findIndex((x) => sameInterruption(x, it))
      next.interruptions = i >= 0 ? state.interruptions.map((x, j) => (j === i ? it : x)) : [...state.interruptions, it]
      next.workerGeneration = Math.max(state.workerGeneration, it.worker_generation)
      return next
    }

    case 'run.resumed': {
      const gen = Math.max(1, Math.trunc(num(data.worker_generation, state.workerGeneration + 1)))
      const resumed: RunResumedData = {
        worker_generation: gen,
        resumed_from_event_id: num(data.resumed_from_event_id, state.lastEventId),
        dangling_tool_use_id: str(data.dangling_tool_use_id),
        resumed_at: str(data.resumed_at) ?? str(ev.ts) ?? '',
      }
      const known = state.resumes.some((r) => r.worker_generation === gen)
      next.resumes = known ? state.resumes : [...state.resumes, resumed]
      next.workerGeneration = Math.max(state.workerGeneration, gen)
      // The interruption this resume answers (matched by the dangling call) is now `resumed`.
      const dangling = resumed.dangling_tool_use_id
      if (dangling) {
        next.interruptions = state.interruptions.map((x) =>
          x.tool_use_id === dangling && !x.resumed ? { ...x, resumed: true } : x,
        )
      }
      if (!isTerminal(state.status)) next.status = 'running'
      return next
    }

    case 'episode.sandbox': {
      const sandboxId = str(data.sandbox_id)
      const status = str(data.status)
      if (!sandboxId || (status !== 'alive' && status !== 'terminated' && status !== 'replaced')) return next
      const notice: EpisodeSandboxData = {
        sandbox_id: sandboxId,
        status,
        reason: str(data.reason),
        step: typeof data.step === 'number' ? data.step : typeof ev.step === 'number' ? ev.step : null,
      }
      next.sandboxEvents = [...state.sandboxEvents, notice]
      next.sandboxId = sandboxId
      next.sandboxAlive = status === 'alive'
      return next
    }

    case 'log': {
      const line = data as unknown as LogLine
      if (typeof line?.ev !== 'string') return next
      next.logs = [...state.logs, line].slice(-LOG_BUFFER_LIMIT)
      if (line.ev === 'ledger.resolution') next.steps = applyLedgerResolution(state.steps, data.resolutions)
      return next
    }

    default:
      return next
  }
}

export function reduceAll(events: Event[], from: ViewState = initialState()): ViewState {
  return events.reduce(reduce, from)
}

/**
 * Overlay a RunRecord's summary fields on a folded view. Record-level fields win over anything
 * the events implied, because the record is the harness's own summary. Used both to seed a view
 * from GET /runs/{id} (`fromRunRecord`) and by the polling fallback, which receives the whole
 * record on every tick — so a missed `run.finished` can never leave the header stale.
 */
export function overlayRecord(folded: ViewState, rec: RunRecord): ViewState {
  const status = asRunStatus(rec.status) ?? folded.status
  // The record's usage is the harness's own running total and wins whenever it says anything.
  // The one exception is an in-flight record whose usage is still the {0,0} default while its
  // events already carry `llm.call` sums — showing zeros there would be a regression.
  const usage =
    rec.usage && (isTerminal(status) || !usageIsEmpty(rec.usage) || usageIsEmpty(folded.usage))
      ? rec.usage
      : folded.usage
  const interruptions = Array.isArray(rec.interruptions)
    ? rec.interruptions.map((it) => asInterruption(it, null)).filter((it): it is Interruption => it !== null)
    : []
  return {
    ...folded,
    runId: rec.run_id ?? folded.runId,
    status,
    scenarioId: rec.scenario_id ?? folded.scenarioId,
    model: rec.model ?? folded.model,
    seed: rec.seed ?? folded.seed,
    maxSteps: rec.max_steps ?? folded.maxSteps,
    episodeId: rec.episode_id ?? folded.episodeId,
    taskPrompt: rec.task_prompt ?? folded.taskPrompt,
    startedAt: rec.created_at ?? folded.startedAt,
    finishedAt: rec.finished_at ?? folded.finishedAt,
    evaluation: rec.evaluation ?? folded.evaluation,
    usage,
    error: rec.error ?? folded.error,
    errorClass: asErrorClass(rec.error_class) ?? folded.errorClass,
    interruptions: interruptions.length > 0 ? interruptions : folded.interruptions,
    workerGeneration: Math.max(
      folded.workerGeneration,
      typeof rec.worker_generation === 'number' ? rec.worker_generation : 1,
    ),
  }
}

/** Seed the view from a whole RunRecord (page refresh mid-run, or a bundled demo). */
export function fromRunRecord(rec: RunRecord): ViewState {
  return overlayRecord(reduceAll(rec.events ?? []), rec)
}

// --------------------------------------------------------------------------- selectors

export function allCalls(state: ViewState): ToolCallView[] {
  return flatten(state.steps)
}

export function faultCount(state: ViewState): number {
  return allCalls(state).filter((c) => c.fault).length
}

export function recoveredCount(state: ViewState): number {
  return allCalls(state).filter((c) => c.fault && c.recovered).length
}

export function elapsedMs(state: ViewState, now: number = Date.now()): number | null {
  if (state.durationMs !== null) return state.durationMs
  if (!state.startedAt) return null
  const t0 = Date.parse(state.startedAt)
  if (Number.isNaN(t0)) return null
  const end = state.finishedAt ? Date.parse(state.finishedAt) : now
  return Math.max(0, (Number.isNaN(end) ? now : end) - t0)
}

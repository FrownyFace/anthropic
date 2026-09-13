/**
 * Source-independent transcript view model.
 *
 * The UI renders one shape whether the run is streaming (ViewState from the reducer), being
 * replayed from a bundled RunRecord (also ViewState), or read back from the harness Store as
 * Anthropic-shaped messages (ConversationDetail.messages). The persisted path re-uses the
 * reducer's own classification, result parsing and recovery derivation, so a fault badge or a
 * "read-back seen" hint means the same thing on every path. That hint is a heuristic from call
 * order; the grader's checks (`evaluation`) are authoritative and the UI prefers them once they
 * exist. The messages projection carries no evaluation or run error of its own — the caller
 * overlays them from the run record (`transcriptFromMessages(messages, run, overlay)`).
 */

import {
  applyRecovery,
  classifyCall,
  isTerminal,
  parseToolResult,
  type StepView,
  type ToolCallView,
  type ViewState,
} from './reducer'
import type { Block, EvaluateResponse, Message, RunStatus, RunSummary } from './types'

export interface Turn {
  step: number
  thinking: string | null
  texts: string[]
  calls: ToolCallView[]
}

export interface Transcript {
  runId: string | null
  status: RunStatus
  scenarioId: string | null
  model: string | null
  taskPrompt: string | null
  turns: Turn[]
  evaluation: EvaluateResponse | null
  score: number | null
  /** The run's error text (`run.finished.error` / `RunRecord.error`); RunSummary carries none. */
  error: string | null
  /** True while more turns may still arrive. */
  live: boolean
}

/** Record-level fields the messages projection does not carry (from `GET /runs/{id}`). */
export interface TranscriptOverlay {
  evaluation?: EvaluateResponse | null
  error?: string | null
  status?: RunStatus | null
}

export function transcriptFromViewState(state: ViewState, live: boolean): Transcript {
  return {
    runId: state.runId,
    status: state.status,
    scenarioId: state.scenarioId,
    model: state.model,
    taskPrompt: state.taskPrompt,
    turns: state.steps.map((s) => ({ step: s.step, thinking: s.thinking, texts: s.texts, calls: s.calls })),
    evaluation: state.evaluation,
    score: state.evaluation?.score ?? null,
    error: state.error,
    live,
  }
}

function sortedBlocks(m: Message): Block[] {
  return [...(m.blocks ?? [])].sort((a, b) => a.seq - b.seq)
}

function upsertTurn(turns: StepView[], step: number): StepView {
  const found = turns.find((t) => t.step === step)
  if (found) return found
  const fresh: StepView = { step, thinking: null, texts: [], calls: [] }
  turns.push(fresh)
  turns.sort((a, b) => a.step - b.step)
  return fresh
}

function highestStep(turns: StepView[]): number {
  return turns.reduce((m, t) => Math.max(m, t.step), 0)
}

function nextSeq(turns: StepView[]): number {
  let max = -1
  for (const t of turns) for (const c of t.calls) if (c.seq > max) max = c.seq
  return max + 1
}

function makeCall(turns: StepView[], step: number, toolUseId: string, tool: string, input: Record<string, unknown>): ToolCallView {
  const cls = classifyCall(tool, input)
  return {
    seq: nextSeq(turns),
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
}

function findCall(turns: StepView[], toolUseId: string): { turn: StepView; idx: number } | null {
  for (const turn of turns) {
    const idx = turn.calls.findIndex((c) => c.toolUseId === toolUseId)
    if (idx >= 0) return { turn, idx }
  }
  return null
}

function attachResult(turns: StepView[], msg: Message, b: Block): void {
  const toolUseId = b.tool_use_id
  if (!toolUseId) return
  const result = parseToolResult(toolUseId, b.text ?? '', b.is_error === true, b.duration_ms ?? 0)
  if (result.exitCode === null && typeof b.exit_code === 'number') result.exitCode = b.exit_code
  if (b.truncated) result.truncated = true
  const fault = b.fault ?? null

  const hit = findCall(turns, toolUseId)
  if (hit) {
    const prev = hit.turn.calls[hit.idx]!
    hit.turn.calls[hit.idx] = { ...prev, result, fault: fault ?? prev.fault }
    return
  }
  const step = typeof msg.step === 'number' ? msg.step : highestStep(turns)
  const turn = upsertTurn(turns, step)
  const synth = makeCall(turns, step, toolUseId, b.tool_name ?? 'unknown', {})
  turn.calls.push({ ...synth, result, fault })
}

/**
 * Build the transcript of one run from a conversation's persisted messages.
 *
 * Grouping follows ARCHITECTURE.md §4.5: the first user message's text block is the task prompt;
 * each assistant message is one turn (thinking / text / tool_use blocks in seq order); the user
 * message that follows carries the tool_result blocks, matched back by tool_use_id.
 *
 * `overlay` supplies what the projection lacks: the grader's evaluation, the run error and the
 * record's status. Without it the transcript has no evaluation — which the UI must render as
 * "not graded", never as a pass.
 */
export function transcriptFromMessages(
  messages: Message[],
  run: RunSummary,
  overlay: TranscriptOverlay = {},
): Transcript {
  const mine = messages.filter((m) => m.run_id === run.id).sort((a, b) => a.seq - b.seq)

  let taskPrompt: string | null = null
  const turns: StepView[] = []

  for (const m of mine) {
    const blocks = sortedBlocks(m)

    if (m.role === 'user') {
      for (const b of blocks) {
        if (b.type === 'tool_result') {
          attachResult(turns, m, b)
        } else if (b.type === 'text' && taskPrompt === null && typeof b.text === 'string' && b.text.length > 0) {
          taskPrompt = b.text
        }
      }
      continue
    }

    const step = typeof m.step === 'number' ? m.step : highestStep(turns)
    const turn = upsertTurn(turns, step)
    for (const b of blocks) {
      switch (b.type) {
        case 'thinking': {
          const text = b.text ?? ''
          if (text.trim().length === 0) break
          turn.thinking = turn.thinking ? `${turn.thinking}\n\n${text}` : text
          break
        }
        case 'text': {
          const text = b.text ?? ''
          if (text.trim().length === 0) break
          turn.texts.push(text)
          break
        }
        case 'tool_use': {
          const toolUseId = b.tool_use_id ?? b.id
          if (turn.calls.some((c) => c.toolUseId === toolUseId)) break
          const input = b.input && typeof b.input === 'object' ? b.input : {}
          turn.calls.push(makeCall(turns, step, toolUseId, b.tool_name ?? 'unknown', input))
          break
        }
        default:
          break
      }
    }
  }

  const recovered = applyRecovery(turns)
  const evaluation = overlay.evaluation ?? null
  const status = overlay.status ?? run.status

  return {
    runId: run.id,
    status,
    scenarioId: run.scenario_id,
    model: run.model,
    taskPrompt,
    turns: recovered.map((t) => ({ step: t.step, thinking: t.thinking, texts: t.texts, calls: t.calls })),
    evaluation,
    score: evaluation?.score ?? run.score ?? null,
    error: overlay.error ?? null,
    live: !isTerminal(status),
  }
}

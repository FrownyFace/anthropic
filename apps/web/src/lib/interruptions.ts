/**
 * Harness-level failure state, derived ONLY from structured fields: real interruptions
 * (`interruption` events / `RunRecord.interruptions`), the workers that picked the run up
 * (`run.resumed`) and sandbox notices (`episode.sandbox`, `tool.result.sandbox`,
 * `error_class.layer === 'sandbox'`). Feeds the header chips (RunStatusStrip), the inline
 * transcript callouts (TranscriptView) and the replay story (story.ts), so all three say the same
 * thing about the same event. Nothing here reads message text.
 *
 * Vocabulary: an interruption is always a *real* failure (docs/error-taxonomy.md); `planned`
 * means a scenario `harness_faults` entry triggered it on purpose (chaos), which does not make it
 * any less real. "Resumed" means a fresh worker continued the same run — browser reconnection is
 * not a resume.
 */

import type { ViewState } from './reducer'
import type { Interruption, RunResumedData, ToolOutcome } from './types'

/** The noun for the in-flight call, by tool: "mid-write", "the write's fate". */
const VERB: Record<string, string> = {
  write_file: 'write',
  read_file: 'read',
  run_command: 'command',
  list_dir: 'listing',
}

export function callNoun(tool: string | null | undefined): string {
  return (tool && VERB[tool]) || 'call'
}

/** "write_file CHANGELOG.md" — how the callout names the dangling call. */
export function callLabel(it: Pick<Interruption, 'tool' | 'path'>): string | null {
  if (!it.tool) return null
  return it.path ? `${it.tool} ${it.path}` : it.tool
}

export function plannedWord(it: Pick<Interruption, 'planned'>): 'planned chaos' | 'unplanned' {
  return it.planned ? 'planned chaos' : 'unplanned'
}

/**
 * The `run.resumed` that answered this interruption: matched by the dangling call first, else by
 * worker generation (the interruption is recorded by the worker that noticed it — the one that
 * resumed). Null when no worker picked the run up (or the record has no resume events).
 */
export function resumeFor(it: Interruption, resumes: readonly RunResumedData[]): RunResumedData | null {
  if (it.tool_use_id) {
    const byCall = resumes.find((r) => r.dangling_tool_use_id === it.tool_use_id)
    if (byCall) return byCall
  }
  if (it.worker_generation > 1) {
    return resumes.find((r) => r.worker_generation === it.worker_generation) ?? null
  }
  return null
}

// --------------------------------------------------------------------------- header chips

export interface WorkerChip {
  /** e.g. "worker 2 · resumed", "interrupted at step 4". Never exactly "interrupted" (that is the status badge's word). */
  text: string
  /** Tooltip: what happened, with each interruption's label and whether it was planned. */
  title: string
  tone: 'resumed' | 'interrupted'
}

function describeInterruption(it: Interruption): string {
  const where = typeof it.step === 'number' ? ` at step ${it.step}` : ''
  return `${it.label}${where} — ${it.planned ? 'planned chaos (a scenario harness fault)' : 'unplanned'}.`
}

/** Shown when a fresh worker continued the run, or a real interruption was recorded. */
export function workerChip(
  state: Pick<ViewState, 'workerGeneration' | 'interruptions'>,
): WorkerChip | null {
  const gen = state.workerGeneration
  const its = state.interruptions
  if (gen <= 1 && its.length === 0) return null
  const details = its.map(describeInterruption)
  const unresumed = its.filter((it) => !it.resumed)
  if (gen > 1 && unresumed.length === 0) {
    const n = gen - 1
    return {
      text: `worker ${gen} · resumed`,
      tone: 'resumed',
      title: [
        `A fresh harness worker picked this run up ${n === 1 ? 'once' : `${n} times`} and continued it (worker generation ${gen}).`,
        ...details,
      ].join(' '),
    }
  }
  const last = unresumed[unresumed.length - 1] ?? its[its.length - 1]
  const step = last && typeof last.step === 'number' ? last.step : null
  return {
    text: gen > 1 ? `worker ${gen} · interrupted${step !== null ? ` at step ${step}` : ''}` : step !== null ? `interrupted at step ${step}` : 'interrupted · not resumed',
    tone: 'interrupted',
    title: ['A real interruption ended the run and no worker resumed it.', ...details].join(' '),
  }
}

export interface SandboxLoss {
  step: number | null
  /** `episode.sandbox` status, or `unavailable` when only an interruption / error class said so. */
  status: 'terminated' | 'replaced' | 'unavailable'
  reason: string | null
  sandboxId: string | null
}

/** The first sign the sandbox went away: a notice, a sandbox-layer interruption, or a sandbox-layer error class on a call. */
export function sandboxLoss(state: Pick<ViewState, 'sandboxEvents' | 'interruptions' | 'steps'>): SandboxLoss | null {
  const notice = state.sandboxEvents.find((e) => e.status === 'terminated' || e.status === 'replaced')
  if (notice) {
    return {
      step: typeof notice.step === 'number' ? notice.step : null,
      status: notice.status as 'terminated' | 'replaced',
      reason: notice.reason ?? null,
      sandboxId: notice.sandbox_id,
    }
  }
  const it = state.interruptions.find((i) => i.layer === 'sandbox')
  if (it) return { step: typeof it.step === 'number' ? it.step : null, status: 'unavailable', reason: it.label, sandboxId: null }
  for (const s of state.steps) {
    for (const c of s.calls) {
      const ec = c.result?.errorClass
      if (ec && ec.layer === 'sandbox') {
        return { step: c.step, status: 'unavailable', reason: ec.label, sandboxId: c.result?.sandbox?.id ?? null }
      }
    }
  }
  return null
}

export function sandboxChipText(loss: SandboxLoss): string {
  return loss.step !== null ? `sandbox lost at step ${loss.step}` : 'sandbox lost'
}

export function sandboxChipTitle(loss: SandboxLoss): string {
  const what =
    loss.status === 'replaced'
      ? 'The sandbox holding the workspace was replaced'
      : loss.status === 'terminated'
        ? 'The sandbox holding the workspace was terminated'
        : 'The sandbox holding the workspace became unavailable'
  return `${what}${loss.sandboxId ? ` (${loss.sandboxId})` : ''}${loss.reason ? `: ${loss.reason}` : '.'} A real failure, not a simulated one.`
}

// --------------------------------------------------------------------------- transcript callout

export interface InterruptionNote {
  /** "Real interruption: <label> (planned chaos)" */
  title: string
  /** "A fresh worker (#2) resumed from event 27; the in-flight call's outcome is unknown until the ledger says otherwise." */
  body: string
  toolUseId: string | null
  /** "write_file CHANGELOG.md", for the link to the dangling call's chip. */
  callLabel: string | null
  planned: boolean
  resumed: boolean
  /** The worker that continued the run, when one did. */
  workerGeneration: number | null
}

export function interruptionNote(it: Interruption, resumes: readonly RunResumedData[]): InterruptionNote {
  const resume = resumeFor(it, resumes)
  const resumed = it.resumed || resume !== null
  const gen = resume?.worker_generation ?? (resumed ? it.worker_generation : null)
  const fate = it.outcome_known
    ? (it.detail ?? 'the in-flight call did not complete')
    : "the in-flight call's outcome is unknown until the ledger says otherwise"
  const body = resumed
    ? `A fresh worker (#${gen}) resumed${resume ? ` from event ${resume.resumed_from_event_id}` : ' the run'}; ${fate}.`
    : `No worker resumed the run; ${fate}.`
  return {
    title: `Real interruption: ${it.label} (${plannedWord(it)})`,
    body,
    toolUseId: it.tool_use_id ?? null,
    callLabel: callLabel(it),
    planned: it.planned,
    resumed,
    workerGeneration: gen,
  }
}

// --------------------------------------------------------------------------- story sentence

/**
 * One or two plain-English sentences for the replay story, e.g. "The harness worker was really
 * killed mid-write — a planned real failure. Worker 2 picked the run up from event 27 and
 * continued; the write's fate is unknown to the agent." `outcome` is the dangling call's
 * effective outcome when its (synthetic) result exists.
 */
export function interruptionSentence(
  it: Interruption,
  resume: RunResumedData | null,
  outcome: ToolOutcome | null,
): string {
  const noun = callNoun(it.tool)
  const mid = `mid-${noun}`
  const what =
    it.layer === 'harness'
      ? `The harness worker was really killed ${mid}`
      : it.layer === 'sandbox'
        ? `The sandbox was really lost ${mid}`
        : it.layer === 'transport'
          ? `The connection between the harness and the environment was really cut ${mid}`
          : it.layer === 'model'
            ? `The model API really failed ${mid}`
            : `A real ${it.layer} failure hit ${mid}`
  const planned = it.planned ? 'a planned real failure' : 'an unplanned real failure'
  const fate =
    outcome === 'unknown' || (outcome === null && !it.outcome_known)
      ? `the ${noun}'s fate is unknown to the agent`
      : outcome === 'not_executed'
        ? `the ${noun} never ran`
        : outcome === 'executed'
          ? `the ${noun} had already completed`
          : outcome === 'failed'
            ? `the ${noun} failed`
            : `the ${noun} did not complete`
  const resumed = it.resumed || resume !== null
  const gen = resume?.worker_generation ?? it.worker_generation
  const after = resumed
    ? `Worker ${gen} picked the run up${resume ? ` from event ${resume.resumed_from_event_id}` : ''} and continued; ${fate}.`
    : `No worker picked the run up; ${fate}.`
  return `${what} — ${planned}. ${after}`
}

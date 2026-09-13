/**
 * One status per tool call, derived ONLY from structured fields — `outcome`, `error_class`, the
 * fault that fired, `is_error` and the exit code — never from the error text.
 *
 * "Unknown" is its own state. A lost acknowledgement, a transport failure or a harness interruption
 * means the call may or may not have run (docs/error-taxonomy.md, `outcome_known: false`).
 * Rendering that as a failure would tell the reader the write did not land — the one thing nobody
 * knows yet. Likewise a call the environment answered without executing (an injected
 * `missing_file` / `denied_write` short-circuit) is "not executed", not "failed".
 */

import type { ToolCallView } from './reducer'
import type { FaultFired, ToolOutcome } from './types'

export type CallStatusKind = 'running' | 'pending' | 'ok' | 'failed' | 'unknown' | 'not_executed'
export type CallStatusTone = 'ok' | 'error' | 'unknown' | 'muted'

export interface CallStatus {
  kind: CallStatusKind
  /** Short pill text: "exit 0", "error", "no ack", "not executed", … */
  label: string
  tone: CallStatusTone
  /** One or two sentences for a tooltip. */
  detail: string
  /** `data-status` value for tests and styling. */
  attr: 'running' | 'pending' | 'ok' | 'error' | 'unknown' | 'not-executed'
}

/**
 * The harness's `outcome` when it sent one; otherwise what the structured error class or the fault
 * that fired implies (FAULTS.md: `ack_lost` always leaves the outcome unknown to the agent; an
 * injected `missing_file` / `denied_write` never reaches the sandbox). Null when nothing structured
 * says anything — the caller then falls back to `is_error` and the exit code.
 */
export function effectiveOutcome(
  result: NonNullable<ToolCallView['result']>,
  fault: FaultFired | null,
): ToolOutcome | null {
  if (result.outcome) return result.outcome
  const ec = result.errorClass
  if (ec && ec.outcome_known === false) return 'unknown'
  if (fault) {
    const origin = fault.origin ?? 'injected'
    if (fault.kind === 'ack_lost') return 'unknown'
    if (origin === 'injected' && (fault.kind === 'missing_file' || fault.kind === 'denied_write')) {
      return 'not_executed'
    }
  }
  // The persisted projection (schemas.Block) carries no outcome / error_class, so the ToolError
  // body's `code` is the only structured signal left. The real-failure codes are never injected
  // (docs/error-taxonomy.md) and ETIMEDOUT is "the response never arrived" whoever caused it, so
  // these are safe to read; ENOENT / EACCES are ambiguous (injected or real) and fall through.
  if (result.isError) {
    switch (result.errorCode) {
      case 'ETIMEDOUT':
      case 'ETRANSPORT':
      case 'EHARNESS':
        return 'unknown'
      case 'ESANDBOX':
      case 'EINVAL':
      case 'EINTERNAL':
      case 'ENOEPISODE':
        return 'not_executed'
      default:
        return null
    }
  }
  return null
}

export function callStatus(call: Pick<ToolCallView, 'result' | 'fault'>, live = false): CallStatus {
  const r = call.result
  if (!r) {
    return live
      ? { kind: 'running', label: 'running', tone: 'muted', detail: 'Waiting for the result.', attr: 'running' }
      : { kind: 'pending', label: 'no result', tone: 'muted', detail: 'No result was recorded for this call.', attr: 'pending' }
  }
  const ec = r.errorClass
  const outcome = effectiveOutcome(r, call.fault)
  const exit = r.exitCode

  if (outcome === 'unknown') {
    const ackLost = call.fault?.kind === 'ack_lost'
    const landed =
      ec?.side_effect_applied === true
        ? ' The ledger later confirmed it had landed.'
        : ec?.side_effect_applied === false
          ? ' The ledger later confirmed it had not run.'
          : ackLost
            ? ' (The environment withheld the acknowledgement on purpose; the call did run.)'
            : ''
    return {
      kind: 'unknown',
      label: ackLost ? 'no ack' : 'unknown',
      tone: 'unknown',
      detail: `The response never arrived, so the agent cannot know whether this call ran. Reading the state back before retrying is the right move.${landed}`,
      attr: 'unknown',
    }
  }

  if (outcome === 'not_executed') {
    const real = ec?.origin === 'real'
    return {
      kind: 'not_executed',
      label: 'not executed',
      tone: real ? 'error' : 'unknown',
      detail: real
        ? 'Refused before it reached the sandbox (a real error at the tool boundary); nothing ran.'
        : 'Answered by the environment without running (an injected fault); nothing on disk changed.',
      attr: 'not-executed',
    }
  }

  const failed = outcome === 'failed' || r.isError || (exit !== null && exit !== 0)
  if (failed) {
    return {
      kind: 'failed',
      label: exit !== null ? `exit ${exit}` : 'error',
      tone: 'error',
      detail:
        ec?.origin === 'real'
          ? 'A real failure: the sandbox ran it (or tried to) and reported an error.'
          : exit !== null
            ? `The command ran and exited ${exit}.`
            : 'The call ran and reported an error.',
      attr: 'error',
    }
  }
  return {
    kind: 'ok',
    label: exit !== null ? `exit ${exit}` : 'ok',
    tone: 'ok',
    detail: exit !== null ? `The command ran and exited ${exit}.` : 'The call ran and succeeded.',
    attr: 'ok',
  }
}

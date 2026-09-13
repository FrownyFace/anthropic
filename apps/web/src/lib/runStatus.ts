/**
 * Run-status presentation that keeps "finished" and "passed" apart.
 *
 * `ok` only means the loop ended and evaluation succeeded (docs/error-taxonomy.md). Whether the
 * agent actually passed is the grader's call, so the colour of an `ok` badge comes from the
 * evaluation: green when every test and check passed, amber when something failed, neutral when
 * no grade exists — pending or unavailable grading is never shown as success.
 */

import type { ErrorClass, EvaluateResponse, EvaluationStatus, RunStatus } from './types'

export type Grade = 'passed' | 'partial' | 'ungraded'

// --------------------------------------------------------------------------- not graded

export interface UngradedNote {
  tone: 'warn' | 'error' | 'neutral'
  /** Always starts with "Not graded". */
  title: string
  body: string
}

/**
 * Why a finished run has no evaluation, from `status`, `evaluation_status` and `error_class` —
 * never from the presence of error text. Null while the run is still in flight or once an
 * evaluation exists. Pending or unavailable grading is never a pass.
 */
export function ungradedNote(t: {
  status: RunStatus
  evaluation: EvaluateResponse | null
  evaluationStatus: EvaluationStatus | null
  evaluationError: string | null
  errorClass: ErrorClass | null
  error: string | null
  score: number | null
}): UngradedNote | null {
  if (t.evaluation) return null
  if (t.status === 'queued' || t.status === 'running') return null
  const why = t.errorClass?.label ?? t.evaluationError ?? null
  const graderFailed = t.evaluationStatus === 'failed' || t.status === 'unevaluated'
  switch (t.status) {
    case 'interrupted':
      return {
        tone: 'error',
        title: 'Not graded — the run was interrupted',
        body: `A real interruption ended the run and no worker resumed it${why ? `: ${why}` : ''}. Grading was ${t.evaluationStatus === 'failed' ? 'attempted but failed' : 'skipped'}; there is no score.`,
      }
    case 'error': {
      const cause = why ?? t.error ?? null
      return {
        tone: 'error',
        title: 'Not graded — the run could not be executed',
        body: `The harness could not run the episode${cause ? `: ${cause}` : ''}. There is no score.`,
      }
    }
    default:
      break
  }
  if (graderFailed) {
    return {
      tone: 'warn',
      title: 'Not graded — the grader failed',
      body: `The agent's loop finished but evaluate() failed${why ? `: ${why}` : ''}. This is the environment's failure, not the agent's; there is no score.`,
    }
  }
  return {
    tone: 'warn',
    title: 'Not graded',
    body:
      t.score !== null
        ? `The record carries a score of ${Math.round(t.score)} but no evaluation detail; the grader's checks are not available for this run.`
        : 'The run finished but no evaluation was recorded — grading is pending or unavailable, not a pass.',
  }
}

// --------------------------------------------------------------------------- grade

/**
 * `score = 60 × tests_pass + 40 × weighted checks` (services/sandbox-env/GRADING.md), so a score
 * of 100 means every test and every check passed and anything less means one of them failed. With
 * the full evaluation at hand the checks themselves decide.
 */
export function gradeOf(evaluation: EvaluateResponse | null | undefined, score?: number | null): Grade {
  if (evaluation) {
    return evaluation.passed && (evaluation.checks ?? []).every((c) => c.ok) ? 'passed' : 'partial'
  }
  if (typeof score === 'number' && Number.isFinite(score)) return score >= 100 ? 'passed' : 'partial'
  return 'ungraded'
}

const BADGE: Record<Exclude<RunStatus, 'ok'>, string> = {
  queued: 'border-border text-muted-foreground',
  running: 'border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300',
  error: 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300',
  truncated: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  // the loop finished but evaluate failed — not the agent's fault (docs/error-taxonomy.md)
  unevaluated: 'border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-300',
  // a real interruption ended the run and no worker resumed it
  interrupted: 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300',
}

const BADGE_OK: Record<Grade, string> = {
  passed: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  partial: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  ungraded: 'border-border bg-muted/40 text-muted-foreground',
}

const DOT: Record<Exclude<RunStatus, 'ok'>, string> = {
  queued: 'bg-muted-foreground/60',
  running: 'bg-sky-500 dark:bg-sky-400 animate-pulse',
  error: 'bg-rose-500 dark:bg-rose-400',
  truncated: 'bg-amber-500 dark:bg-amber-400',
  unevaluated: 'bg-violet-500 dark:bg-violet-400',
  interrupted: 'bg-rose-500 dark:bg-rose-400',
}

const DOT_OK: Record<Grade, string> = {
  passed: 'bg-emerald-500 dark:bg-emerald-400',
  partial: 'bg-amber-500 dark:bg-amber-400',
  ungraded: 'bg-muted-foreground/60',
}

/** Badge classes for a status chip. */
export function statusTone(status: RunStatus, grade: Grade = 'ungraded'): string {
  return status === 'ok' ? BADGE_OK[grade] : BADGE[status]
}

/** Classes for the small status dot in the sidebar. */
export function statusDot(status: RunStatus, grade: Grade = 'ungraded'): string {
  return status === 'ok' ? DOT_OK[grade] : DOT[status]
}

/** What the chip means, for a title / aria-label. */
export function statusTitle(status: RunStatus, grade: Grade = 'ungraded'): string {
  if (status !== 'ok') return status
  switch (grade) {
    case 'passed':
      return 'ok · passed'
    case 'partial':
      return 'ok · graded with failures'
    default:
      return 'ok · not graded'
  }
}

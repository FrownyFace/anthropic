import { useEffect, useRef } from 'react'
import { AlertTriangle, CircleDashed, CornerDownRight, OctagonAlert } from 'lucide-react'

import { AssistantText, LoadingState, ScoreRows, ThinkingTrace, ToolCallChips } from '@/components/bui'
import type { TransportState } from '@/lib/api'
import { isTerminal } from '@/lib/reducer'
import type { Transcript, Turn } from '@/lib/transcript'
import type { FileDiff } from '@/lib/types'

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end" aria-label="Task prompt">
      <div className="max-w-[85%] rounded-2xl rounded-br-md border border-line bg-surface px-4 py-3">
        <div className="mb-1 flex items-center gap-1.5 text-[10px] tracking-wide text-ink-3 uppercase">
          <CornerDownRight className="size-3" aria-hidden /> task
        </div>
        <p className="text-[13.5px] leading-relaxed whitespace-pre-wrap text-ink">{text}</p>
      </div>
    </div>
  )
}

function Callout({
  tone,
  title,
  body,
  slot,
}: {
  tone: 'error' | 'warn' | 'neutral'
  title: string
  body?: string | null
  slot?: string
}) {
  const cls =
    tone === 'error'
      ? 'border-rose-500/30 bg-rose-500/10 text-rose-800 dark:text-rose-200'
      : tone === 'warn'
        ? 'border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-200'
        : 'border-line bg-muted/40 text-ink-2'
  const Icon = tone === 'error' ? OctagonAlert : tone === 'warn' ? AlertTriangle : CircleDashed
  return (
    <div role="status" data-slot={slot} className={`flex items-start gap-2 rounded-xl border px-3 py-2.5 text-[13px] ${cls}`}>
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0">
        <div className="font-medium">{title}</div>
        {body ? <div className="mt-0.5 font-mono text-[11.5px] break-words opacity-80">{body}</div> : null}
      </div>
    </div>
  )
}

function TurnBlock({
  turn,
  isLast,
  live,
  maxSteps,
  diffs,
}: {
  turn: Turn
  isLast: boolean
  live: boolean
  maxSteps: number
  diffs: FileDiff[]
}) {
  const pending = turn.calls.some((c) => c.result === null)
  const active = live && isLast && pending
  const done = !live || !isLast || (!pending && turn.calls.length > 0)
  return (
    <section aria-label={`Step ${turn.step}`} className="scroll-mt-4 space-y-3">
      {turn.texts.map((t, i) => (
        <AssistantText
          key={`${turn.step}-t-${i}`}
          text={t}
          streaming={live && isLast && i === turn.texts.length - 1 && turn.calls.length === 0}
        />
      ))}
      {turn.calls.length > 0 || turn.thinking ? (
        <ThinkingTrace
          step={turn.step}
          maxSteps={maxSteps}
          calls={turn.calls}
          thinking={turn.thinking}
          active={active}
          done={done}
          defaultOpen={isLast}
        >
          <ToolCallChips calls={turn.calls} diffs={diffs} live={live && isLast} />
        </ThinkingTrace>
      ) : null}
    </section>
  )
}

export function TranscriptView({
  transcript,
  diffs,
  maxSteps,
  loading,
  loadingLabel,
  error,
  transport,
  notFound = false,
  footer,
  focusStep = null,
}: {
  transcript: Transcript
  diffs: FileDiff[]
  maxSteps: number
  loading?: boolean
  loadingLabel?: string
  error?: string | null
  transport?: TransportState | null
  /** GET /runs/{id} answered 404: distinct from loading and from other errors. */
  notFound?: boolean
  /** Rendered below the transcript inside the same scroll container (run switcher, composer spacer). */
  footer?: React.ReactNode
  /** Replay tour: scroll this step into view ('start' / 'end' for the prologue / verdict). */
  focusStep?: number | 'start' | 'end' | null
}) {
  const ref = useRef<HTMLDivElement | null>(null)
  const stick = useRef(true)
  const live = transcript.live

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const onScroll = () => {
      stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 96
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  const turnCount = transcript.turns.length
  const callCount = transcript.turns.reduce((n, t) => n + t.calls.length, 0)
  useEffect(() => {
    const el = ref.current
    if (!el || !live || !stick.current) return
    el.scrollTop = el.scrollHeight
  }, [turnCount, callCount, live, transcript.evaluation])

  useEffect(() => {
    const el = ref.current
    if (!el || focusStep === null || focusStep === undefined) return
    if (focusStep === 'start') {
      el.scrollTo({ top: 0, behavior: 'smooth' })
      return
    }
    if (focusStep === 'end') {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
      return
    }
    const target = el.querySelector<HTMLElement>(`section[aria-label="Step ${focusStep}"]`)
    if (target) {
      stick.current = false
      target.scrollIntoView({ block: 'start', behavior: 'smooth' })
    }
  }, [focusStep, turnCount])

  const last = transcript.turns[transcript.turns.length - 1]
  const waitingOnModel =
    live && (!last || (last.calls.length > 0 && last.calls.every((c) => c.result !== null)))

  return (
    <div ref={ref} className="min-h-0 flex-1 overflow-y-auto" tabIndex={0} aria-label="Transcript">
      <div className="mx-auto w-full max-w-3xl space-y-6 px-4 py-6 md:px-6">
        {loading ? (
          <LoadingState label={loadingLabel ?? 'Loading…'} />
        ) : null}

        {notFound ? (
          <Callout
            tone="warn"
            slot="not-found"
            title="Run not found"
            body="Could not load this run: the harness has no run with this id for this browser identity — it may belong to another browser, or the id is wrong."
          />
        ) : error && turnCount === 0 && !loading ? (
          <Callout tone="warn" title="Could not load this run" body={error} />
        ) : null}

        {transcript.taskPrompt ? <UserBubble text={transcript.taskPrompt} /> : null}

        {transcript.turns.map((t, i) => (
          <TurnBlock
            key={t.step}
            turn={t}
            isLast={i === transcript.turns.length - 1}
            live={live}
            maxSteps={maxSteps}
            diffs={diffs}
          />
        ))}

        {live && turnCount === 0 && !loading && !error && !notFound ? (
          <LoadingState
            label={transcript.status === 'queued' ? 'Queued — provisioning the sandbox…' : 'Provisioning the sandbox…'}
            sublabel="creating an isolated sandbox, copying the fixture repo, applying the fault plan"
          />
        ) : null}
        {waitingOnModel && turnCount > 0 ? (
          <LoadingState label="Waiting on the model…" sublabel={transcript.model ?? undefined} />
        ) : null}

        {transcript.evaluation ? <ScoreRows evaluation={transcript.evaluation} /> : null}

        {/* Pending or unavailable grading is never a pass: say so when a finished run has no evaluation. */}
        {!transcript.evaluation && !live && isTerminal(transcript.status) && !notFound ? (
          <Callout
            tone={transcript.status === 'ok' || transcript.status === 'truncated' ? 'warn' : 'neutral'}
            slot="not-graded"
            title="Not graded"
            body={
              transcript.status === 'ok' || transcript.status === 'truncated'
                ? transcript.score !== null
                  ? `The record carries a score of ${Math.round(transcript.score)} but no evaluation detail; the grader's checks are not available for this run.`
                  : 'The run finished but no evaluation was recorded — grading is pending or unavailable, not a pass.'
                : 'No evaluation exists for this run.'
            }
          />
        ) : null}

        {transcript.status === 'error' ? (
          <Callout tone="error" title="The run ended with an error" body={transcript.error ?? null} />
        ) : transcript.error && !transcript.live ? (
          <Callout
            tone="warn"
            title={transcript.evaluation ? 'The run reported an error' : 'The run finished but could not be graded'}
            body={transcript.error}
          />
        ) : null}
        {transcript.status === 'truncated' ? (
          <Callout
            tone="warn"
            title={`Step budget exhausted (${maxSteps} steps)`}
            body="The agent did not submit in time; whatever it left on disk was still graded."
          />
        ) : null}
        {transport?.mode === 'poll' ? (
          <p className="text-center text-[11px] text-muted-foreground">
            live stream unavailable — polling the run record every 1.5 s
          </p>
        ) : null}
        {footer}
      </div>
    </div>
  )
}

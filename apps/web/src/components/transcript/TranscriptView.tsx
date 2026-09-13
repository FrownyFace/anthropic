import { useEffect, useRef, type ReactNode } from 'react'
import { AlertTriangle, CircleDashed, CornerDownRight, OctagonAlert, Zap } from 'lucide-react'

import { AssistantText, LoadingState, ScoreRows, ThinkingTrace, ToolCallChips, toolCallDomId } from '@/components/bui'
import type { TransportState } from '@/lib/api'
import { interruptionNote } from '@/lib/interruptions'
import { ungradedNote } from '@/lib/runStatus'
import type { Transcript, Turn } from '@/lib/transcript'
import type { FileDiff, Interruption, RunResumedData } from '@/lib/types'

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

const CALLOUT_TONE = {
  error: 'border-rose-500/30 bg-rose-500/10 text-rose-800 dark:text-rose-200',
  warn: 'border-amber-500/30 bg-amber-500/10 text-amber-800 dark:text-amber-200',
  neutral: 'border-line bg-muted/40 text-ink-2',
} as const

function Callout({
  tone,
  title,
  body,
  slot,
  icon,
  children,
  attrs,
}: {
  tone: 'error' | 'warn' | 'neutral'
  title: string
  body?: string | null
  slot?: string
  icon?: typeof OctagonAlert
  children?: ReactNode
  attrs?: Record<string, string | number | boolean | undefined>
}) {
  const Icon = icon ?? (tone === 'error' ? OctagonAlert : tone === 'warn' ? AlertTriangle : CircleDashed)
  return (
    <div
      role="status"
      data-slot={slot}
      {...attrs}
      className={`flex items-start gap-2 rounded-xl border px-3 py-2.5 text-[13px] ${CALLOUT_TONE[tone]}`}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0">
        <div className="font-medium">{title}</div>
        {body ? <div className="mt-0.5 font-mono text-[11.5px] break-words opacity-80">{body}</div> : null}
        {children}
      </div>
    </div>
  )
}

/** Scroll the dangling call's chip into view (or its step, if the chips are collapsed). */
function revealCall(toolUseId: string, step: number | null | undefined) {
  const chip = document.getElementById(toolCallDomId(toolUseId))
  const visible = chip && chip.getClientRects().length > 0
  const target =
    (visible ? chip : null) ??
    (typeof step === 'number' ? document.querySelector<HTMLElement>(`section[aria-label="Step ${step}"]`) : null)
  if (target && typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'center', behavior: 'smooth' })
  if (visible) chip.querySelector<HTMLElement>('button')?.focus()
}

/**
 * A real interruption at this step (docs/error-taxonomy.md): who failed, whether it was planned
 * chaos, which worker resumed and from where, and that the in-flight call's outcome is unknown
 * until the ledger says otherwise. Everything comes from the `interruption` / `run.resumed`
 * events (lib/interruptions.ts).
 */
function InterruptionCallout({ it, resumes }: { it: Interruption; resumes: RunResumedData[] }) {
  const note = interruptionNote(it, resumes)
  return (
    <Callout
      tone="error"
      slot="interruption"
      icon={Zap}
      title={note.title}
      body={note.body}
      attrs={{
        'data-layer': it.layer,
        'data-code': it.code,
        'data-planned': it.planned,
        'data-resumed': note.resumed,
        'data-worker-generation': note.workerGeneration ?? undefined,
        'data-tool-use-id': note.toolUseId ?? undefined,
      }}
    >
      {note.toolUseId && note.callLabel ? (
        <button
          type="button"
          onClick={() => revealCall(note.toolUseId!, it.step)}
          className="mt-1 font-mono text-[11.5px] underline decoration-dotted underline-offset-2 hover:decoration-solid"
          title={note.toolUseId}
        >
          in-flight call: {note.callLabel}
        </button>
      ) : null}
    </Callout>
  )
}

function TurnBlock({
  turn,
  isLast,
  live,
  maxSteps,
  diffs,
  interruptions,
  resumes,
}: {
  turn: Turn
  isLast: boolean
  live: boolean
  maxSteps: number
  diffs: FileDiff[]
  interruptions: Interruption[]
  resumes: RunResumedData[]
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
      {interruptions.map((it, i) => (
        <InterruptionCallout key={it.tool_use_id ?? `${it.at}-${i}`} it={it} resumes={resumes} />
      ))}
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

  // Interruptions render inline at their step; any without a matching turn (no step, or a step
  // that produced no turn) are listed after the transcript so none is silently dropped.
  const hasTurn = (step: number | null | undefined) => typeof step === 'number' && transcript.turns.some((t) => t.step === step)
  const orphanInterruptions = transcript.interruptions.filter((it) => !hasTurn(it.step))

  const ungraded = notFound || live ? null : ungradedNote(transcript)

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
            interruptions={transcript.interruptions.filter((it) => it.step === t.step)}
            resumes={transcript.resumes}
          />
        ))}
        {orphanInterruptions.map((it, i) => (
          <InterruptionCallout key={it.tool_use_id ?? `${it.at}-${i}`} it={it} resumes={transcript.resumes} />
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

        {/* Pending or unavailable grading is never a pass: say why a finished run has no evaluation,
            from status / evaluation_status / error_class — never from the presence of error text. */}
        {ungraded ? (
          <Callout
            tone={ungraded.tone}
            slot="not-graded"
            title={ungraded.title}
            body={ungraded.body}
            attrs={{
              'data-status': transcript.status,
              'data-evaluation-status': transcript.evaluationStatus ?? undefined,
              'data-error-code': transcript.errorClass?.code ?? undefined,
            }}
          />
        ) : null}

        {transcript.status === 'error' ? (
          <Callout
            tone="error"
            slot="run-error"
            title="The run ended with an error"
            body={transcript.errorClass?.label ?? transcript.error ?? null}
          />
        ) : transcript.evaluation && transcript.error && !transcript.live ? (
          <Callout tone="warn" slot="run-error" title="The run reported an error" body={transcript.error} />
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

import { useEffect, useState } from 'react'
import { Database, Radio, RefreshCw, ServerOff, Zap } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { TransportState } from '@/lib/api'
import { fmtDuration, fmtTokens } from '@/lib/format'
import { sandboxChipText, sandboxChipTitle, sandboxLoss, workerChip } from '@/lib/interruptions'
import { elapsedMs, faultCount, recoveredCount, type ViewState } from '@/lib/reducer'
import { gradeOf, statusTitle, statusTone } from '@/lib/runStatus'

/** Where the transcript on screen is coming from. Shown so a reviewer can see the persistence path. */
export type TranscriptSource = 'sse' | 'poll' | 'sqlite' | 'record'

const SOURCE_LABEL: Record<TranscriptSource, { label: string; tip: string; icon: typeof Radio }> = {
  sse: { label: 'live · sse', tip: 'Tailing GET /runs/{id}/events; the harness rotates the stream every ~110 s and we resume with Last-Event-ID.', icon: Radio },
  poll: { label: 'live · poll', tip: 'SSE failed twice in a row — polling GET /runs/{id} every 1.5 s instead.', icon: Radio },
  sqlite: { label: 'sqlite', tip: 'Transcript loaded from GET /conversations/{id}: the messages/blocks projection stored in SQLite on the persistent volume.', icon: Database },
  record: { label: 'record', tip: 'Loaded from GET /runs/{id} (the persisted run record).', icon: Database },
}

const REAL_CHIP = 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300'
const RESUMED_CHIP = 'border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300'

function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="hidden flex-col leading-tight lg:flex" title={title}>
      <span className="text-[10px] tracking-wide text-muted-foreground uppercase">{label}</span>
      <span className="font-mono text-[11px] tabular-nums">{value}</span>
    </div>
  )
}

export function RunStatusStrip({
  state,
  transport,
  source,
}: {
  state: ViewState | null
  transport: TransportState | null
  source: TranscriptSource | null
}) {
  const live = !!state && (state.status === 'running' || state.status === 'queued')
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!live) return
    const t = setInterval(() => setTick((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [live])

  if (!state) return null
  const faults = faultCount(state)
  const recovered = recoveredCount(state)
  const grade = gradeOf(state.evaluation)
  const checks = state.evaluation?.checks ?? []
  const okChecks = checks.filter((c) => c.ok).length
  const src = source ? SOURCE_LABEL[source] : null
  const SrcIcon = src?.icon ?? Radio
  // Harness-level failure state, from structured events only (lib/interruptions.ts).
  const worker = workerChip(state)
  const loss = sandboxLoss(state)

  return (
    <div className="flex items-center gap-2.5">
      <Badge
        variant="outline"
        className={`font-mono ${statusTone(state.status, grade)}`}
        title={statusTitle(state.status, grade)}
        data-grade={state.status === 'ok' ? grade : undefined}
      >
        {state.status}
      </Badge>
      {worker ? (
        <Badge
          variant="outline"
          className={`font-mono ${worker.tone === 'resumed' ? RESUMED_CHIP : REAL_CHIP}`}
          title={worker.title}
          data-slot="worker-chip"
          data-tone={worker.tone}
          data-worker-generation={state.workerGeneration}
        >
          {worker.tone === 'resumed' ? <RefreshCw aria-hidden /> : <Zap aria-hidden />} {worker.text}
        </Badge>
      ) : null}
      {loss ? (
        <Badge
          variant="outline"
          className={`font-mono ${REAL_CHIP}`}
          title={sandboxChipTitle(loss)}
          data-slot="sandbox-chip"
          data-sandbox-status={loss.status}
        >
          <ServerOff aria-hidden /> {sandboxChipText(loss)}
        </Badge>
      ) : null}
      {src ? (
        <Tooltip>
          <TooltipTrigger
            render={
              <Badge variant="outline" className="font-mono text-muted-foreground">
                <SrcIcon aria-hidden /> {src.label}
              </Badge>
            }
          />
          <TooltipContent>{src.tip}{transport?.detail ? ` (${transport.detail})` : ''}</TooltipContent>
        </Tooltip>
      ) : null}
      <Stat label="step" value={`${state.step}/${state.maxSteps}`} />
      <Stat
        label="tokens"
        value={`${fmtTokens(state.usage.input_tokens)} in · ${fmtTokens(state.usage.output_tokens)} out`}
        title={`${state.usage.input_tokens} input / ${state.usage.output_tokens} output`}
      />
      <Stat label="elapsed" value={fmtDuration(elapsedMs(state))} />
      <Stat label="faults" value={`${faults}`} title="Faults the environment fired on this run's tool calls" />
      {state.evaluation ? (
        <Stat
          label="checks"
          value={`${okChecks}/${checks.length} passed`}
          title="The grader's checks (authoritative)"
        />
      ) : (
        <Stat
          label="read-back"
          value={`${recovered}/${faults} seen`}
          title="Live hint from the call order (a read of the faulted path before a later success); heuristic — the grader's checks are authoritative"
        />
      )}
      {state.evaluation ? <Stat label="score" value={`${Math.round(state.evaluation.score)}`} /> : null}
    </div>
  )
}

import { useEffect, useState } from 'react'
import { Database, Radio } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { TransportState } from '@/lib/api'
import { fmtDuration, fmtTokens } from '@/lib/format'
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

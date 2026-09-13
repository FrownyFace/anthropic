/**
 * ThinkingTrace — adapted from Beautiful UI `ThinkingState` ("Steps" variant; MIT, © 2026 Shane
 * Levine).
 *
 * A collapsible per-step trace. The header shimmers while the step is in flight and settles when
 * it is done; the body lists the step's tool calls down a thin timeline rail (a spinner on the
 * call still running, a check or cross once it has a result), an optional thinking paragraph
 * above, and whatever the parent passes as children (ToolCallChips) below.
 *
 * The scripted stage timers are gone: `active` / `done` come from the run reducer.
 */

import { useId, useState, type ReactNode } from 'react'
import { Ban, Check, ChevronDown, CircleQuestionMark, Sparkles, Terminal, X } from 'lucide-react'

import { ErrorOriginBadge, FaultFiredBadge, RecoveredBadge } from '@/components/FaultBadge'
import { callStatus } from '@/lib/callStatus'
import { oneLine } from '@/lib/format'
import type { ToolCallView } from '@/lib/reducer'
import { cn } from '@/lib/utils'

import { summariseCall, TOOL_ICON, toolLabel } from './toolMeta'

export interface ThinkingTraceProps {
  step: number
  maxSteps?: number
  calls: ToolCallView[]
  /** Prose the agent produced in this step, shown above the call rows. */
  thinking?: string | null
  /** The step is in flight: shimmer header, spinner on the call without a result. */
  active: boolean
  /** The step has settled. */
  done: boolean
  /** Initial open state; defaults to open while `active`. Users can toggle regardless. */
  defaultOpen?: boolean
  /** Rendered inside the expanded body, under the rows. */
  children?: ReactNode
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

function statusText(active: boolean, done: boolean, n: number): string {
  if (active) return n === 0 ? 'thinking' : `running ${plural(n, 'tool call')}`
  if (done) return n === 0 ? 'no tool calls' : plural(n, 'tool call')
  return 'pending'
}

/** Rail glyph from structured status (`lib/callStatus.ts`); an unknown outcome is not a cross. */
function Glyph({ call, active }: { call: ToolCallView; active: boolean }) {
  const st = callStatus(call, active)
  switch (st.kind) {
    case 'running':
      return (
        <span
          className="bui-spin size-3 shrink-0 rounded-full border-[1.5px] border-line-strong border-t-ink-2"
          aria-label="running"
          role="img"
        />
      )
    case 'pending':
      return <span className="size-3 shrink-0 rounded-full border-[1.5px] border-line" aria-label="pending" role="img" />
    case 'failed':
      return (
        <span role="img" aria-label="error" title={st.detail} className="flex shrink-0">
          <X className="size-3.5 text-rose-700 dark:text-rose-300" strokeWidth={2.5} aria-hidden />
        </span>
      )
    case 'unknown':
      return (
        <span role="img" aria-label="unknown" title={st.detail} className="flex shrink-0">
          <CircleQuestionMark className="size-3.5 text-amber-700 dark:text-amber-300" strokeWidth={2.5} aria-hidden />
        </span>
      )
    case 'not_executed':
      return (
        <span role="img" aria-label="not executed" title={st.detail} className="flex shrink-0">
          <Ban className="size-3.5 text-amber-700 dark:text-amber-300" strokeWidth={2.5} aria-hidden />
        </span>
      )
    default:
      return (
        <span role="img" aria-label="ok" title={st.detail} className="flex shrink-0">
          <Check className="size-3.5 text-ink-3" strokeWidth={2.5} aria-hidden />
        </span>
      )
  }
}

export function ThinkingTrace({
  step,
  maxSteps,
  calls,
  thinking,
  active,
  done,
  defaultOpen,
  children,
}: ThinkingTraceProps) {
  const [manual, setManual] = useState<boolean | null>(null)
  const expanded = manual ?? defaultOpen ?? active
  const panelId = useId()

  const n = calls.length
  const faults = calls.filter((c) => c.fault).length
  const recovered = calls.filter((c) => c.fault && c.recovered).length
  const kinds = calls.map((c) => callStatus(c).kind)
  const errors = kinds.filter((k) => k === 'failed').length
  const unknown = kinds.filter((k) => k === 'unknown').length
  const title = maxSteps ? `Step ${step} of ${maxSteps}` : `Step ${step}`

  return (
    <div className="flex w-full flex-col" data-slot="thinking-trace" data-step={step}>
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={() => setManual(!expanded)}
        className="-mx-1.5 flex w-fit max-w-full items-center gap-2 rounded-md px-1.5 py-1 text-left transition-colors duration-100 hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
      >
        <Sparkles
          className={cn('size-4 shrink-0 transition-colors duration-200', active ? 'text-ink-2' : 'text-ink-3')}
          fill="currentColor"
          strokeWidth={0}
          aria-hidden
        />
        <span className="text-[13px] font-medium whitespace-nowrap text-ink">{title}</span>
        <span role="status" className="contents">
          {active ? (
            <span className="bui-shimmer text-[13px] font-medium whitespace-nowrap text-ink-2">
              {statusText(active, done, n)}
            </span>
          ) : (
            <span className="bui-fade-in text-[13px] font-medium whitespace-nowrap text-ink-2">
              {statusText(active, done, n)}
            </span>
          )}
        </span>
        {faults > 0 ? (
          <span className="font-mono text-[11px] text-amber-700 dark:text-amber-300 tabular-nums">{plural(faults, 'fault')}</span>
        ) : null}
        {recovered > 0 ? (
          <span
            className="font-mono text-[11px] text-emerald-700 dark:text-emerald-300 tabular-nums"
            title="Heuristic from the call order; the grader's checks are authoritative."
          >
            {recovered} read-back seen
          </span>
        ) : null}
        {unknown > 0 ? (
          <span className="font-mono text-[11px] text-amber-700 dark:text-amber-300 tabular-nums">{unknown} unknown</span>
        ) : null}
        {errors > 0 ? (
          <span className="font-mono text-[11px] text-rose-700 dark:text-rose-300 tabular-nums">{plural(errors, 'error')}</span>
        ) : null}
        <ChevronDown
          className={cn('size-3.5 shrink-0 text-ink-3 transition-transform duration-300', expanded && 'rotate-180')}
          aria-hidden
        />
      </button>

      <div
        id={panelId}
        aria-hidden={!expanded}
        inert={!expanded}
        className={cn(
          'grid motion-safe:transition-[grid-template-rows,opacity] motion-safe:duration-400 motion-safe:ease-[cubic-bezier(0.23,1,0.32,1)]',
          expanded ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0',
        )}
      >
        <div className="min-h-0 overflow-hidden">
          <div className="relative mt-1 ml-[7px] pl-4">
            <span aria-hidden className="absolute top-0 bottom-1 left-0 w-px bg-line" />
            <div className="flex flex-col gap-1 py-1">
              {thinking ? (
                <p className="px-1.5 py-0.5 text-[12.5px] leading-relaxed whitespace-pre-wrap text-ink-2">
                  {thinking}
                </p>
              ) : null}

              {calls.map((call, i) => {
                const Icon = TOOL_ICON[call.tool] ?? Terminal
                return (
                  <div
                    key={call.toolUseId}
                    className="bui-fade-up flex min-h-7 w-full min-w-0 flex-wrap items-center gap-x-2 gap-y-1 rounded-md px-1.5 py-0.5"
                    style={{ animationDelay: `${active ? 0 : Math.min(i, 8) * 60}ms` }}
                    data-slot="trace-row"
                  >
                    <Glyph call={call} active={active} />
                    <Icon className="size-3.5 shrink-0 text-ink-3" aria-hidden />
                    <span className="shrink-0 text-[12.5px] font-medium text-ink">{toolLabel(call.tool)}</span>
                    <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-ink-3">
                      {oneLine(summariseCall(call), 140)}
                    </span>
                    {call.fault ? <FaultFiredBadge fault={call.fault} /> : null}
                    {call.result?.errorClass && (call.result.errorClass.origin === 'real' || !call.fault) ? (
                      <ErrorOriginBadge errorClass={call.result.errorClass} />
                    ) : null}
                    {call.recovered ? <RecoveredBadge /> : null}
                  </div>
                )
              })}

              {active && n === 0 ? (
                <div className="flex min-h-7 items-center gap-2 px-1.5 text-[12.5px] text-ink-3">
                  <span className="bui-spin size-3 shrink-0 rounded-full border-[1.5px] border-line-strong border-t-ink-2" />
                  waiting for the model…
                </div>
              ) : null}

              {children ? <div className="mt-1 min-w-0">{children}</div> : null}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

/**
 * ToolCallChips — adapted from Beautiful UI `ToolChips` (MIT, © 2026 Shane Levine).
 *
 * One row per tool call: tool icon that swaps to a chevron on hover, a label, the command / path
 * in a mono chip, then status. Expanding a row reveals the call input, every result part the
 * harness returned (error + code, stdout, stderr, content, entries, bytes written, truncation),
 * and — for a mutating call whose path has a `FileDiff` — the unified diff.
 *
 * The original's hover-portal diff preview was replaced by inline expansion so it behaves inside
 * a scrolling column; the demo timers are gone (rows appear as the reducer adds them).
 */

import { useId, useState, type ReactNode } from 'react'
import { Ban, Check, ChevronDown, CircleQuestionMark, LoaderCircle, Terminal, X } from 'lucide-react'

import { ErrorOriginBadge, FaultFiredBadge, RecoveredBadge } from '@/components/FaultBadge'
import { callStatus, type CallStatusTone } from '@/lib/callStatus'
import { fmtDuration, oneLine } from '@/lib/format'
import type { ToolCallView } from '@/lib/reducer'
import type { FileDiff } from '@/lib/types'
import { cn } from '@/lib/utils'

import { CodeBlock, parseUnifiedDiff } from './CodeBlock'
import { diffForCall, resultParts, summariseCall, TOOL_ICON, toolLabel } from './toolMeta'

export interface ToolCallChipsProps {
  calls: ToolCallView[]
  /** Workspace diffs; a mutating call whose path matches gets its diff inline. */
  diffs?: FileDiff[]
  /** A call without a result renders as running (instead of "no result"). */
  live?: boolean
}

const CLIP = 4000
function clip(s: string): string {
  return s.length > CLIP ? `${s.slice(0, CLIP)}\n…[truncated in view]` : s
}

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-0.5 font-mono text-[10px] tracking-wide text-ink-3 uppercase">{label}</div>
      {children}
    </div>
  )
}

function Mono({ children, tone = 'plain' }: { children: ReactNode; tone?: 'error' | 'plain' }) {
  return (
    <pre
      className={cn(
        'overflow-x-auto rounded-md px-2 py-1.5 font-mono text-[11.5px] leading-relaxed break-words whitespace-pre-wrap',
        tone === 'error' ? 'bg-rose-500/10 text-rose-700 dark:text-rose-300' : 'bg-muted/40 text-ink-2',
      )}
    >
      {children}
    </pre>
  )
}

const PILL_TONE: Record<CallStatusTone, string> = {
  muted: 'text-ink-3 ring-line',
  ok: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 ring-emerald-500/30',
  error: 'bg-rose-500/10 text-rose-700 dark:text-rose-300 ring-rose-500/30',
  // outcome unknown / not executed: neither a pass nor a failure
  unknown: 'bg-amber-500/10 text-amber-700 dark:text-amber-300 ring-amber-500/30',
}

/**
 * Status from structured fields only (`lib/callStatus.ts`): a lost acknowledgement is "no ack",
 * not a red cross, because the write may well have landed.
 */
function StatusPill({ call, live }: { call: ToolCallView; live: boolean }) {
  const st = callStatus(call, live)
  const base = 'inline-flex h-5 shrink-0 items-center gap-1 rounded-full px-2 font-mono text-[11px] ring-1 ring-inset'
  let icon: ReactNode = null
  switch (st.kind) {
    case 'running':
      icon = <LoaderCircle className="bui-spin size-3" aria-hidden />
      break
    case 'ok':
      icon = <Check className="size-3" aria-hidden />
      break
    case 'failed':
      icon = <X className="size-3" aria-hidden />
      break
    case 'unknown':
      icon = <CircleQuestionMark className="size-3" aria-hidden />
      break
    case 'not_executed':
      icon = <Ban className="size-3" aria-hidden />
      break
    default:
      break
  }
  return (
    <span
      className={cn(base, st.kind === 'running' ? 'bg-muted/50 text-ink-2 ring-line' : PILL_TONE[st.tone])}
      data-status={st.attr}
      title={st.detail}
    >
      {icon}
      {st.label}
    </span>
  )
}

function InputBlock({ call }: { call: ToolCallView }) {
  if (call.command) return <Mono>$ {call.command}</Mono>
  if (call.tool === 'write_file' && typeof call.input.content === 'string') {
    const { content, ...rest } = call.input
    return (
      <div className="space-y-1.5">
        <CodeBlock code={content} filename={call.path ?? 'content'} maxHeight={240} />
        {Object.keys(rest).length > 0 ? <Mono>{JSON.stringify(rest)}</Mono> : null}
      </div>
    )
  }
  return <Mono>{JSON.stringify(call.input, null, 2)}</Mono>
}

function ResultBlock({ call }: { call: ToolCallView }) {
  const result = call.result!
  const parts = resultParts(result, call.path)
  return (
    <>
      {parts.map((p) => (
        <Section key={p.label} label={p.label}>
          {p.code ? (
            <CodeBlock code={clip(p.body)} filename={p.code.filename ?? undefined} maxHeight={280} />
          ) : (
            <Mono tone={p.tone}>{clip(p.body)}</Mono>
          )}
        </Section>
      ))}
      {result.truncated ? (
        <p className="text-[11px] text-ink-3">Output was truncated by the environment.</p>
      ) : null}
    </>
  )
}

function Row({
  call,
  diff,
  live,
  index,
}: {
  call: ToolCallView
  diff: FileDiff | null
  live: boolean
  index: number
}) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const Icon = TOOL_ICON[call.tool] ?? Terminal
  const result = call.result
  const summary = summariseCall(call)
  const counts = diff ? parseUnifiedDiff(diff.unified) : null
  const added = counts?.filter((l) => l.kind === 'add').length ?? 0
  const removed = counts?.filter((l) => l.kind === 'del').length ?? 0

  return (
    <div
      className="bui-fade-up"
      style={{ animationDelay: `${live ? 0 : Math.min(index, 8) * 50}ms` }}
      data-slot="tool-call"
      data-tool={call.tool}
    >
      <div className="flex min-h-7 w-full min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((v) => !v)}
          className="group/row flex h-7 min-w-0 flex-1 basis-48 items-center gap-2 rounded-md px-1 text-left transition-colors duration-100 hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
        >
          <span className="relative flex size-4 shrink-0 items-center justify-center text-ink-3">
            <Icon
              className={cn(
                'size-[13px] transition-opacity duration-100 group-hover/row:opacity-0',
                open && 'opacity-0',
              )}
              aria-hidden
            />
            <ChevronDown
              className={cn(
                'absolute size-3 transition-[opacity,transform] duration-150 group-hover/row:opacity-100',
                open ? 'rotate-0 opacity-100' : '-rotate-90 opacity-0',
              )}
              aria-hidden
            />
          </span>
          <span className="shrink-0 text-[12.5px] font-medium text-ink">{toolLabel(call.tool)}</span>
          <span className="inline-flex h-5.5 min-w-0 flex-1 items-center rounded-md bg-muted/50 px-1.5 font-mono text-[11.5px] text-ink-2 ring-1 ring-line ring-inset">
            <span className="truncate">{oneLine(summary, 160)}</span>
          </span>
        </button>

        {diff ? (
          <span
            className="shrink-0 font-mono text-[11px] tabular-nums"
            aria-label={`${added} lines added, ${removed} lines removed`}
          >
            <span className="text-emerald-700 dark:text-emerald-300">+{added}</span>{' '}
            <span className="text-rose-700 dark:text-rose-300">−{removed}</span>
          </span>
        ) : null}
        {call.fault ? <FaultFiredBadge fault={call.fault} /> : null}
        {result?.errorClass && (result.errorClass.origin === 'real' || !call.fault) ? (
          <ErrorOriginBadge errorClass={result.errorClass} />
        ) : null}
        {call.recovered ? <RecoveredBadge /> : null}
        <StatusPill call={call} live={live} />
        {result ? (
          <span className="shrink-0 font-mono text-[11px] text-ink-3 tabular-nums">
            {fmtDuration(result.durationMs)}
          </span>
        ) : null}
      </div>

      <div
        id={panelId}
        aria-hidden={!open}
        inert={!open}
        className={cn(
          'grid motion-safe:transition-[grid-template-rows,opacity] motion-safe:duration-300 motion-safe:ease-[cubic-bezier(0.23,1,0.32,1)]',
          open ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0',
        )}
      >
        <div className="min-h-0 overflow-hidden">
          <div className="mt-0.5 mb-1 ml-3 flex flex-col gap-2 border-l border-line py-1 pl-3.5">
            <Section label="input">
              <InputBlock call={call} />
            </Section>
            {result ? (
              <ResultBlock call={call} />
            ) : (
              <p className="text-[11.5px] text-ink-3">{live ? 'Waiting for the result…' : 'No result recorded.'}</p>
            )}
            {diff ? (
              <Section label="diff">
                <CodeBlock diff={diff.unified} filename={diff.path} maxHeight={320} />
              </Section>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  )
}

export function ToolCallChips({ calls, diffs = [], live = false }: ToolCallChipsProps) {
  if (calls.length === 0) {
    return <p className="px-1 text-[12px] text-ink-3">No tool calls.</p>
  }
  return (
    <div className="flex w-full min-w-0 flex-col gap-1" data-slot="tool-call-chips">
      {calls.map((c, i) => (
        <Row key={c.toolUseId} call={c} diff={diffForCall(c, diffs)} live={live} index={i} />
      ))}
    </div>
  )
}

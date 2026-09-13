/**
 * ScoreRows — adapted from Beautiful UI `TaskRows` ("Capsules" variant; MIT, © 2026 Shane
 * Levine).
 *
 * A score headline, then one capsule per grading check (round pass/fail badge, id, weight,
 * status pill) that expands to the grader's detail. A final capsule holds the pytest summary
 * and expands to the raw output in a CodeBlock. The demo's staged running/failed sequence was
 * removed — an evaluation is always settled.
 */

import { useId, useState, type ReactNode } from 'react'
import { Check, ChevronDown, X } from 'lucide-react'

import { gradeOf } from '@/lib/runStatus'
import type { Check as CheckResult, EvaluateResponse } from '@/lib/types'
import { cn } from '@/lib/utils'

import { CodeBlock } from './CodeBlock'

export interface ScoreRowsProps {
  evaluation: EvaluateResponse
}

type HeadlineTone = 'ok' | 'warn' | 'fail'

/**
 * The headline is green only for a real pass — `gradeOf`: hidden tests passed AND every recovery
 * check ok (lib/runStatus.ts). Tests passed with a failed check is amber ("graded with
 * failures"); failed tests are red. The score number alone never decides the colour.
 */
function headlineTone(evaluation: EvaluateResponse): HeadlineTone {
  if (gradeOf(evaluation) === 'passed') return 'ok'
  return evaluation.passed ? 'warn' : 'fail'
}

const HEADLINE: Record<HeadlineTone, { number: string; pill: string; word: string }> = {
  ok: { number: 'text-emerald-700 dark:text-emerald-300', pill: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300', word: 'Passed' },
  warn: { number: 'text-amber-700 dark:text-amber-300', pill: 'bg-amber-500/10 text-amber-700 dark:text-amber-300', word: 'Graded with failures' },
  fail: { number: 'text-rose-700 dark:text-rose-300', pill: 'bg-rose-500/10 text-rose-700 dark:text-rose-300', word: 'Failed' },
}

function RoundBadge({ ok }: { ok: boolean }) {
  return (
    <span
      className={cn(
        'bui-pop-in flex size-5.5 shrink-0 items-center justify-center rounded-full text-white',
        ok ? 'bg-emerald-500' : 'bg-rose-500',
      )}
      role="img"
      aria-label={ok ? 'pass' : 'fail'}
    >
      {ok ? <Check className="size-3" strokeWidth={3.5} aria-hidden /> : <X className="size-3" strokeWidth={3.5} aria-hidden />}
    </span>
  )
}

function Pill({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <span
      className={cn(
        'inline-flex h-5.5 shrink-0 items-center rounded-full px-2 text-[11.5px] font-medium',
        ok ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300' : 'bg-rose-500/10 text-rose-700 dark:text-rose-300',
      )}
    >
      {children}
    </span>
  )
}

function Capsule({
  ok,
  label,
  mono = false,
  amount,
  pill,
  index,
  open,
  onToggle,
  children,
}: {
  ok: boolean
  label: string
  mono?: boolean
  amount: string
  pill: string
  index: number
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  const panelId = useId()
  return (
    <div
      className="bui-fade-up self-stretch overflow-hidden border border-line bg-surface transition-[border-radius,background-color] duration-300 hover:bg-muted/30"
      style={{ borderRadius: open ? 14 : 22, animationDelay: `${index * 80}ms` }}
      data-slot="score-row"
      data-ok={ok}
    >
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={onToggle}
        className="flex h-11 w-full items-center gap-2.5 px-2.5 text-left focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none focus-visible:ring-inset"
      >
        <span className="flex size-6 shrink-0 items-center justify-center">
          <RoundBadge ok={ok} />
        </span>
        <span className={cn('min-w-0 flex-1 truncate text-[13px] font-medium text-ink', mono && 'font-mono text-[12.5px]')}>
          {label}
        </span>
        <span className="shrink-0 text-[12px] text-ink-2 tabular-nums">{amount}</span>
        <Pill ok={ok}>{pill}</Pill>
        <span aria-hidden className="-ml-1 flex size-7 shrink-0 items-center justify-center rounded-full text-ink-3">
          <ChevronDown className={cn('size-[15px] transition-transform duration-300', open && 'rotate-180')} />
        </span>
      </button>

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
          <div className="mb-2.5 grid grid-cols-[24px_minmax(0,1fr)] gap-2.5 px-2.5">
            <span aria-hidden className="mx-auto h-full w-px bg-line" />
            <div className="flex min-w-0 flex-col gap-1.5">{children}</div>
          </div>
        </div>
      </div>
    </div>
  )
}

function CheckBody({ check }: { check: CheckResult }) {
  return (
    <div className="bui-fade-up flex items-start justify-between gap-3" style={{ animationDelay: '120ms' }}>
      <span className="min-w-0 text-[12px] leading-relaxed break-words text-ink-2">{check.detail || '—'}</span>
      <span className="shrink-0 font-mono text-[11.5px] text-ink-3 tabular-nums">{check.ok ? 'ok' : 'fail'}</span>
    </div>
  )
}

export function ScoreRows({ evaluation }: ScoreRowsProps) {
  const tests = evaluation.tests ?? { passed: 0, failed: 0, errors: 0, output: '' }
  const checks = evaluation.checks ?? []
  const okChecks = checks.filter((c) => c.ok).length
  const testsOk = tests.failed === 0 && tests.errors === 0
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const toggle = (key: string) => setOpen((cur) => ({ ...cur, [key]: !cur[key] }))
  const grade = gradeOf(evaluation)
  const headline = HEADLINE[headlineTone(evaluation)]

  return (
    <div className="flex w-full flex-col gap-2" data-slot="score-rows" data-grade={grade}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-1">
        <span className={cn('font-mono text-3xl tabular-nums', headline.number)} aria-label="score">
          {Math.round(evaluation.score)}
        </span>
        <span className="text-[12.5px] text-ink-3">/ 100</span>
        <span
          className={cn('inline-flex h-5.5 shrink-0 items-center rounded-full px-2 text-[11.5px] font-medium', headline.pill)}
          data-slot="grade-pill"
          title={
            grade === 'passed'
              ? 'Hidden tests passed and every recovery check is ok.'
              : evaluation.passed
                ? 'Hidden tests passed but a recovery check failed.'
                : 'Hidden tests failed.'
          }
        >
          {headline.word}
        </span>
        <span className="font-mono text-[11.5px] text-ink-3 tabular-nums">
          {tests.passed} passed · {tests.failed} failed · {tests.errors} errors
        </span>
        <span className="font-mono text-[11.5px] text-ink-3 tabular-nums">
          checks {okChecks}/{checks.length}
        </span>
      </div>

      {checks.map((c, i) => (
        <Capsule
          key={c.id}
          ok={c.ok}
          label={c.id}
          mono
          amount={`weight ${c.weight}`}
          pill={c.ok ? 'Passed' : 'Failed'}
          index={i}
          open={!!open[`check:${c.id}`]}
          onToggle={() => toggle(`check:${c.id}`)}
        >
          <CheckBody check={c} />
        </Capsule>
      ))}

      <Capsule
        ok={testsOk}
        label="pytest"
        amount={`${tests.passed} passed · ${tests.failed} failed · ${tests.errors} errors`}
        pill={testsOk ? 'Passed' : 'Failed'}
        index={checks.length}
        open={!!open.tests}
        onToggle={() => toggle('tests')}
      >
        <CodeBlock
          code={tests.output || '(no output)'}
          filename="python -m pytest -q"
          language="log"
          maxHeight={240}
        />
      </Capsule>

      <p className="px-1 text-[11px] text-ink-3">
        score = 60 × tests_pass + 40 × weighted recovery checks (services/sandbox-env/GRADING.md)
      </p>
    </div>
  )
}

/**
 * CodeBlock — adapted from Beautiful UI `CodeBlock` (MIT, © 2026 Shane Levine).
 *
 * Line-numbered listing, or a unified diff parsed from text with +/- colouring. The original
 * took pre-split rows and demo data; this one takes raw strings. No network access.
 */

import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { Check, Copy, FileCode } from 'lucide-react'

import { cn } from '@/lib/utils'

export interface CodeBlockProps {
  /** Plain listing. Ignored when `diff` is given. */
  code?: string
  /** Unified diff text (`--- a/x`, `+++ b/x`, `@@ -1,3 +1,4 @@`, then ` `, `+`, `-` lines). */
  diff?: string
  filename?: string
  /** Hint only: `text`/`plain`/`log` disable the light syntax tinting. */
  language?: string
  /** Scroll inside the block past this many pixels. */
  maxHeight?: number
  className?: string
}

export type DiffLineKind = 'ctx' | 'add' | 'del' | 'hunk' | 'meta' | 'note'

export interface DiffLine {
  kind: DiffLineKind
  /** Line number in the old file (deletions and context). */
  old: number | null
  /** Line number in the new file (additions and context). */
  cur: number | null
  text: string
}

/** Parse unified-diff text into numbered rows. Tolerant: unknown lines become context. */
export function parseUnifiedDiff(unified: string): DiffLine[] {
  const lines = unified.replace(/\n$/, '').split('\n')
  const out: DiffLine[] = []
  let old = 0
  let cur = 0
  let inHunk = false
  for (const raw of lines) {
    if (!inHunk && (raw.startsWith('+++') || raw.startsWith('---'))) {
      out.push({ kind: 'meta', old: null, cur: null, text: raw })
      continue
    }
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw)
    if (hunk) {
      inHunk = true
      old = Number(hunk[1])
      cur = Number(hunk[2])
      out.push({ kind: 'hunk', old: null, cur: null, text: raw })
      continue
    }
    if (raw.startsWith('\\')) {
      out.push({ kind: 'note', old: null, cur: null, text: raw })
      continue
    }
    if (raw.startsWith('+')) {
      out.push({ kind: 'add', old: null, cur: cur++, text: raw.slice(1) })
      continue
    }
    if (raw.startsWith('-')) {
      out.push({ kind: 'del', old: old++, cur: null, text: raw.slice(1) })
      continue
    }
    out.push({ kind: 'ctx', old: old++, cur: cur++, text: raw.startsWith(' ') ? raw.slice(1) : raw })
  }
  return out
}

const KEYWORDS = new Set([
  // js / ts
  'import', 'from', 'export', 'default', 'async', 'function', 'const', 'let', 'var', 'await',
  'return', 'if', 'else', 'for', 'while', 'new', 'throw', 'try', 'catch', 'null', 'true', 'false',
  'undefined',
  // python (the sandbox fixture is a Python package)
  'def', 'class', 'elif', 'in', 'not', 'and', 'or', 'is', 'None', 'True', 'False', 'except',
  'finally', 'with', 'as', 'lambda', 'pass', 'raise', 'yield',
])

const TOKEN =
  /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`[^`]*`|\b\d+(?:\.\d+)?\b|\b(?:import|from|export|default|async|function|const|let|var|await|return|if|else|for|while|new|throw|try|catch|null|true|false|undefined|def|class|elif|in|not|and|or|is|None|True|False|except|finally|with|as|lambda|pass|raise|yield)\b|[A-Za-z_$][\w$]*(?=\s*\())/g

const PLAIN = new Set(['text', 'plain', 'log', 'txt'])

function highlight(text: string, enabled: boolean): ReactNode {
  if (!enabled || text.length === 0) return text
  const nodes: ReactNode[] = []
  let last = 0
  let k = 0
  for (const m of text.matchAll(TOKEN)) {
    const idx = m.index ?? 0
    const t = m[0]
    if (idx > last) nodes.push(text.slice(last, idx))
    let className: string
    if (/^["'`]/.test(t) || /^\d/.test(t)) className = 'text-amber-700 dark:text-amber-300'
    else if (KEYWORDS.has(t)) className = 'text-sky-700 dark:text-sky-300'
    else className = 'font-medium text-ink'
    nodes.push(
      <span key={k++} className={className}>
        {t}
      </span>,
    )
    last = idx + t.length
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

/** Deleted-line gutter bar: the original's diagonal hatch, drawn in `currentColor`. */
const HATCH =
  'repeating-linear-gradient(45deg, currentColor 0, currentColor 1.5px, transparent 1.5px, transparent 3px)'

const ROW_TONE: Record<DiffLineKind, string> = {
  ctx: '',
  add: 'bg-emerald-500/10',
  del: 'bg-rose-500/10',
  hunk: 'bg-muted/30 text-sky-300/80',
  meta: 'text-ink-3',
  note: 'text-ink-3 italic',
}

const NUM_TONE: Record<DiffLineKind, string> = {
  ctx: 'text-ink-3',
  add: 'text-emerald-700 dark:text-emerald-300',
  del: 'text-rose-700 dark:text-rose-300',
  hunk: 'text-ink-3',
  meta: 'text-ink-3',
  note: 'text-ink-3',
}

export function CodeBlock({ code, diff, filename, language, maxHeight, className }: CodeBlockProps) {
  const [copied, setCopied] = useState(false)
  const isDiff = typeof diff === 'string'
  const raw = isDiff ? diff : (code ?? '')
  const tint = !PLAIN.has((language ?? '').toLowerCase())

  const rows = useMemo(() => (isDiff ? parseUnifiedDiff(diff) : null), [isDiff, diff])
  const lines = useMemo(() => (isDiff ? null : (code ?? '').replace(/\n$/, '').split('\n')), [isDiff, code])

  const copy = useCallback(() => {
    if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) return
    navigator.clipboard
      .writeText(raw)
      .then(() => {
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      })
      .catch(() => {
        /* clipboard refused (insecure context / permission) — nothing to recover */
      })
  }, [raw])

  const added = rows?.filter((r) => r.kind === 'add').length ?? 0
  const removed = rows?.filter((r) => r.kind === 'del').length ?? 0
  const title = filename ?? language ?? (isDiff ? 'diff' : 'output')

  return (
    <div
      className={cn('w-full overflow-hidden rounded-xl border border-line bg-surface', className)}
      data-slot="code-block"
    >
      <div className="flex h-9 items-center gap-2 border-b border-line px-3 text-[12px]">
        <span className="inline-flex min-w-0 items-center gap-1.5">
          <FileCode className="size-3.5 shrink-0 text-ink-3" aria-hidden />
          <span className="truncate font-mono leading-none text-ink">{title}</span>
        </span>
        <span className="ml-auto inline-flex items-center gap-2">
          {isDiff ? (
            <span
              className="inline-flex items-center gap-1.5 font-mono text-[11.5px] leading-none tabular-nums"
              aria-label={`${added} added, ${removed} removed`}
            >
              <span className="text-emerald-700 dark:text-emerald-300">+{added}</span>
              <span className="text-rose-700 dark:text-rose-300">−{removed}</span>
            </span>
          ) : null}
          <button
            type="button"
            aria-label={copied ? 'Copied' : 'Copy code'}
            onClick={copy}
            className={cn(
              '-mr-1 flex h-6 items-center gap-1 rounded-md px-1.5 text-[11.5px] font-medium transition-colors duration-100 hover:bg-muted/60 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none',
              copied ? 'text-emerald-700 dark:text-emerald-300' : 'text-ink-3 hover:text-ink',
            )}
          >
            {copied ? <Check className="size-3" aria-hidden /> : <Copy className="size-3" aria-hidden />}
            <span aria-live="polite">{copied ? 'Copied' : 'Copy'}</span>
          </button>
        </span>
      </div>

      <div
        className="overflow-auto py-2 font-mono text-[12px] leading-[1.65] text-ink-2"
        style={maxHeight ? { maxHeight } : undefined}
      >
        <div className="relative min-w-fit">
          <span aria-hidden className="pointer-events-none absolute inset-y-0 left-10 w-px bg-line" />
          {rows
            ? rows.map((r, i) => {
                const num = r.kind === 'del' ? r.old : r.cur
                return (
                  <div
                    key={i}
                    data-line-kind={r.kind}
                    className={cn('relative grid grid-cols-[2.5rem_minmax(0,1fr)] items-start', ROW_TONE[r.kind])}
                  >
                    {r.kind === 'add' || r.kind === 'del' ? (
                      <span
                        aria-hidden
                        className={cn(
                          'absolute inset-y-0 left-0 w-[3px]',
                          r.kind === 'add' ? 'bg-emerald-500 dark:bg-emerald-400' : 'text-rose-600 dark:text-rose-400',
                        )}
                        style={r.kind === 'del' ? { background: HATCH } : undefined}
                      />
                    ) : null}
                    <span className={cn('pr-2 text-right text-[11px] select-none', NUM_TONE[r.kind])}>
                      {num ?? ''}
                    </span>
                    <code className="pr-3 pl-2.5 break-words whitespace-pre-wrap">
                      {r.kind === 'ctx' || r.kind === 'add' || r.kind === 'del'
                        ? highlight(r.text, tint)
                        : r.text}
                    </code>
                  </div>
                )
              })
            : lines!.map((line, i) => (
                <div key={i} data-line-kind="code" className="grid grid-cols-[2.5rem_minmax(0,1fr)] items-start">
                  <span className="pr-2 text-right text-[11px] text-ink-3 select-none">{i + 1}</span>
                  <code className="pr-3 pl-2.5 break-words whitespace-pre-wrap">{highlight(line, tint)}</code>
                </div>
              ))}
        </div>
      </div>
    </div>
  )
}

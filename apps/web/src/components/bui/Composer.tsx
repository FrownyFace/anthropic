/**
 * Composer — adapted from Beautiful UI `PromptBar` ("Rounded" variant, `demo={false}`; MIT,
 * © 2026 Shane Levine).
 *
 * Kept: the rounded bar, the auto-growing textarea, the slash menu that pops above the bar with
 * a sliding hover highlight and arrow-key selection, the chip-style controls, and the send
 * button that lights up when there is something to send.
 *
 * Changed: `/` lists the harness scenarios (picking one sets `scenarioId` and fills the prompt
 * with its `task_prompt`); the model chip is a shadcn DropdownMenu over the allowlist; a seed
 * chip holds a small numeric input. Dropped: the `@` sources menu, attachments, dictation, and
 * the `glimm` shader/sound celebration.
 */

import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { ArrowUp, Check, ChevronDown, Cpu, Dices, LoaderCircle, Slash } from 'lucide-react'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { Scenario } from '@/lib/types'
import { cn } from '@/lib/utils'

export interface ComposerProps {
  scenarios: Scenario[]
  scenarioId: string | null
  onScenarioChange: (id: string) => void
  models: readonly string[]
  model: string
  onModelChange: (m: string) => void
  seed: number | null
  onSeedChange: (s: number | null) => void
  prompt: string
  onPromptChange: (p: string) => void
  onSubmit: () => void
  /** Blocks sending only — typing, `/` scenario picking, model and seed stay usable. */
  disabled?: boolean
  busy?: boolean
  placeholder?: string
  /** Shown under the bar, e.g. "harness unreachable". */
  hint?: string
}

/** The slash query when the draft is exactly `/` + word characters, else null. */
export function parseSlash(draft: string): string | null {
  const m = /^\/([\w-]*)$/.exec(draft)
  return m ? m[1]!.toLowerCase() : null
}

export function filterScenarios(scenarios: readonly Scenario[], query: string): Scenario[] {
  const q = query.toLowerCase()
  return scenarios.filter((s) => s.id.toLowerCase().startsWith(q) || s.title.toLowerCase().includes(q))
}

const MIN_H = 28
const MAX_H = 160

const CHIP =
  'flex h-7 shrink-0 items-center gap-1 rounded-[8px] px-1.5 text-[12px] font-medium text-ink-2 transition-colors duration-150 hover:bg-muted/60 hover:text-ink focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50 aria-expanded:bg-muted/60 aria-expanded:text-ink'

export function Composer({
  scenarios,
  scenarioId,
  onScenarioChange,
  models,
  model,
  onModelChange,
  seed,
  onSeedChange,
  prompt,
  onPromptChange,
  onSubmit,
  disabled = false,
  busy = false,
  placeholder = 'Type / to pick a scenario, or describe the task…',
  hint,
}: ComposerProps) {
  const [dismissed, setDismissed] = useState(false)
  const [forced, setForced] = useState(false)
  const [active, setActive] = useState(0)
  const [engaged, setEngaged] = useState(false)
  const [rowBox, setRowBox] = useState<{ top: number; height: number } | null>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const rowRefs = useRef<(HTMLButtonElement | null)[]>([])
  const listId = useId()

  const slash = dismissed ? null : parseSlash(prompt)
  const query = forced ? '' : slash
  const menuOpen = query !== null
  const rows = menuOpen ? filterScenarios(scenarios, query) : []

  useEffect(() => {
    setActive(0)
    setEngaged(false)
  }, [menuOpen, query])

  useLayoutEffect(() => {
    const target = rowRefs.current[active]
    if (target) setRowBox({ top: target.offsetTop, height: target.offsetHeight })
  }, [menuOpen, query, active, rows.length])

  useLayoutEffect(() => {
    const input = inputRef.current
    if (!input) return
    input.style.height = '0px'
    const contentHeight = input.scrollHeight
    input.style.height = `${Math.min(Math.max(contentHeight, MIN_H), MAX_H)}px`
    input.style.overflowY = contentHeight > MAX_H ? 'auto' : 'hidden'
  }, [prompt])

  useEffect(() => {
    if (!forced) return
    const close = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setForced(false)
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [forced])

  const pick = (s: Scenario) => {
    onScenarioChange(s.id)
    onPromptChange(s.task_prompt)
    setForced(false)
    setDismissed(false)
    inputRef.current?.focus()
  }

  const canSend = !disabled && !busy && prompt.trim().length > 0
  const send = () => {
    if (!canSend) return
    onSubmit()
  }

  return (
    <div ref={rootRef} data-slot="composer" className="w-full">
      <div className="relative">
        {menuOpen ? (
          <div
            id={listId}
            role="listbox"
            aria-label="Scenarios"
            onMouseLeave={() => setEngaged(false)}
            className="bui-pop-in absolute inset-x-0 bottom-full z-20 mb-2 origin-bottom rounded-[10px] border border-line bg-popover p-1 text-popover-foreground shadow-lg"
          >
            <span
              aria-hidden
              className="pointer-events-none absolute inset-x-1 rounded-md bg-muted/60 motion-safe:transition-[top,height,opacity] motion-safe:duration-200 motion-safe:ease-[cubic-bezier(0.23,1,0.32,1)]"
              style={{
                top: rowBox?.top ?? 0,
                height: rowBox?.height ?? 0,
                opacity: rowBox && engaged && rows.length > 0 ? 1 : 0,
              }}
            />
            {rows.map((s, i) => (
              <button
                key={s.id}
                type="button"
                role="option"
                id={`${listId}-${i}`}
                aria-selected={i === active}
                ref={(el) => {
                  rowRefs.current[i] = el
                }}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => {
                  setActive(i)
                  setEngaged(true)
                }}
                onClick={() => pick(s)}
                className={cn(
                  'relative z-10 flex h-9 w-full items-center gap-2.5 rounded-md px-2 text-left focus-visible:outline-none',
                  i === active && !engaged && 'bg-muted/40',
                )}
              >
                <span className="flex size-5 shrink-0 items-center justify-center text-ink-3">
                  <Slash className="size-3.5" aria-hidden />
                </span>
                <span className="shrink-0 font-mono text-[12.5px] font-medium text-ink">{s.id}</span>
                <span className="min-w-0 flex-1 truncate text-[12px] text-ink-3">{s.title}</span>
                {s.id === scenarioId ? <Check className="size-3.5 shrink-0 text-ink" aria-label="current" /> : null}
              </button>
            ))}
            {rows.length === 0 ? (
              <div className="flex h-9 items-center px-2 text-[12px] text-ink-3">
                No scenario matches “/{query}”
              </div>
            ) : null}
            <div className="mt-1 border-t border-line px-2 pt-1.5 pb-1 text-[11px] text-ink-3">
              ↑↓ to move · Enter to pick · Esc to dismiss
            </div>
          </div>
        ) : null}

        <div className="relative isolate flex flex-col gap-1.5 overflow-hidden rounded-[14px] border border-line bg-surface p-1.5 shadow-sm transition-[border-color] duration-150 focus-within:border-line-strong">
          <textarea
            ref={inputRef}
            rows={1}
            value={prompt}
            onChange={(event) => {
              onPromptChange(event.target.value)
              setDismissed(false)
              setForced(false)
            }}
            onKeyDown={(event) => {
              if (menuOpen) {
                if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                  event.preventDefault()
                  if (rows.length === 0) return
                  setEngaged(true)
                  setActive((cur) => (cur + (event.key === 'ArrowDown' ? 1 : rows.length - 1)) % rows.length)
                  return
                }
                if ((event.key === 'Enter' && !event.shiftKey) || event.key === 'Tab') {
                  event.preventDefault()
                  const target = rows[active]
                  if (target) pick(target)
                  return
                }
                if (event.key === 'Escape') {
                  event.preventDefault()
                  setDismissed(true)
                  setForced(false)
                  return
                }
              }
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                send()
              }
            }}
            placeholder={placeholder}
            aria-label="Prompt"
            aria-autocomplete="list"
            aria-controls={menuOpen ? listId : undefined}
            aria-activedescendant={menuOpen && rows[active] ? `${listId}-${active}` : undefined}
            className="min-h-7 w-full min-w-0 resize-none bg-transparent px-1.5 py-[5px] text-[13px] leading-[18px] text-ink outline-none [overflow-wrap:anywhere] placeholder:text-ink-3"
          />

          <div className="flex flex-wrap items-center gap-1">
            <button
              type="button"
              aria-expanded={menuOpen}
              aria-controls={listId}
              aria-label="Choose scenario"
              onClick={() => {
                setForced((cur) => !cur)
                setDismissed(false)
                inputRef.current?.focus()
              }}
              className={CHIP}
            >
              <Slash className="size-3 text-ink-3" aria-hidden />
              <span className={cn('font-mono', !scenarioId && 'text-ink-3')}>{scenarioId ?? 'scenario'}</span>
              <ChevronDown className="size-3 text-ink-3" aria-hidden />
            </button>

            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <button type="button" aria-label="Choose model" className={CHIP}>
                    <Cpu className="size-3 text-ink-3" aria-hidden />
                    <span className="font-mono">{model}</span>
                    <ChevronDown className="size-3 text-ink-3" aria-hidden />
                  </button>
                }
              />
              <DropdownMenuContent align="start" side="top" className="w-56">
                <DropdownMenuRadioGroup value={model} onValueChange={(v) => onModelChange(String(v))}>
                  {models.map((m) => (
                    <DropdownMenuRadioItem key={m} value={m}>
                      <span className="font-mono text-xs">{m}</span>
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>

            <label className={cn(CHIP, 'cursor-text')}>
              <Dices className="size-3 text-ink-3" aria-hidden />
              <span className="sr-only">Seed</span>
              <input
                type="text"
                inputMode="numeric"
                pattern="[0-9]*"
                value={seed ?? ''}
                placeholder="seed"
                onChange={(event) => {
                  const digits = event.target.value.replace(/\D/g, '')
                  onSeedChange(digits === '' ? null : Number(digits))
                }}
                className="w-12 bg-transparent font-mono text-[12px] text-ink outline-none placeholder:text-ink-3"
              />
            </label>

            <span className="flex-1" />

            <button
              type="button"
              aria-label={busy ? 'Running' : 'Send'}
              disabled={!canSend}
              onClick={send}
              className={cn(
                'flex size-7 shrink-0 items-center justify-center rounded-[8px] transition-[background-color,color,transform] duration-200 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none enabled:active:scale-[0.94] disabled:cursor-not-allowed',
                canSend ? 'bg-ink text-canvas' : 'bg-line-strong text-ink-2',
              )}
            >
              {busy ? (
                <LoaderCircle className="bui-spin size-4" aria-hidden />
              ) : (
                <ArrowUp className="size-4" strokeWidth={2.4} aria-hidden />
              )}
            </button>
          </div>
        </div>
      </div>

      {hint ? (
        <p className="mt-1.5 px-1 text-[11.5px] text-ink-3" aria-live="polite">
          {hint}
        </p>
      ) : null}
    </div>
  )
}

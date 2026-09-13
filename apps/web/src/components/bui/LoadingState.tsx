/**
 * LoadingState — adapted from Beautiful UI `LoadingState` ("Drive" variant; MIT, © 2026 Shane
 * Levine). A 3×3 pixel grid pulses in a chevron sweep next to a shimmering label and a running
 * elapsed clock. The video "Surfer" variant was dropped.
 */

import { useEffect, useState, type CSSProperties } from 'react'

import { cn } from '@/lib/utils'

export interface LoadingStateProps {
  label: string
  sublabel?: string
  className?: string
}

/** Delay per cell so the grid lights up as a chevron pointing right. */
const CHEVRON = Array.from({ length: 9 }, (_, i) => {
  const r = Math.floor(i / 3)
  const c = i % 3
  return (c + Math.abs(r - 1)) * 90
})
const DUR_MS = 650

function LoaderGrid() {
  return (
    <span aria-hidden className="grid shrink-0 grid-cols-[repeat(3,4px)] gap-[1.5px]">
      {CHEVRON.map((delay, i) => (
        <span
          key={i}
          className="bui-pixel size-[4px] rounded-[1px] bg-ink opacity-15"
          style={{ '--bui-delay': `${delay}ms`, '--bui-dur': `${DUR_MS}ms` } as CSSProperties}
        />
      ))}
    </span>
  )
}

function useElapsed(): string {
  const [ds, setDs] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setDs((d) => d + 1), 100)
    return () => clearInterval(t)
  }, [])
  const total = ds / 10
  if (total < 60) return `${total.toFixed(1)}s`
  return `${Math.floor(total / 60)}m ${(total % 60).toFixed(1)}s`
}

export function LoadingState({ label, sublabel, className }: LoadingStateProps) {
  const elapsed = useElapsed()
  return (
    <div role="status" aria-live="polite" className={cn('flex w-fit flex-col gap-1', className)}>
      <div className="flex items-center gap-2.5">
        <LoaderGrid />
        <span className="bui-shimmer text-[13px] font-medium text-ink-2">{label}</span>
        <span className="font-mono text-[12px] text-ink-3 tabular-nums" aria-hidden>
          {elapsed}
        </span>
      </div>
      {sublabel ? <p className="pl-[26px] text-[12px] text-ink-3">{sublabel}</p> : null}
    </div>
  )
}

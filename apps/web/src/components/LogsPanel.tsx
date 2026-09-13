import { useEffect, useMemo, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { subscribeLogs } from '@/lib/log'
import type { ViewState } from '@/lib/reducer'
import type { LogLevel, LogLine } from '@/lib/types'

const LEVELS: (LogLevel | 'all')[] = ['all', 'debug', 'info', 'warn', 'error']
const RANK: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 }

const LVL_TONE: Record<LogLevel, string> = {
  debug: 'text-muted-foreground',
  info: 'text-sky-700 dark:text-sky-300',
  warn: 'text-amber-700 dark:text-amber-300',
  error: 'text-rose-700 dark:text-rose-300',
}

const ROW_ESTIMATE_PX = 22

function renderExtras(line: LogLine): string {
  const skip = new Set(['ts', 'svc', 'lvl', 'ev', 'msg', '_seq'])
  const parts: string[] = []
  for (const [k, v] of Object.entries(line)) {
    if (skip.has(k) || v === null || v === undefined) continue
    parts.push(`${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
  }
  return parts.join(' ')
}

/**
 * Unified log lines from the run's event stream (harness + sandbox-env) merged with the browser's
 * own log ring buffer. Rows are virtualized (@tanstack/react-virtual) so a long run's thousands of
 * lines stay cheap; each row is measured after render because lines wrap.
 */
export function LogsPanel({ state, live = false }: { state: ViewState; live?: boolean }) {
  const [level, setLevel] = useState<LogLevel | 'all'>('all')
  const [raw, setRaw] = useState(false)
  const [follow, setFollow] = useState(true)
  const [webLines, setWebLines] = useState<LogLine[]>([])
  const scrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => subscribeLogs(setWebLines), [])

  const lines = useMemo(() => {
    const merged = [...state.logs, ...webLines].sort((a, b) =>
      String(a.ts ?? '').localeCompare(String(b.ts ?? '')),
    )
    if (level === 'all') return merged
    const min = RANK[level]
    return merged.filter((l) => (RANK[(l.lvl as LogLevel) ?? 'info'] ?? 20) >= min)
  }, [state.logs, webLines, level])

  const virtualizer = useVirtualizer({
    count: lines.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_ESTIMATE_PX,
    overscan: 12,
    getItemKey: (i) => `${lines[i]?.ts ?? ''}-${i}`,
  })

  // Follow the tail while live unless the reader scrolled up.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onScroll = () => {
      setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 48)
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (!follow || lines.length === 0) return
    virtualizer.scrollToIndex(lines.length - 1, { align: 'end' })
  }, [lines.length, follow, virtualizer, raw])

  const items = virtualizer.getVirtualItems()

  return (
    <div className="flex h-full min-h-0 flex-col" aria-label="Logs">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <span className="text-xs text-muted-foreground">level</span>
        {LEVELS.map((l) => (
          <Button
            key={l}
            size="sm"
            variant={level === l ? 'secondary' : 'ghost'}
            className="h-6 px-2 font-mono text-[11px]"
            aria-pressed={level === l}
            onClick={() => setLevel(l)}
          >
            {l}
          </Button>
        ))}
        <span className="flex-1" />
        {live ? (
          <Button
            size="sm"
            variant={follow ? 'secondary' : 'ghost'}
            className="h-6 px-2 font-mono text-[11px]"
            aria-pressed={follow}
            onClick={() => {
              setFollow(true)
              if (lines.length) virtualizer.scrollToIndex(lines.length - 1, { align: 'end' })
            }}
            title="Keep the newest line in view"
          >
            follow
          </Button>
        ) : null}
        <Button
          size="sm"
          variant={raw ? 'secondary' : 'ghost'}
          className="h-6 px-2 font-mono text-[11px]"
          aria-pressed={raw}
          onClick={() => setRaw((v) => !v)}
        >
          raw json
        </Button>
        <Badge variant="outline" className="font-mono text-[11px]" title="lines after the level filter">
          {lines.length}
        </Badge>
      </div>

      <div
        ref={scrollRef}
        className="min-h-0 flex-1 overflow-auto font-mono text-[11.5px] leading-[1.6]"
        role="log"
        aria-live={live ? 'polite' : 'off'}
      >
        {lines.length === 0 ? (
          <p className="p-4 text-muted-foreground">
            No log lines yet. The harness mirrors its unified JSON log into the event stream.
          </p>
        ) : (
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }} className="w-full">
            {items.map((item) => {
              const l = lines[item.index]!
              return (
                <div
                  key={item.key}
                  ref={virtualizer.measureElement}
                  data-index={item.index}
                  className="absolute top-0 left-0 w-full px-3 py-px"
                  style={{ transform: `translateY(${item.start}px)` }}
                >
                  {raw ? (
                    <div className="break-all whitespace-pre-wrap text-foreground/80">{JSON.stringify(l)}</div>
                  ) : (
                    <div className="grid grid-cols-[84px_40px_minmax(0,1fr)] gap-x-2">
                      <span className="truncate text-muted-foreground/70" title={typeof l.ts === 'string' ? l.ts : ''}>
                        {typeof l.ts === 'string' ? l.ts.slice(11, 23) : ''}
                      </span>
                      <span className={LVL_TONE[(l.lvl as LogLevel) ?? 'info']}>{l.lvl}</span>
                      <span className="min-w-0 [overflow-wrap:anywhere] whitespace-pre-wrap">
                        <span className="text-muted-foreground">{l.svc}</span>{' '}
                        <span className="text-foreground">{l.ev}</span>
                        {l.msg ? <span className="text-foreground/70"> {l.msg}</span> : null}
                        {(() => {
                          const extras = renderExtras(l)
                          return extras ? <span className="text-muted-foreground/70"> {extras}</span> : null
                        })()}
                      </span>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

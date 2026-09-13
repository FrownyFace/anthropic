import { useCallback, useEffect, useMemo, useState } from 'react'

import { getLogger } from '@/lib/log'
import { fromRunRecord, reduceAll, initialState, type ViewState } from '@/lib/reducer'
import { DEMO_STEP_MS, DEMOS, loadDemo } from '@/lib/replay'
import type { Event, RunRecord } from '@/lib/types'

const log = getLogger('web')

export interface ReplayState {
  record: RunRecord | null
  events: Event[]
  /** View folded from the first `cursor` events. */
  state: ViewState
  cursor: number
  setCursor: (n: number) => void
  playing: boolean
  setPlaying: (p: boolean) => void
  restart: () => void
  loading: boolean
  error: string | null
  /** true while auto-advancing and not at the end */
  live: boolean
  done: boolean
}

interface Loaded {
  demoId: string
  record: RunRecord | null
  error: string | null
}

interface Playhead {
  demoId: string
  cursor: number
  playing: boolean
}

/**
 * Scrubbable playback of a bundled RunRecord: the view is a pure fold of the first `cursor`
 * events, so the slider can move in both directions and the transcript, workspace and story stay
 * consistent with each other.
 *
 * State is keyed by `demoId` (the loaded record and the playhead both remember which demo they
 * belong to), so switching demos resets by derivation rather than by setState inside an effect.
 */
export function useReplay(demoId: string): ReplayState {
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [playhead, setPlayhead] = useState<Playhead>({ demoId, cursor: 0, playing: true })

  useEffect(() => {
    let cancelled = false
    const meta = DEMOS.find((d) => d.id === demoId)
    void (async () => {
      try {
        if (!meta) throw new Error(`no bundled demo named "${demoId}"`)
        const rec = await loadDemo(meta.file)
        if (cancelled) return
        setLoaded({ demoId, record: rec, error: null })
        log.info('replay.loaded', 'bundled run loaded', { demo: demoId, events: rec.events.length })
      } catch (err) {
        if (cancelled) return
        setLoaded({ demoId, record: null, error: String(err) })
        log.error('replay.error', 'failed to load bundled run', { demo: demoId, error: String(err) })
      }
    })()
    return () => {
      cancelled = true
    }
  }, [demoId])

  const current = loaded && loaded.demoId === demoId ? loaded : null
  const record = current?.record ?? null
  const loading = current === null
  const error = current?.error ?? null

  const head = playhead.demoId === demoId ? playhead : { demoId, cursor: 0, playing: true }
  const events = useMemo(() => record?.events ?? [], [record])
  const total = events.length
  const cursor = Math.min(head.cursor, total)
  // Playing past the end is meaningless, so "playing" is derived rather than switched off.
  const playing = head.playing && (record === null || cursor < total)

  useEffect(() => {
    if (!playing || !record || cursor >= total) return
    const t = setTimeout(
      () =>
        setPlayhead((h) =>
          h.demoId === demoId ? { ...h, cursor: Math.min(h.cursor + 1, total) } : h,
        ),
      DEMO_STEP_MS,
    )
    return () => clearTimeout(t)
  }, [playing, cursor, total, record, demoId])

  const base = useMemo(() => {
    if (!record) return initialState()
    const seeded = fromRunRecord({
      ...record,
      events: [],
      evaluation: null,
      finished_at: null,
      error: null,
      error_class: null,
      interruptions: [],
      worker_generation: 1,
    })
    return { ...seeded, status: 'running' as const, evaluation: null }
  }, [record])

  const state = useMemo(() => reduceAll(events.slice(0, cursor), base), [events, cursor, base])

  const setCursor = useCallback(
    (n: number) => setPlayhead({ demoId, cursor: Math.max(0, Math.min(n, total)), playing: false }),
    [demoId, total],
  )
  const setPlaying = useCallback(
    (p: boolean) =>
      setPlayhead((h) => ({ demoId, cursor: h.demoId === demoId ? h.cursor : 0, playing: p })),
    [demoId],
  )
  const restart = useCallback(() => setPlayhead({ demoId, cursor: 0, playing: true }), [demoId])

  return {
    record,
    events,
    state,
    cursor,
    setCursor,
    playing,
    setPlaying,
    restart,
    loading,
    error,
    live: playing && cursor < total,
    done: total > 0 && cursor >= total,
  }
}

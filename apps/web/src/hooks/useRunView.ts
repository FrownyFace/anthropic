/**
 * Owns one run's view state and its transport: GET /runs/{id} to seed (so a page refresh mid-run
 * restores everything), then subscribeRun() for the tail.
 *
 * The seed is best-effort in one direction only: a run created a moment ago can 5xx or time out
 * until the harness has written it, and then the tail must still start. A 404 is different — the
 * harness answers 404 both for an unknown run and for one that belongs to another browser identity
 * (no existence oracle) — so it is reported as `notFound` and no tail is opened.
 *
 * State is keyed by the source, so switching runs resets by derivation rather than by setState
 * inside the effect.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { HarnessClient, isNotFound, subscribeRun, type Subscription, type TransportState } from '@/lib/api'
import { getLogger } from '@/lib/log'
import { fromRunRecord, initialState, reduce, type ViewState } from '@/lib/reducer'
import type { Event, RunRecord } from '@/lib/types'

const log = getLogger('web')

export type RunSource = { kind: 'none' } | { kind: 'live'; runId: string }

export interface RunViewState {
  state: ViewState
  transport: TransportState | null
  /** Transport / server error from the seed (the tail still runs). Null for a 404 — see `notFound`. */
  loadError: string | null
  loading: boolean
  /** GET /runs/{id} answered 404: unknown run, or one owned by another browser identity. */
  notFound: boolean
  /** The seed record, when the harness returned one (conversation_id, task_prompt, …). */
  record: RunRecord | null
}

interface Slot {
  key: string
  state: ViewState
  transport: TransportState | null
  loadError: string | null
  loading: boolean
  notFound: boolean
  record: RunRecord | null
}

function emptySlot(key: string): Slot {
  return {
    key,
    state: initialState(),
    transport: null,
    loadError: null,
    loading: key !== 'none',
    notFound: false,
    record: null,
  }
}

function sourceKey(source: RunSource): string {
  return source.kind === 'live' ? `live:${source.runId}` : 'none'
}

export function useRunView(client: HarnessClient | null, source: RunSource): RunViewState {
  const key = sourceKey(source)
  const [slot, setSlot] = useState<Slot | null>(null)
  const subRef = useRef<Subscription | null>(null)

  /** Update the slot for `key` only; a stale callback for a previous run is dropped. */
  const patch = useCallback((forKey: string, fn: (s: Slot) => Slot) => {
    setSlot((prev) => {
      const base = prev && prev.key === forKey ? prev : emptySlot(forKey)
      return fn(base)
    })
  }, [])

  const push = useCallback(
    (forKey: string, events: Event[]) => {
      patch(forKey, (s) => ({ ...s, state: events.reduce(reduce, s.state) }))
    },
    [patch],
  )

  useEffect(() => {
    let cancelled = false
    const stop = () => {
      cancelled = true
      subRef.current?.close()
      subRef.current = null
    }

    if (key === 'none') return stop

    const runId = key.slice('live:'.length)
    if (!client) return stop // reported as "harness URL not resolved yet" by derivation below

    void (async () => {
      let from = -1
      try {
        const rec = await client.getRun(runId)
        if (cancelled) return
        patch(key, (s) => ({ ...s, state: fromRunRecord(rec), record: rec, loadError: null, notFound: false }))
        from = rec.events?.length ? Math.max(...rec.events.map((e) => e.id)) : -1
        log.info('run.restored', 'seeded view from GET /runs/{id}', {
          run_id: runId,
          events: rec.events?.length ?? 0,
          status: rec.status,
        })
      } catch (err) {
        if (cancelled) return
        if (isNotFound(err)) {
          patch(key, (s) => ({ ...s, loading: false, notFound: true, loadError: null }))
          log.warn('run.not_found', 'GET /runs/{id} is 404 for this identity; not tailing', { run_id: runId })
          return
        }
        patch(key, (s) => ({ ...s, loadError: String(err) }))
        log.warn('run.load_error', 'GET /runs failed; relying on the stream', {
          run_id: runId,
          error: String(err),
        })
      }
      if (cancelled) return
      patch(key, (s) => ({ ...s, loading: false }))
      subRef.current = subscribeRun({
        base: client.base,
        runId,
        lastEventId: from,
        headers: client.headers(),
        onEvents: (events) => {
          if (!cancelled) push(key, events)
        },
        onRecord: (r) => {
          if (cancelled) return
          // The polling fallback returns the whole record; take the summary fields from it so the
          // header stays right even if a `run.finished` event was missed.
          patch(key, (s) => ({
            ...s,
            record: r,
            state: {
              ...s.state,
              status: r.status ?? s.state.status,
              usage: r.usage ?? s.state.usage,
              evaluation: r.evaluation ?? s.state.evaluation,
              error: r.error ?? s.state.error,
              finishedAt: r.finished_at ?? s.state.finishedAt,
            },
          }))
        },
        onTransport: (t) => {
          if (!cancelled) patch(key, (s) => ({ ...s, transport: t }))
        },
      })
    })()

    return stop
  }, [key, client, patch, push])

  const current = slot && slot.key === key ? slot : emptySlot(key)
  const noClient = key !== 'none' && client === null
  return {
    state: current.state,
    transport: current.transport,
    loadError: noClient ? 'harness URL not resolved yet' : current.loadError,
    loading: noClient ? false : current.loading,
    notFound: current.notFound,
    record: current.record,
  }
}

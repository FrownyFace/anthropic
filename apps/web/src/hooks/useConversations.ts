/**
 * The conversation rail: GET /conversations for the current identity.
 *
 * State is the last *loaded* answer, keyed by the request that produced it (client identity +
 * refresh epoch); `loading` is derived from whether that key is current. No setState runs
 * synchronously inside the effect, so a refresh or an identity reset costs one render, not two.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { HarnessClient } from '@/lib/api'
import { getLogger } from '@/lib/log'
import type { ConversationSummary } from '@/lib/types'

const log = getLogger('web')

export interface ConversationsState {
  items: ConversationSummary[]
  loading: boolean
  /** Transport / server error text (a 404 on /conversations is an error like any other). */
  error: string | null
  refresh: () => void
}

interface Loaded {
  client: HarnessClient
  epoch: number
  items: ConversationSummary[]
  error: string | null
}

export function useConversations(client: HarnessClient | null): ConversationsState {
  const [epoch, setEpoch] = useState(0)
  const [loaded, setLoaded] = useState<Loaded | null>(null)

  useEffect(() => {
    if (!client) return
    let cancelled = false

    client
      .listConversations()
      .then((list) => {
        if (cancelled) return
        setLoaded({ client, epoch, items: list, error: null })
        log.info('conversations.loaded', 'rail loaded', { count: list.length })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        // Keep whatever was on screen; just surface the error.
        setLoaded((prev) => ({
          client,
          epoch,
          items: prev?.items ?? [],
          error: String(err),
        }))
        log.warn('conversations.error', 'GET /conversations failed', { error: String(err) })
      })

    return () => {
      cancelled = true
    }
  }, [client, epoch])

  const refresh = useCallback(() => setEpoch((n) => n + 1), [])

  return useMemo(() => {
    const current = !!client && !!loaded && loaded.client === client && loaded.epoch === epoch
    return {
      items: loaded?.items ?? [],
      loading: !!client && !current,
      error: current ? loaded.error : null,
      refresh,
    }
  }, [client, loaded, epoch, refresh])
}

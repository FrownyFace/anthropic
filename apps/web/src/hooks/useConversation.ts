/**
 * One persisted conversation: GET /conversations/{id} → runs + messages.
 *
 * The harness answers 404 both for an unknown id and for an id that belongs to another browser
 * (no existence oracle), so the two are reported identically as "not found".
 *
 * State is the last *loaded* answer, keyed by (client, id, refresh epoch). `detail` / `error`
 * are only exposed while that key is current, so switching conversations never shows the
 * previous one's transcript while the next loads — and no setState runs synchronously in the
 * effect.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { HarnessClient, isNotFound } from '@/lib/api'
import { getLogger } from '@/lib/log'
import type { ConversationDetail } from '@/lib/types'

const log = getLogger('web')

export const NOT_FOUND = 'not found'

export interface ConversationState {
  detail: ConversationDetail | null
  loading: boolean
  /** `NOT_FOUND` for a 404, otherwise the transport / server error text. */
  error: string | null
  refresh: () => void
}

interface Loaded {
  client: HarnessClient
  id: string
  epoch: number
  detail: ConversationDetail | null
  error: string | null
}

export function useConversation(client: HarnessClient | null, id: string | null): ConversationState {
  const [epoch, setEpoch] = useState(0)
  const [loaded, setLoaded] = useState<Loaded | null>(null)

  useEffect(() => {
    if (!client || !id) return
    let cancelled = false

    client
      .getConversation(id)
      .then((value) => {
        if (cancelled) return
        setLoaded({ client, id, epoch, detail: value, error: null })
        log.info('conversation.loaded', 'GET /conversations/{id}', {
          conversation_id: id,
          runs: value.runs?.length ?? 0,
          messages: value.messages?.length ?? 0,
        })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (isNotFound(err)) {
          setLoaded({ client, id, epoch, detail: null, error: NOT_FOUND })
          log.warn('conversation.not_found', 'conversation is 404 for this identity', { conversation_id: id })
          return
        }
        setLoaded({ client, id, epoch, detail: null, error: String(err) })
        log.warn('conversation.error', 'GET /conversations/{id} failed', { conversation_id: id, error: String(err) })
      })

    return () => {
      cancelled = true
    }
  }, [client, id, epoch])

  const refresh = useCallback(() => setEpoch((n) => n + 1), [])

  return useMemo(() => {
    const wanted = !!client && !!id
    const current =
      wanted && !!loaded && loaded.client === client && loaded.id === id && loaded.epoch === epoch
    return {
      detail: current ? loaded.detail : null,
      loading: wanted && !current,
      error: current ? loaded.error : null,
      refresh,
    }
  }, [client, id, loaded, epoch, refresh])
}

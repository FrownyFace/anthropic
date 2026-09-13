import { useCallback, useState } from 'react'

import type { ConversationsState } from '@/hooks/useConversations'
import type { HarnessState } from '@/hooks/useHarness'
import { getLogger } from '@/lib/log'
import { navigate } from '@/lib/router'

const log = getLogger('web')

export interface StartParams {
  scenarioId: string
  model: string
  seed: number | null
  /** Task prompt override; empty/null = the scenario default (server side). */
  prompt: string | null
  /** Start the run inside an existing conversation. */
  conversationId?: string | null
  title?: string
}

/**
 * One place that knows how to start a run:
 *   1. inside an existing conversation → POST /conversations/{id}/runs
 *   2. otherwise                        → POST /conversations, then (1)
 * and then navigates to the conversation. Every path refreshes the conversation rail; any
 * failure (a 404 included) is surfaced as the error, never worked around.
 */
export function useStartRun(harness: HarnessState, conversations: ConversationsState) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const start = useCallback(
    async (p: StartParams): Promise<string | null> => {
      const client = harness.client
      if (!client) {
        setError('harness URL not resolved yet')
        return null
      }
      setBusy(true)
      setError(null)
      const req = {
        model: p.model,
        seed: p.seed,
        task_prompt: p.prompt && p.prompt.trim() ? p.prompt : undefined,
      }
      try {
        const conversationId =
          p.conversationId ?? (await client.createConversation({ scenario_id: p.scenarioId, title: p.title })).id
        const r = await client.createConversationRun(conversationId, req)
        log.info('run.started', p.conversationId ? 'run accepted (existing conversation)' : 'run accepted (new conversation)', {
          run_id: r.run_id,
          conversation_id: conversationId,
          scenario_id: p.scenarioId,
          model: p.model,
        })
        navigate({ kind: 'conversation', id: r.conversation_id ?? conversationId, runId: r.run_id })
        conversations.refresh()
        return r.run_id
      } catch (err) {
        setError(String(err))
        log.error('run.start_failed', 'could not start the run', {
          scenario_id: p.scenarioId,
          error: String(err),
        })
        return null
      } finally {
        setBusy(false)
      }
    },
    [harness.client, conversations],
  )

  return { start, busy, error }
}

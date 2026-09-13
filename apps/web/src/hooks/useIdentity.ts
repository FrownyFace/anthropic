/**
 * The anonymous browser id as React state. Every instance of this hook shares one value: a
 * reset() from the sidebar is seen by useHarness(), which then rebuilds its client with the new
 * `X-Faultline-User`.
 */

import { useCallback, useMemo, useSyncExternalStore } from 'react'

import { peekUserId, resetUserId, subscribeIdentity } from '@/lib/identity'
import { getLogger } from '@/lib/log'

const log = getLogger('web')

export interface IdentityState {
  userId: string
  /** Mint a new id (the old conversations become unreachable from this browser). Returns it. */
  reset: () => string
}

function getServerSnapshot(): string {
  return ''
}

export function useIdentity(): IdentityState {
  const userId = useSyncExternalStore(subscribeIdentity, peekUserId, getServerSnapshot)

  const reset = useCallback(() => {
    const next = resetUserId()
    log.info('identity.reset_requested', 'user reset their anonymous id from the UI')
    return next
  }, [])

  return useMemo(() => ({ userId, reset }), [userId, reset])
}

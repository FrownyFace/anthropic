import { describe, expect, it } from 'vitest'

import { healthPhase } from './useHarness'
import type { Health } from '@/lib/types'

const health = (ok: boolean): Health => ({ svc: 'harness', ok, version: '1', has_provider_key: false, detail: {} })

describe('healthPhase', () => {
  it('is "checking", not "unreachable", while /health has not answered yet', () => {
    expect(healthPhase(false, null, null)).toBe('resolving')
    expect(healthPhase(true, null, null)).toBe('checking')
  })

  it('settles on the answer', () => {
    expect(healthPhase(true, health(true), null)).toBe('reachable')
    expect(healthPhase(true, health(false), null)).toBe('unreachable')
    expect(healthPhase(true, null, 'network error')).toBe('unreachable')
    expect(healthPhase(false, null, 'harness URL not configured')).toBe('unreachable')
  })
})

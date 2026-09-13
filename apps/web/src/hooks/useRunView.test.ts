import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as api from '@/lib/api'
import type { RunRecord } from '@/lib/types'

import { useRunView } from './useRunView'

const subscribeRun = vi.spyOn(api, 'subscribeRun').mockImplementation(
  () => ({ close: vi.fn(), state: () => ({ mode: 'closed' as const, errors: 0 }) }),
)

afterEach(() => {
  subscribeRun.mockClear()
})

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

const record: RunRecord = {
  run_id: 'r_1',
  status: 'ok',
  scenario_id: 'lost-ack',
  model: 'claude-haiku-4-5',
  max_steps: 20,
  created_at: '2026-09-12T00:00:00Z',
  finished_at: '2026-09-12T00:01:00Z',
  conversation_id: 'c_9',
  events: [
    { id: 0, ts: '2026-09-12T00:00:00Z', run_id: 'r_1', type: 'run.started', step: null, data: { scenario_id: 'lost-ack', model: 'm' } },
    { id: 1, ts: '2026-09-12T00:00:01Z', run_id: 'r_1', type: 'run.finished', step: null, data: { status: 'ok' } },
  ],
  usage: { input_tokens: 1, output_tokens: 1 },
  evaluation: null,
}

describe('useRunView', () => {
  it('reports a 404 seed as notFound and does NOT open the tail', async () => {
    const fetchImpl = vi.fn(async () => response({ detail: 'Not Found' }, 404)) as unknown as typeof fetch
    const client = new api.HarnessClient('http://h', { userId: 'u_1', fetchImpl })
    const { result } = renderHook(() => useRunView(client, { kind: 'live', runId: 'r_missing' }))
    expect(result.current.loading).toBe(true)
    await waitFor(() => expect(result.current.notFound).toBe(true))
    expect(result.current.loading).toBe(false)
    expect(result.current.loadError).toBeNull()
    expect(result.current.record).toBeNull()
    expect(subscribeRun).not.toHaveBeenCalled()
  })

  it('seeds from the record (exposing it for conversation_id) and tails from the last event id', async () => {
    const fetchImpl = vi.fn(async () => response(record)) as unknown as typeof fetch
    const client = new api.HarnessClient('http://h', { userId: 'u_1', fetchImpl })
    const { result } = renderHook(() => useRunView(client, { kind: 'live', runId: 'r_1' }))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.notFound).toBe(false)
    expect(result.current.record?.conversation_id).toBe('c_9')
    expect(result.current.state.status).toBe('ok')
    expect(subscribeRun).toHaveBeenCalledTimes(1)
    expect(subscribeRun.mock.calls[0]![0]).toMatchObject({ runId: 'r_1', lastEventId: 1, headers: { 'X-Faultline-User': 'u_1' } })
  })

  it('a non-404 seed failure is a loadError and the tail still starts', async () => {
    const fetchImpl = vi.fn(async () => response({ detail: 'boom' }, 503)) as unknown as typeof fetch
    const client = new api.HarnessClient('http://h', { userId: 'u_1', fetchImpl })
    const { result } = renderHook(() => useRunView(client, { kind: 'live', runId: 'r_2' }))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.notFound).toBe(false)
    expect(result.current.loadError).toMatch(/503/)
    expect(subscribeRun).toHaveBeenCalledTimes(1)
  })

  it('says the harness URL is not resolved yet when there is no client', () => {
    const { result } = renderHook(() => useRunView(null, { kind: 'live', runId: 'r_3' }))
    expect(result.current.loading).toBe(false)
    expect(result.current.loadError).toMatch(/not resolved/)
    expect(subscribeRun).not.toHaveBeenCalled()
  })
})

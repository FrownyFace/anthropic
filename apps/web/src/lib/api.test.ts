import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  HarnessClient,
  HarnessError,
  USER_HEADER,
  doneMeansReconnect,
  isNotFound,
  normaliseScenarios,
  parseDone,
  subscribeRun,
  type TransportState,
} from './api'
import type { Event, RunRecord } from './types'

// --------------------------------------------------------------------------- fakes

type Listener = (e: { data?: string }) => void

class FakeEventSource {
  static instances: FakeEventSource[] = []
  static reset() {
    FakeEventSource.instances = []
  }

  readonly listeners = new Map<string, Listener[]>()
  closed = false

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, fn: Listener): void {
    const arr = this.listeners.get(type) ?? []
    arr.push(fn)
    this.listeners.set(type, arr)
  }

  close(): void {
    this.closed = true
  }

  emit(type: string, data?: string): void {
    for (const fn of [...(this.listeners.get(type) ?? [])]) fn({ data })
  }

  deliver(ev: Event): void {
    this.emit('message', JSON.stringify(ev))
  }

  static get last(): FakeEventSource {
    const l = FakeEventSource.instances.at(-1)
    if (!l) throw new Error('no EventSource was opened')
    return l
  }
}

/** Deterministic timer queue so reconnect/poll scheduling is observable without real time. */
function makeClock() {
  let nextId = 1
  const q: { id: number; fn: () => void }[] = []
  return {
    setTimeoutImpl: (fn: () => void, _ms: number) => {
      const id = nextId++
      q.push({ id, fn })
      return id
    },
    clearTimeoutImpl: (h: unknown) => {
      const i = q.findIndex((t) => t.id === h)
      if (i >= 0) q.splice(i, 1)
    },
    pending: () => q.length,
    /** Run exactly one queued callback, then let microtasks settle. */
    async tick() {
      const t = q.shift()
      if (!t) return false
      t.fn()
      for (let i = 0; i < 50; i += 1) await Promise.resolve()
      return true
    },
  }
}

/** Let every pending microtask (async fetch chains) settle. */
async function settle(turns = 50): Promise<void> {
  for (let i = 0; i < turns; i += 1) await Promise.resolve()
}

function mkEvent(id: number, type: Event['type'] = 'turn.text', data: Record<string, unknown> = { text: 'hi' }): Event {
  return { id, ts: '2026-09-12T14:00:00.000Z', run_id: 'r_1', type, step: 1, data }
}

function jsonResponse(body: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    statusText: ok ? 'OK' : 'ERR',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

function record(partial: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: 'r_1',
    status: 'running',
    scenario_id: 'lost-ack',
    model: 'claude-haiku-4-5',
    max_steps: 20,
    created_at: '2026-09-12T14:00:00.000Z',
    events: [],
    usage: { input_tokens: 0, output_tokens: 0 },
    ...partial,
  }
}

const BASE = 'https://harness.example.test'

beforeEach(() => {
  FakeEventSource.reset()
})

// --------------------------------------------------------------------------- fetchers

describe('HarnessClient', () => {
  it('GETs /health, /scenarios and /runs/{id} off the configured base', async () => {
    const fetchImpl = vi.fn(async (url: string | URL) => {
      const u = String(url)
      if (u.endsWith('/health')) return jsonResponse({ svc: 'harness', ok: true, has_provider_key: false })
      if (u.endsWith('/scenarios')) return jsonResponse([{ id: 'lost-ack' }])
      return jsonResponse(record())
    }) as unknown as typeof fetch

    const c = new HarnessClient(BASE, fetchImpl)
    expect((await c.health()).ok).toBe(true)
    expect((await c.scenarios())[0]!.id).toBe('lost-ack')
    expect((await c.getRun('r_1')).run_id).toBe('r_1')
    const calls = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls.map((a) => String(a[0]))
    expect(calls).toEqual([
      `${BASE}/health`,
      `${BASE}/scenarios`,
      `${BASE}/runs/r_1`,
    ])
  })

  it('POSTs /runs with a JSON body', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ run_id: 'r_new' })) as unknown as typeof fetch
    const c = new HarnessClient(BASE, fetchImpl)
    const res = await c.createRun({ scenario_id: 'lost-ack', model: 'claude-sonnet-5' })
    expect(res.run_id).toBe('r_new')
    const init = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0]![1] as RequestInit
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({ scenario_id: 'lost-ack', model: 'claude-sonnet-5' })
  })

  it('throws a HarnessError carrying the status on a non-2xx', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ detail: 'nope' }, false, 503)) as unknown as typeof fetch
    const c = new HarnessClient(BASE, fetchImpl)
    await expect(c.health()).rejects.toMatchObject({ name: 'HarnessError', status: 503 })
  })
})

// --------------------------------------------------------------------------- identity + conversations

type Call = [string, RequestInit]

function calls(fetchImpl: typeof fetch): Call[] {
  return (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls.map(
    (a) => [String(a[0]), a[1] as RequestInit] as Call,
  )
}

function headerOf(init: RequestInit, name: string): string | undefined {
  const h = init.headers as Record<string, string>
  return h[name] ?? h[name.toLowerCase()]
}

const UID = 'u_123e4567-e89b-42d3-a456-426614174000'

describe('HarnessClient identity header', () => {
  it('sends X-Faultline-User on every JSON request when constructed with { userId }', async () => {
    const fetchImpl = vi.fn(async (url: string | URL) => {
      const u = String(url)
      if (u.endsWith('/scenarios')) return jsonResponse({ scenarios: [] })
      if (u.endsWith('/me')) return jsonResponse({ user_id: UID, conversations: 2 })
      if (u.endsWith('/conversations')) return jsonResponse([])
      if (u.endsWith('/runs')) return jsonResponse({ run_id: 'r_9', conversation_id: 'c_1' })
      return jsonResponse({ svc: 'harness', ok: true })
    }) as unknown as typeof fetch

    const c = new HarnessClient(BASE, { userId: UID, fetchImpl })
    expect(c.userId).toBe(UID)
    await c.health()
    await c.scenarios()
    await c.me()
    await c.listConversations()
    await c.createRun({ scenario_id: 'lost-ack' })
    await c.getRun('r_9')

    const all = calls(fetchImpl)
    expect(all).toHaveLength(6)
    for (const [, init] of all) {
      expect(headerOf(init, USER_HEADER)).toBe(UID)
      expect(init.credentials).toBe('omit')
    }
    // The JSON content-type on POST must not have displaced the identity header.
    const post = all.find(([, i]) => i.method === 'POST')!
    expect(headerOf(post[1], 'content-type')).toBe('application/json')
  })

  it('sends no identity header when constructed without one (positional fetchImpl form)', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ ok: true })) as unknown as typeof fetch
    const c = new HarnessClient(BASE, fetchImpl)
    expect(c.userId).toBeNull()
    await c.health()
    expect(headerOf(calls(fetchImpl)[0]![1], USER_HEADER)).toBeUndefined()
    expect(c.headers()).toEqual({})
  })
})

describe('HarnessClient scenarios()', () => {
  it('accepts a bare array', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse([{ id: 'a' }, { id: 'b' }])) as unknown as typeof fetch
    const list = await new HarnessClient(BASE, { fetchImpl }).scenarios()
    expect(list.map((s) => s.id)).toEqual(['a', 'b'])
  })

  it('accepts the {scenarios: [...]} envelope the live harness returns', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ scenarios: [{ id: 'a' }] })) as unknown as typeof fetch
    const list = await new HarnessClient(BASE, { fetchImpl }).scenarios()
    expect(list.map((s) => s.id)).toEqual(['a'])
  })
})

describe('HarnessClient conversation routes', () => {
  it('createRun surfaces conversation_id when the harness returns one', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ run_id: 'r_1', conversation_id: 'c_1' })) as unknown as typeof fetch
    const res = await new HarnessClient(BASE, { fetchImpl }).createRun({ scenario_id: 'lost-ack' })
    expect(res).toEqual({ run_id: 'r_1', conversation_id: 'c_1' })
  })

  it('hits the documented paths with the documented methods and bodies', async () => {
    const fetchImpl = vi.fn(async (url: string | URL, init?: RequestInit) => {
      const u = String(url)
      if (u.endsWith('/me')) return jsonResponse({ user_id: UID, conversations: 1 })
      if (u.endsWith('/conversations') && init?.method === 'POST') {
        return jsonResponse({ id: 'c_new', user_id: UID, scenario_id: 'lost-ack', title: 't' })
      }
      if (u.endsWith('/conversations')) return jsonResponse([{ id: 'c_1', title: 'one', last_run: null }])
      if (u.endsWith('/conversations/c_1/runs')) return jsonResponse({ run_id: 'r_2', conversation_id: 'c_1' })
      if (u.endsWith('/conversations/c_1') && init?.method === 'PATCH') {
        return jsonResponse({ id: 'c_1', title: 'renamed' })
      }
      if (u.endsWith('/conversations/c_1') && init?.method === 'DELETE') return jsonResponse({ archived: true })
      if (u.endsWith('/conversations/c_1')) {
        return jsonResponse({ conversation: { id: 'c_1' }, runs: [], messages: [] })
      }
      return jsonResponse({}, false, 500)
    }) as unknown as typeof fetch

    const c = new HarnessClient(BASE, { userId: UID, fetchImpl })
    expect((await c.me()).conversations).toBe(1)
    expect((await c.listConversations())[0]!.id).toBe('c_1')
    expect((await c.getConversation('c_1')).conversation.id).toBe('c_1')
    expect((await c.createConversation({ scenario_id: 'lost-ack', title: 't' })).id).toBe('c_new')
    expect((await c.createConversationRun('c_1', { model: 'claude-sonnet-5', seed: 3 })).run_id).toBe('r_2')
    expect((await c.updateConversation('c_1', { title: 'renamed' })).title).toBe('renamed')
    expect((await c.archiveConversation('c_1')).archived).toBe(true)

    const all = calls(fetchImpl)
    expect(all.map(([u, i]) => `${i.method ?? 'GET'} ${u.slice(BASE.length)}`)).toEqual([
      'GET /me',
      'GET /conversations',
      'GET /conversations/c_1',
      'POST /conversations',
      'POST /conversations/c_1/runs',
      'PATCH /conversations/c_1',
      'DELETE /conversations/c_1',
    ])
    expect(JSON.parse(String(all[3]![1].body))).toEqual({ scenario_id: 'lost-ack', title: 't' })
    expect(JSON.parse(String(all[4]![1].body))).toEqual({ model: 'claude-sonnet-5', seed: 3 })
    expect(JSON.parse(String(all[5]![1].body))).toEqual({ title: 'renamed' })
    expect(all[6]![1].body).toBeUndefined()
    for (const [, init] of all) expect(headerOf(init, USER_HEADER)).toBe(UID)
  })

  it('URL-encodes ids', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ conversation: {}, runs: [], messages: [] })) as unknown as typeof fetch
    await new HarnessClient(BASE, { fetchImpl }).getConversation('c/1 2')
    expect(calls(fetchImpl)[0]![0]).toBe(`${BASE}/conversations/c%2F1%202`)
  })
})

describe('isNotFound', () => {
  it('is true only for a HarnessError with status 404', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ detail: 'Not Found' }, false, 404)) as unknown as typeof fetch
    const c = new HarnessClient(BASE, { userId: UID, fetchImpl })
    let caught: unknown
    try {
      await c.me()
    } catch (err) {
      caught = err
    }
    expect(isNotFound(caught)).toBe(true)
    expect(isNotFound(new HarnessError('x', 500))).toBe(false)
    expect(isNotFound(new HarnessError('x'))).toBe(false)
    expect(isNotFound(new Error('404'))).toBe(false)
    expect(isNotFound(null)).toBe(false)
  })
})

// --------------------------------------------------------------------------- SSE

describe('subscribeRun over SSE', () => {
  it('delivers parsed events and de-duplicates replayed ids', () => {
    const onEvents = vi.fn()
    const clock = makeClock()
    const sub = subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents,
      eventSourceImpl: FakeEventSource as unknown as typeof EventSource,
      ...clock,
    })

    FakeEventSource.last.deliver(mkEvent(0))
    FakeEventSource.last.deliver(mkEvent(1))
    FakeEventSource.last.deliver(mkEvent(1)) // replay after a reconnect
    FakeEventSource.last.emit('message', 'not json')

    expect(onEvents).toHaveBeenCalledTimes(2)
    expect(onEvents.mock.calls.map((c) => (c[0] as Event[])[0]!.id)).toEqual([0, 1])
    sub.close()
    expect(FakeEventSource.last.closed).toBe(true)
  })

  it('opens without last_event_id, and resumes with it after the ~110 s rotation', async () => {
    const clock = makeClock()
    const transports: TransportState[] = []
    subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents: () => {},
      onTransport: (t) => transports.push({ ...t }),
      eventSourceImpl: FakeEventSource as unknown as typeof EventSource,
      ...clock,
    })

    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.last.url).toBe(`${BASE}/runs/r_1/events`)

    // The stream delivered something, then the server closed it: that is a rotation, not a failure.
    FakeEventSource.last.deliver(mkEvent(7))
    FakeEventSource.last.emit('error')
    expect(FakeEventSource.instances[0]!.closed).toBe(true)

    await clock.tick()
    expect(FakeEventSource.instances).toHaveLength(2)
    expect(FakeEventSource.last.url).toBe(`${BASE}/runs/r_1/events?last_event_id=7`)
    expect(transports.every((t) => t.mode !== 'poll')).toBe(true)
  })

  it('honours an explicit lastEventId on the first open', () => {
    const clock = makeClock()
    subscribeRun({
      base: BASE,
      runId: 'r_1',
      lastEventId: 12,
      onEvents: () => {},
      eventSourceImpl: FakeEventSource as unknown as typeof EventSource,
      ...clock,
    })
    expect(FakeEventSource.last.url).toBe(`${BASE}/runs/r_1/events?last_event_id=12`)
  })

  it('stops on `event: done` and reports it once', () => {
    const onDone = vi.fn()
    const clock = makeClock()
    subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents: () => {},
      onDone,
      eventSourceImpl: FakeEventSource as unknown as typeof EventSource,
      ...clock,
    })
    FakeEventSource.last.emit('done')
    FakeEventSource.last.emit('done')
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(clock.pending()).toBe(0)
  })

  it('stops on `done` with reason=finished', () => {
    const onDone = vi.fn()
    const transports: TransportState[] = []
    const clock = makeClock()
    subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents: () => {},
      onDone,
      onTransport: (t) => transports.push({ ...t }),
      eventSourceImpl: FakeEventSource as unknown as typeof EventSource,
      ...clock,
    })
    FakeEventSource.last.deliver(mkEvent(0))
    FakeEventSource.last.emit(
      'done',
      JSON.stringify({ reason: 'finished', status: 'ok', last_event_id: 0, run_id: 'r_1' }),
    )
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.last.closed).toBe(true)
    expect(clock.pending()).toBe(0)
    expect(transports.at(-1)!.mode).toBe('closed')
  })

  it('reconnects with the resume point on `done` with reason=window, errors staying 0', async () => {
    const onDone = vi.fn()
    const onEvents = vi.fn()
    const transports: TransportState[] = []
    const clock = makeClock()
    subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents,
      onDone,
      onTransport: (t) => transports.push({ ...t }),
      eventSourceImpl: FakeEventSource as unknown as typeof EventSource,
      ...clock,
    })
    FakeEventSource.last.deliver(mkEvent(0))
    FakeEventSource.last.deliver(mkEvent(1))
    FakeEventSource.last.emit(
      'done',
      JSON.stringify({ reason: 'window', status: 'running', last_event_id: 1, run_id: 'r_1' }),
    )
    expect(onDone).not.toHaveBeenCalled()
    expect(FakeEventSource.instances[0]!.closed).toBe(true)
    expect(clock.pending()).toBe(1)

    await clock.tick()
    expect(FakeEventSource.instances).toHaveLength(2)
    expect(FakeEventSource.last.url).toBe(`${BASE}/runs/r_1/events?last_event_id=1`)
    expect(transports.every((t) => t.errors === 0 && t.mode !== 'poll')).toBe(true)

    // A late `error` from the old stream (the browser noticing the close) must be ignored.
    FakeEventSource.instances[0]!.emit('error')
    expect(transports.every((t) => t.errors === 0)).toBe(true)
    expect(clock.pending()).toBe(0)

    // The new window continues where the old one left off.
    FakeEventSource.last.deliver(mkEvent(1)) // replayed by the server, de-duplicated here
    FakeEventSource.last.deliver(mkEvent(2))
    expect(onEvents.mock.calls.map((c) => (c[0] as Event[])[0]!.id)).toEqual([0, 1, 2])

    FakeEventSource.last.emit('done', JSON.stringify({ reason: 'finished', status: 'ok' }))
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('treats a window-closed `done` that carries a terminal status as finished', () => {
    const onDone = vi.fn()
    const clock = makeClock()
    subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents: () => {},
      onDone,
      eventSourceImpl: FakeEventSource as unknown as typeof EventSource,
      ...clock,
    })
    FakeEventSource.last.emit('done', JSON.stringify({ reason: 'window', status: 'error' }))
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(clock.pending()).toBe(0)
  })

  it('treats unparseable `done` data as finished', () => {
    const onDone = vi.fn()
    const clock = makeClock()
    subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents: () => {},
      onDone,
      eventSourceImpl: FakeEventSource as unknown as typeof EventSource,
      ...clock,
    })
    FakeEventSource.last.emit('done', '{not json')
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(clock.pending()).toBe(0)
  })

  it('listens for the llm.call and turn.thinking named frames', () => {
    const onEvents = vi.fn()
    const clock = makeClock()
    subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents,
      eventSourceImpl: FakeEventSource as unknown as typeof EventSource,
      ...clock,
    })
    FakeEventSource.last.emit(
      'llm.call',
      JSON.stringify(mkEvent(0, 'llm.call', { attempt: 1, model: 'm', usage: { input_tokens: 1, output_tokens: 1 }, duration_ms: 5 })),
    )
    FakeEventSource.last.emit('turn.thinking', JSON.stringify(mkEvent(1, 'turn.thinking', { text: 'hmm' })))
    expect(onEvents.mock.calls.map((c) => (c[0] as Event[])[0]!.type)).toEqual(['llm.call', 'turn.thinking'])
  })

  it('sends the caller-supplied headers on the polling fetch', async () => {
    const clock = makeClock()
    const fetchImpl = vi.fn(async () => jsonResponse(record({ status: 'ok' }))) as unknown as typeof fetch
    subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents: () => {},
      headers: { [USER_HEADER]: UID },
      eventSourceImpl: null,
      fetchImpl,
      ...clock,
    })
    await settle()
    const init = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0]![1] as RequestInit
    expect((init.headers as Record<string, string>)[USER_HEADER]).toBe(UID)
    expect(init.credentials).toBe('omit')
  })
  it('retries once, then falls back to polling after two failures with no progress', async () => {
    const onEvents = vi.fn()
    const onDone = vi.fn()
    const transports: TransportState[] = []
    const clock = makeClock()
    const fetchImpl = vi.fn(async () =>
      jsonResponse(record({ status: 'ok', events: [mkEvent(0), mkEvent(1)] })),
    ) as unknown as typeof fetch

    subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents,
      onDone,
      onTransport: (t) => transports.push({ ...t }),
      eventSourceImpl: FakeEventSource as unknown as typeof EventSource,
      fetchImpl,
      ...clock,
    })

    // First failure, nothing delivered -> one retry is scheduled.
    FakeEventSource.last.emit('error')
    expect(transports.at(-1)!.errors).toBe(1)
    await clock.tick()
    expect(FakeEventSource.instances).toHaveLength(2)

    // Second failure, still nothing delivered -> demote to polling.
    FakeEventSource.last.emit('error')
    await settle()
    expect(transports.some((t) => t.mode === 'poll')).toBe(true)
    expect(fetchImpl).toHaveBeenCalled()
    expect(onEvents).toHaveBeenCalledTimes(1)
    expect((onEvents.mock.calls[0]![0] as Event[]).map((e) => e.id)).toEqual([0, 1])
    // The polled record was terminal, so the subscription closed itself.
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(transports.at(-1)!.mode).toBe('closed')
  })

  it('keeps polling while the run is not terminal', async () => {
    const onEvents = vi.fn()
    const onDone = vi.fn()
    const clock = makeClock()
    let nth = 0
    const fetchImpl = vi.fn(async () => {
      nth += 1
      return nth === 1
        ? jsonResponse(record({ status: 'running', events: [mkEvent(0)] }))
        : jsonResponse(record({ status: 'ok', events: [mkEvent(0), mkEvent(1)] }))
    }) as unknown as typeof fetch

    subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents,
      onDone,
      eventSourceImpl: null, // no EventSource in this environment -> straight to polling
      fetchImpl,
      ...clock,
    })

    await settle()
    expect(onEvents).toHaveBeenCalledTimes(1)
    expect(onDone).not.toHaveBeenCalled()
    expect(clock.pending()).toBe(1) // the next poll is scheduled

    await clock.tick()
    expect((onEvents.mock.calls[1]![0] as Event[]).map((e) => e.id)).toEqual([1])
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('survives a failing poll and schedules another', async () => {
    const clock = makeClock()
    const fetchImpl = vi.fn(async () => {
      throw new Error('offline')
    }) as unknown as typeof fetch

    const sub = subscribeRun({
      base: BASE,
      runId: 'r_1',
      onEvents: () => {},
      eventSourceImpl: null,
      fetchImpl,
      ...clock,
    })
    await settle()
    expect(clock.pending()).toBe(1)
    expect(sub.state().mode).toBe('poll')
    sub.close()
    expect(clock.pending()).toBe(0)
  })
})

describe('normaliseScenarios (contract deviation: /scenarios envelope)', () => {
  const one = {
    id: 'lost-ack',
    title: 't',
    description: 'd',
    task_prompt: 'p',
    max_steps: 20,
    fault_kinds: ['ack_lost'] as const,
  }

  it('passes a bare array through (the documented shape)', () => {
    expect(normaliseScenarios([one])).toEqual([one])
  })

  it('unwraps {scenarios: [...]} (what the deployed harness actually returns)', () => {
    expect(normaliseScenarios({ scenarios: [one] })).toEqual([one])
  })

  it('returns [] for anything else rather than throwing', () => {
    expect(normaliseScenarios(null)).toEqual([])
    expect(normaliseScenarios({ nope: 1 })).toEqual([])
    expect(normaliseScenarios('x')).toEqual([])
  })
})

describe('done payload helpers', () => {
  it('parseDone tolerates missing, empty and malformed data', () => {
    expect(parseDone(undefined)).toBeNull()
    expect(parseDone('')).toBeNull()
    expect(parseDone('[1]')).toBeNull()
    expect(parseDone('nope')).toBeNull()
    expect(parseDone('{"reason":"window","status":"running"}')).toEqual({ reason: 'window', status: 'running' })
  })

  it('doneMeansReconnect is true only for reason=window with a non-terminal status', () => {
    expect(doneMeansReconnect(null)).toBe(false)
    expect(doneMeansReconnect({ reason: 'finished', status: 'ok' })).toBe(false)
    expect(doneMeansReconnect({ reason: 'window', status: 'running' })).toBe(true)
    expect(doneMeansReconnect({ reason: 'window', status: 'queued' })).toBe(true)
    expect(doneMeansReconnect({ reason: 'window' })).toBe(true)
    expect(doneMeansReconnect({ reason: 'window', status: 'ok' })).toBe(false)
    expect(doneMeansReconnect({ reason: 'window', status: 'truncated' })).toBe(false)
    expect(doneMeansReconnect({ reason: 'something-new' })).toBe(false)
  })
})

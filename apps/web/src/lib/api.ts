/**
 * Typed harness client: plain cross-origin JSON fetches plus a resilient run subscription.
 *
 * Reliability model (this is the interesting part):
 *  - The harness closes every SSE stream at ~110 s to stay under the hosting platform's 150 s request cap, so a
 *    clean close mid-run is NORMAL and must not be treated as a failure. The server announces it
 *    with `event: done` + `{reason:"window"}`; an older harness just closes, and we distinguish
 *    that from a real failure by whether the stream delivered anything first.
 *  - EventSource cannot set headers, so the resume point is passed both natively (the browser
 *    sends `Last-Event-ID` on its own reconnect) and as a `last_event_id` query param on ours.
 *    For the same reason the SSE request carries no `X-Faultline-User`; ownership is enforced on
 *    the JSON routes, and a run id is unguessable.
 *  - After two consecutive failed opens we stop trusting SSE and fall back to polling
 *    GET /runs/{id} every 1.5 s, which is strictly less efficient but works through anything.
 *  - No credentials are ever sent: the web origin and the harness origin are different
 *    subdomains and these are ordinary CORS requests. Identity travels as a header instead
 *    (ARCHITECTURE.md §6).
 */

import { getLogger } from './log'
import type {
  ArchiveResponse,
  Conversation,
  ConversationDetail,
  ConversationRunRequest,
  ConversationRunResponse,
  ConversationSummary,
  CreateConversationRequest,
  CreateRunResponse,
  Event,
  EventType,
  Health,
  MeResponse,
  RunRecord,
  RunRequest,
  RunStatus,
  Scenario,
  UpdateConversationRequest,
} from './types'
import { isTerminal } from './reducer'

const log = getLogger('web')

export const USER_HEADER = 'X-Faultline-User'

const EVENT_TYPES: EventType[] = [
  'run.started',
  'episode.reset',
  'turn.text',
  'tool.call',
  'tool.result',
  'fault.fired',
  'workspace.diff',
  'episode.evaluated',
  'run.finished',
  'log',
  'llm.call',
  'turn.thinking',
  'interruption',
  'run.resumed',
  'episode.sandbox',
]

export class HarnessError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly url?: string,
  ) {
    super(message)
    this.name = 'HarnessError'
  }
}

/** True for a HarnessError carrying HTTP 404 — the harness's "not yours / not there". */
export function isNotFound(err: unknown): boolean {
  return err instanceof HarnessError && err.status === 404
}

type HeaderMap = Record<string, string>

function toHeaderMap(h: HeadersInit | undefined): HeaderMap {
  if (!h) return {}
  if (Array.isArray(h)) return Object.fromEntries(h)
  if (typeof Headers !== 'undefined' && h instanceof Headers) {
    const out: HeaderMap = {}
    h.forEach((v, k) => {
      out[k] = v
    })
    return out
  }
  return { ...(h as HeaderMap) }
}

async function jsonFetch<T>(
  url: string,
  init: RequestInit = {},
  fetchImpl: typeof fetch = fetch,
  headers: HeaderMap = {},
): Promise<T> {
  const t0 = Date.now()
  let res: Response
  try {
    res = await fetchImpl(url, {
      ...init,
      credentials: 'omit',
      headers: { accept: 'application/json', ...headers, ...toHeaderMap(init.headers) },
    })
  } catch (err) {
    log.error('http.error', 'request failed', { url, error: String(err) })
    throw new HarnessError(`network error contacting ${url}: ${String(err)}`, undefined, url)
  }
  const durMs = Date.now() - t0
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    // 404 is an expected answer for routes a harness build may not have yet (/me, /conversations).
    log[res.status === 404 ? 'warn' : 'error']('http.response', 'non-2xx', {
      url,
      status: res.status,
      dur_ms: durMs,
    })
    throw new HarnessError(
      `${res.status} ${res.statusText} from ${url}${body ? `: ${body.slice(0, 400)}` : ''}`,
      res.status,
      url,
    )
  }
  log.debug('http.response', 'ok', { url, status: res.status, dur_ms: durMs })
  return (await res.json()) as T
}

/**
 * PLAN.md 2.3 specifies `GET /scenarios -> [Scenario]`, but the deployed harness answers
 * `{"scenarios": [...]}`. Rather than break on either, accept both shapes here — this is the one
 * place in the app that has to know, and a client that tolerates an envelope costs nothing.
 * Logged as a contract deviation.
 */
export function normaliseScenarios(body: unknown): Scenario[] {
  if (Array.isArray(body)) return body as Scenario[]
  if (body && typeof body === 'object') {
    const inner = (body as { scenarios?: unknown }).scenarios
    if (Array.isArray(inner)) {
      log.debug('scenarios.envelope', 'harness returned {scenarios: [...]}, unwrapping', {
        count: inner.length,
      })
      return inner as Scenario[]
    }
  }
  log.warn('scenarios.shape', 'unrecognised /scenarios payload; treating as empty', {
    got: typeof body,
  })
  return []
}

/** Same tolerance as normaliseScenarios, for any list route: bare array or `{<key>: [...]}`. */
function normaliseList<T>(body: unknown, key: string): T[] {
  if (Array.isArray(body)) return body as T[]
  if (body && typeof body === 'object') {
    const inner = (body as Record<string, unknown>)[key]
    if (Array.isArray(inner)) {
      log.debug(`${key}.envelope`, `harness returned {${key}: [...]}, unwrapping`, { count: inner.length })
      return inner as T[]
    }
  }
  log.warn(`${key}.shape`, `unrecognised /${key} payload; treating as empty`, { got: typeof body })
  return []
}

export interface HarnessClientOptions {
  /** Anonymous browser id (identity.ts). Sent as `X-Faultline-User` on every request when set. */
  userId?: string | null
  fetchImpl?: typeof fetch
}

export class HarnessClient {
  readonly base: string
  readonly userId: string | null
  private readonly fetchImpl: typeof fetch

  /**
   * `new HarnessClient(base, { userId, fetchImpl })`. The older positional form
   * `new HarnessClient(base, fetchImpl)` is still accepted.
   */
  constructor(base: string, opts: HarnessClientOptions | typeof fetch = {}) {
    this.base = base
    if (typeof opts === 'function') {
      this.fetchImpl = opts
      this.userId = null
    } else {
      this.fetchImpl = opts.fetchImpl ?? fetch
      this.userId = opts.userId ?? null
    }
  }

  /** Headers every JSON request carries (identity). Also used by the polling fallback. */
  headers(): HeaderMap {
    return this.userId ? { [USER_HEADER]: this.userId } : {}
  }

  private get<T>(path: string, signal?: AbortSignal): Promise<T> {
    return jsonFetch<T>(`${this.base}${path}`, { signal }, this.fetchImpl, this.headers())
  }

  private send<T>(method: 'POST' | 'PATCH' | 'DELETE', path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    return jsonFetch<T>(
      `${this.base}${path}`,
      {
        method,
        ...(body === undefined
          ? {}
          : { headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }),
        signal,
      },
      this.fetchImpl,
      this.headers(),
    )
  }

  // ---------------------------------------------------------------- health / catalogue

  health(signal?: AbortSignal): Promise<Health> {
    return this.get<Health>('/health', signal)
  }

  async scenarios(signal?: AbortSignal): Promise<Scenario[]> {
    const body = await this.get<Scenario[] | { scenarios?: Scenario[] }>('/scenarios', signal)
    return normaliseScenarios(body)
  }

  // ---------------------------------------------------------------- runs

  createRun(req: RunRequest, signal?: AbortSignal): Promise<CreateRunResponse> {
    log.info('run.create', 'POST /runs', {
      scenario_id: req.scenario_id,
      model: req.model ?? null,
    })
    return this.send<CreateRunResponse>('POST', '/runs', req, signal)
  }

  getRun(runId: string, signal?: AbortSignal): Promise<RunRecord> {
    return this.get<RunRecord>(`/runs/${encodeURIComponent(runId)}`, signal)
  }

  // ---------------------------------------------------------------- identity / conversations
  // The harness answers 404 both for an unknown id and for one owned by another browser identity
  // (no existence oracle); callers use isNotFound() to say "not found", never to fall back.

  me(signal?: AbortSignal): Promise<MeResponse> {
    return this.get<MeResponse>('/me', signal)
  }

  async listConversations(signal?: AbortSignal): Promise<ConversationSummary[]> {
    const body = await this.get<ConversationSummary[] | { conversations?: ConversationSummary[] }>(
      '/conversations',
      signal,
    )
    return normaliseList(body, 'conversations')
  }

  getConversation(id: string, signal?: AbortSignal): Promise<ConversationDetail> {
    return this.get<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`, signal)
  }

  createConversation(req: CreateConversationRequest, signal?: AbortSignal): Promise<Conversation> {
    log.info('conversation.create', 'POST /conversations', { scenario_id: req.scenario_id })
    return this.send<Conversation>('POST', '/conversations', req, signal)
  }

  createConversationRun(
    id: string,
    req: ConversationRunRequest,
    signal?: AbortSignal,
  ): Promise<ConversationRunResponse> {
    log.info('conversation.run', 'POST /conversations/{id}/runs', {
      conversation_id: id,
      model: req.model ?? null,
    })
    return this.send<ConversationRunResponse>(
      'POST',
      `/conversations/${encodeURIComponent(id)}/runs`,
      req,
      signal,
    )
  }

  updateConversation(
    id: string,
    patch: UpdateConversationRequest,
    signal?: AbortSignal,
  ): Promise<Conversation> {
    return this.send<Conversation>('PATCH', `/conversations/${encodeURIComponent(id)}`, patch, signal)
  }

  /** Soft archive (the harness never hard-deletes). */
  archiveConversation(id: string, signal?: AbortSignal): Promise<ArchiveResponse> {
    log.info('conversation.archive', 'DELETE /conversations/{id}', { conversation_id: id })
    return this.send<ArchiveResponse>('DELETE', `/conversations/${encodeURIComponent(id)}`, undefined, signal)
  }
}

// ------------------------------------------------------------------ run subscription

export type TransportMode = 'sse' | 'poll' | 'closed'

export interface TransportState {
  mode: TransportMode
  /** Consecutive failed SSE opens. Reset whenever a stream delivers an event. */
  errors: number
  detail?: string
}

export interface SubscribeOptions {
  base: string
  runId: string
  /** Resume point; -1 means "from the beginning". */
  lastEventId?: number
  onEvents: (events: Event[]) => void
  onRecord?: (record: RunRecord) => void
  onTransport?: (state: TransportState) => void
  onDone?: () => void
  /** Extra headers for the polling fetch (identity). EventSource cannot carry them. */
  headers?: Record<string, string>
  /** Injection points for tests. */
  eventSourceImpl?: typeof EventSource | null
  fetchImpl?: typeof fetch
  pollIntervalMs?: number
  reconnectDelayMs?: number
  maxSseErrors?: number
  setTimeoutImpl?: (fn: () => void, ms: number) => unknown
  clearTimeoutImpl?: (handle: unknown) => void
}

export interface Subscription {
  close: () => void
  /** Test/debug introspection. */
  state: () => TransportState
}

function parseEvent(raw: string): Event | null {
  try {
    const v = JSON.parse(raw) as Event
    return typeof v?.id === 'number' && typeof v?.type === 'string' ? v : null
  } catch {
    return null
  }
}

/** Payload of the server's `event: done` frame. Older harness builds send no data at all. */
export interface DonePayload {
  reason?: 'finished' | 'window' | string
  status?: RunStatus | string | null
  last_event_id?: number
  run_id?: string
}

export function parseDone(raw: unknown): DonePayload | null {
  if (typeof raw !== 'string' || raw.length === 0) return null
  try {
    const v = JSON.parse(raw) as unknown
    return v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as DonePayload) : null
  } catch {
    return null
  }
}

/**
 * Does this `done` frame end the run, or merely the current ~110 s window?
 * Only an explicit `reason: "window"` with a non-terminal status means "reconnect"; anything
 * ambiguous (no data, unparseable, unknown reason, terminal status) is treated as finished, which
 * is the safe default — a wrongly-ended subscription can be re-opened, a wrongly-kept one spins.
 */
export function doneMeansReconnect(payload: DonePayload | null): boolean {
  if (!payload) return false
  if (payload.reason !== 'window') return false
  return !isTerminal((payload.status ?? null) as RunStatus | null)
}

/**
 * Tail a run. Returns immediately; every update arrives through the callbacks.
 * Always call `close()` — it tears down the EventSource and any pending timer.
 */
export function subscribeRun(opts: SubscribeOptions): Subscription {
  const {
    base,
    runId,
    onEvents,
    onRecord,
    onTransport,
    onDone,
    headers = {},
    fetchImpl = typeof fetch !== 'undefined' ? fetch : undefined,
    pollIntervalMs = 1500,
    reconnectDelayMs = 1000,
    maxSseErrors = 2,
    setTimeoutImpl = (fn, ms) => setTimeout(fn, ms),
    clearTimeoutImpl = (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  } = opts

  const ESImpl =
    opts.eventSourceImpl !== undefined
      ? opts.eventSourceImpl
      : typeof EventSource !== 'undefined'
        ? EventSource
        : null

  let lastEventId = opts.lastEventId ?? -1
  let closed = false
  let es: EventSource | null = null
  let timer: unknown = null
  let transport: TransportState = { mode: 'sse', errors: 0 }

  const setTransport = (patch: Partial<TransportState>) => {
    transport = { ...transport, ...patch }
    onTransport?.(transport)
  }

  const clearTimer = () => {
    if (timer !== null) {
      clearTimeoutImpl(timer)
      timer = null
    }
  }

  const emit = (events: Event[]) => {
    const fresh = events.filter((e) => e.id > lastEventId)
    if (fresh.length === 0) return
    lastEventId = Math.max(lastEventId, ...fresh.map((e) => e.id))
    onEvents(fresh)
  }

  const finish = (reason: string) => {
    if (closed) return
    log.info('stream.done', reason, { run_id: runId, last_event_id: lastEventId })
    closed = true
    es?.close()
    es = null
    clearTimer()
    setTransport({ mode: 'closed', detail: reason })
    onDone?.()
  }

  // ------------------------------------------------ polling fallback

  const pollOnce = async () => {
    if (closed || !fetchImpl) return
    try {
      const rec = await jsonFetch<RunRecord>(
        `${base}/runs/${encodeURIComponent(runId)}`,
        {},
        fetchImpl,
        headers,
      )
      onRecord?.(rec)
      emit(rec.events ?? [])
      if (isTerminal(rec.status)) {
        finish('run reached a terminal status (poll)')
        return
      }
    } catch (err) {
      log.warn('poll.error', 'GET /runs failed', { run_id: runId, error: String(err) })
      setTransport({ detail: String(err) })
    }
    if (!closed) timer = setTimeoutImpl(() => void pollOnce(), pollIntervalMs)
  }

  const startPolling = (why: string) => {
    if (closed) return
    es?.close()
    es = null
    clearTimer()
    log.warn('stream.fallback', `switching to polling: ${why}`, {
      run_id: runId,
      interval_ms: pollIntervalMs,
    })
    setTransport({ mode: 'poll', detail: why })
    void pollOnce()
  }

  // ------------------------------------------------ SSE

  const openSse = () => {
    if (closed || !ESImpl) return
    const url = new URL(`${base}/runs/${encodeURIComponent(runId)}/events`)
    if (lastEventId >= 0) url.searchParams.set('last_event_id', String(lastEventId))
    let deliveredThisStream = 0

    log.info('stream.open', 'opening SSE', { run_id: runId, url: url.toString() })
    const source = new ESImpl(url.toString())
    es = source

    /** Drop this stream and open a fresh one at the resume point; the error counter stays 0. */
    const rotate = (why: string) => {
      source.close()
      if (es === source) es = null
      log.info('stream.rotate', why, {
        run_id: runId,
        delivered: deliveredThisStream,
        last_event_id: lastEventId,
      })
      setTransport({ errors: 0 })
      timer = setTimeoutImpl(openSse, 0)
    }

    const handle = (e: MessageEvent) => {
      const ev = parseEvent(e.data as string)
      if (!ev) {
        log.warn('stream.parse_error', 'unparseable SSE frame', { run_id: runId })
        return
      }
      deliveredThisStream += 1
      if (transport.errors !== 0) setTransport({ errors: 0, detail: undefined })
      emit([ev])
      if (ev.type === 'run.finished') {
        // Let the server send `done`; if it does not, the close handler below stops us.
        log.info('stream.run_finished', 'run.finished seen', { run_id: runId })
      }
    }

    source.addEventListener('message', handle as EventListener)
    for (const t of EVENT_TYPES) source.addEventListener(t, handle as EventListener)
    source.addEventListener('done', ((e: MessageEvent) => {
      if (closed) return
      const payload = parseDone(e?.data)
      if (doneMeansReconnect(payload)) {
        rotate('server closed its ~110 s window (`done` reason=window); reconnecting')
        return
      }
      finish(
        payload?.reason
          ? `server sent \`event: done\` (reason=${payload.reason}, status=${String(payload.status ?? '?')})`
          : 'server sent `event: done`',
      )
    }) as EventListener)

    source.addEventListener('open', () => {
      log.info('stream.connected', 'SSE open', { run_id: runId })
      setTransport({ mode: 'sse' })
    })

    source.addEventListener('error', () => {
      if (closed) return
      if (es !== source) return // already rotated away from this stream
      if (deliveredThisStream > 0) {
        // Expected on an older harness: the stream closes at ~110 s with no `done` frame.
        rotate('stream closed after delivering events; reconnecting')
        return
      }
      source.close()
      es = null
      const errors = transport.errors + 1
      setTransport({ errors, detail: 'EventSource error' })
      log.warn('stream.error', 'SSE failed before delivering anything', {
        run_id: runId,
        errors,
      })
      if (errors >= maxSseErrors) {
        startPolling(`${errors} consecutive EventSource errors`)
        return
      }
      timer = setTimeoutImpl(openSse, reconnectDelayMs)
    })
  }

  if (ESImpl) {
    openSse()
  } else {
    startPolling('EventSource unavailable in this environment')
  }

  return {
    close: () => {
      if (closed) return
      closed = true
      es?.close()
      es = null
      clearTimer()
      setTransport({ mode: 'closed', detail: 'closed by caller' })
      log.info('stream.close', 'subscription closed by caller', { run_id: runId })
    },
    state: () => transport,
  }
}

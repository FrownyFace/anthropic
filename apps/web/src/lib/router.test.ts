import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  ROUTE_EVENT,
  currentRoute,
  hrefFor,
  navigate,
  parseRoute,
  sameRoute,
  subscribeRoute,
  useRoute,
  type Route,
} from './router'

afterEach(() => {
  window.history.replaceState({}, '', '/')
})

describe('parseRoute', () => {
  it('maps the path forms', () => {
    expect(parseRoute('/')).toEqual({ kind: 'home' })
    expect(parseRoute('')).toEqual({ kind: 'home' })
    expect(parseRoute('/conversations/c_1')).toEqual({ kind: 'conversation', id: 'c_1', runId: null })
    expect(parseRoute('/conversations/c_1', '?run=r_9')).toEqual({ kind: 'conversation', id: 'c_1', runId: 'r_9' })
    expect(parseRoute('/conversations/c_1/', 'run=r_9')).toEqual({ kind: 'conversation', id: 'c_1', runId: 'r_9' })
    expect(parseRoute('/runs/r_1')).toEqual({ kind: 'run', runId: 'r_1' })
    expect(parseRoute('/replay/lost-ack')).toEqual({ kind: 'replay', demoId: 'lost-ack' })
  })

  it('ignores query params on the root path (no legacy ?run= / ?demo= / ?c= routing)', () => {
    expect(parseRoute('/', '?run=r_1')).toEqual({ kind: 'home' })
    expect(parseRoute('/', '?demo=lost-ack')).toEqual({ kind: 'home' })
    expect(parseRoute('/', '?c=c_1')).toEqual({ kind: 'home' })
    expect(parseRoute('/', '?c=c_1&run=r_2')).toEqual({ kind: 'home' })
    expect(parseRoute('/', '?theme=dark')).toEqual({ kind: 'home' })
    // Only /conversations/:id reads ?run=; other path routes ignore the query.
    expect(parseRoute('/runs/r_1', '?demo=x')).toEqual({ kind: 'run', runId: 'r_1' })
  })

  it('falls back to home for anything it does not know', () => {
    expect(parseRoute('/nope')).toEqual({ kind: 'home' })
    expect(parseRoute('/runs')).toEqual({ kind: 'home' })
    expect(parseRoute('/runs/')).toEqual({ kind: 'home' })
    expect(parseRoute('/runs/r_1/extra')).toEqual({ kind: 'home' })
    expect(parseRoute('/conversations/c_1/runs/r_1')).toEqual({ kind: 'home' })
    expect(parseRoute('/', '?run=')).toEqual({ kind: 'home' })
  })

  it('decodes percent-encoded ids and tolerates broken encodings', () => {
    expect(parseRoute('/runs/r%2F1')).toEqual({ kind: 'run', runId: 'r/1' })
    expect(parseRoute('/runs/%E0%A4%A')).toEqual({ kind: 'run', runId: '%E0%A4%A' })
  })
})

describe('hrefFor / parseRoute round trip', () => {
  const routes: Route[] = [
    { kind: 'home' },
    { kind: 'conversation', id: 'c_1' },
    { kind: 'conversation', id: 'c_1', runId: null },
    { kind: 'conversation', id: 'c_1', runId: 'r_2' },
    { kind: 'conversation', id: 'c/odd id', runId: 'r?2' },
    { kind: 'run', runId: 'r_1' },
    { kind: 'replay', demoId: 'lost-ack' },
  ]

  it.each(routes)('%o survives href → parse', (route) => {
    const href = hrefFor(route)
    const url = new URL(href, 'http://localhost')
    const back = parseRoute(url.pathname, url.search)
    const expected: Route =
      route.kind === 'conversation' ? { ...route, runId: route.runId ?? null } : route
    expect(back).toEqual(expected)
  })

  it('emits the path form', () => {
    expect(hrefFor({ kind: 'home' })).toBe('/')
    expect(hrefFor({ kind: 'conversation', id: 'c_1' })).toBe('/conversations/c_1')
    expect(hrefFor({ kind: 'conversation', id: 'c_1', runId: 'r_2' })).toBe('/conversations/c_1?run=r_2')
    expect(hrefFor({ kind: 'run', runId: 'r_1' })).toBe('/runs/r_1')
    expect(hrefFor({ kind: 'replay', demoId: 'lost-ack' })).toBe('/replay/lost-ack')
  })

  it('sameRoute compares structurally', () => {
    expect(sameRoute({ kind: 'conversation', id: 'c_1' }, { kind: 'conversation', id: 'c_1', runId: null })).toBe(true)
    expect(sameRoute({ kind: 'run', runId: 'a' }, { kind: 'run', runId: 'b' })).toBe(false)
  })
})

describe('navigate', () => {
  it('pushes the href, updates currentRoute() and notifies subscribers', () => {
    const fn = vi.fn()
    const off = subscribeRoute(fn)
    const before = window.history.length

    navigate({ kind: 'run', runId: 'r_1' })
    expect(window.location.pathname).toBe('/runs/r_1')
    expect(currentRoute()).toEqual({ kind: 'run', runId: 'r_1' })
    expect(fn).toHaveBeenCalledTimes(1)
    expect(window.history.length).toBe(before + 1)

    off()
    navigate({ kind: 'home' })
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('replaces instead of pushing when asked, or when the target is the current location', () => {
    navigate({ kind: 'run', runId: 'r_1' })
    const len = window.history.length
    navigate({ kind: 'conversation', id: 'c_1' }, { replace: true })
    expect(window.location.pathname).toBe('/conversations/c_1')
    expect(window.history.length).toBe(len)
    navigate({ kind: 'conversation', id: 'c_1' })
    expect(window.history.length).toBe(len)
  })

  it('dispatches the custom route event carrying the route', () => {
    const seen: Route[] = []
    const handler = (e: Event) => seen.push((e as CustomEvent<Route>).detail)
    window.addEventListener(ROUTE_EVENT, handler)
    navigate({ kind: 'replay', demoId: 'lost-ack' })
    window.removeEventListener(ROUTE_EVENT, handler)
    expect(seen).toEqual([{ kind: 'replay', demoId: 'lost-ack' }])
  })
})

describe('useRoute', () => {
  it('tracks navigate() and popstate, with a stable snapshot for an unchanged location', () => {
    window.history.replaceState({}, '', '/conversations/c_1?run=r_1')
    const { result, rerender } = renderHook(() => useRoute())
    expect(result.current).toEqual({ kind: 'conversation', id: 'c_1', runId: 'r_1' })

    const first = result.current
    rerender()
    expect(result.current).toBe(first)

    act(() => navigate({ kind: 'run', runId: 'r_2' }))
    expect(result.current).toEqual({ kind: 'run', runId: 'r_2' })

    act(() => {
      window.history.replaceState({}, '', '/replay/lost-ack')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    expect(result.current).toEqual({ kind: 'replay', demoId: 'lost-ack' })
  })
})

/**
 * Path-based routes with no router dependency.
 *
 *   /                         home
 *   /conversations/:id[?run=] a persisted conversation, optionally focused on one of its runs
 *   /runs/:id                 a bare run (a run id without its conversation context)
 *   /replay/:demoId           a bundled replay, no backend
 *
 * Anything else is home. hrefFor() emits exactly these forms; serve.py answers unknown
 * extension-less paths with index.html, so deep links survive a refresh.
 *
 * Navigation is pushState/replaceState plus a custom event, so every useRoute() instance and any
 * plain listener re-reads the location; the browser's own popstate (back/forward) is honoured too.
 */

import { useSyncExternalStore } from 'react'

import { getLogger } from './log'

const log = getLogger('web')

export type Route =
  | { kind: 'home' }
  | { kind: 'conversation'; id: string; runId?: string | null }
  | { kind: 'run'; runId: string }
  | { kind: 'replay'; demoId: string }

/** Dispatched on `window` after every in-app navigation (in addition to the native `popstate`). */
export const ROUTE_EVENT = 'faultline:route'

const HOME: Route = { kind: 'home' }

function decode(s: string): string {
  try {
    return decodeURIComponent(s)
  } catch {
    return s
  }
}

function segments(pathname: string): string[] {
  return pathname.split('/').filter((s) => s.length > 0).map(decode)
}

function params(search: string): URLSearchParams {
  return new URLSearchParams(search.startsWith('?') ? search.slice(1) : search)
}

export function parseRoute(pathname: string, search = ''): Route {
  const segs = segments(pathname || '/')
  const q = params(search)

  if (segs.length === 0) return HOME

  if (segs.length === 2) {
    const [head, id] = segs as [string, string]
    if (id.length === 0) return HOME
    if (head === 'conversations') return { kind: 'conversation', id, runId: q.get('run') || null }
    if (head === 'runs') return { kind: 'run', runId: id }
    if (head === 'replay') return { kind: 'replay', demoId: id }
  }

  return HOME
}

export function hrefFor(route: Route): string {
  switch (route.kind) {
    case 'home':
      return '/'
    case 'conversation': {
      const base = `/conversations/${encodeURIComponent(route.id)}`
      return route.runId ? `${base}?run=${encodeURIComponent(route.runId)}` : base
    }
    case 'run':
      return `/runs/${encodeURIComponent(route.runId)}`
    case 'replay':
      return `/replay/${encodeURIComponent(route.demoId)}`
  }
}

// --------------------------------------------------------------------------- window binding

function locationKey(): string {
  return `${window.location.pathname}${window.location.search}`
}

export interface NavigateOptions {
  /** Replace the current history entry instead of pushing a new one. */
  replace?: boolean
}

export function navigate(route: Route, opts: NavigateOptions = {}): void {
  if (typeof window === 'undefined') return
  const href = hrefFor(route)
  // Pushing an entry identical to the current one only makes Back a no-op; replace instead.
  const replace = opts.replace === true || href === locationKey()
  try {
    if (replace) window.history.replaceState({}, '', href)
    else window.history.pushState({}, '', href)
  } catch (err) {
    // history can throw in sandboxed/srcdoc frames; the in-memory route still updates below.
    log.warn('route.history_error', 'history update failed', { href, error: String(err) })
  }
  log.info('route.change', replace ? 'replaced' : 'navigated', { href, kind: route.kind })
  window.dispatchEvent(new CustomEvent<Route>(ROUTE_EVENT, { detail: route }))
}

/** Fires on back/forward and after navigate(). */
export function subscribeRoute(fn: () => void): () => void {
  if (typeof window === 'undefined') return () => {}
  window.addEventListener('popstate', fn)
  window.addEventListener(ROUTE_EVENT, fn)
  return () => {
    window.removeEventListener('popstate', fn)
    window.removeEventListener(ROUTE_EVENT, fn)
  }
}

// Snapshot must be referentially stable for an unchanged location, or useSyncExternalStore
// would re-render forever.
let cache: { key: string; route: Route } | null = null

function getSnapshot(): Route {
  const key = locationKey()
  if (cache && cache.key === key) return cache.route
  cache = { key, route: parseRoute(window.location.pathname, window.location.search) }
  return cache.route
}

function getServerSnapshot(): Route {
  return HOME
}

/** The current Route; re-renders on navigate() and on the browser's back/forward. */
export function useRoute(): Route {
  return useSyncExternalStore(subscribeRoute, getSnapshot, getServerSnapshot)
}

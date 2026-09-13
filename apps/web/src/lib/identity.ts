/**
 * Anonymous browser identity (ARCHITECTURE.md §6).
 *
 * The id scopes conversations to a browser; it is not authentication. It is minted here as
 * `u_<uuid4>`, kept in localStorage (primary) and mirrored into a cookie (so it survives a
 * "clear site data → local storage" that leaves cookies), and travels to the harness as the
 * `X-Faultline-User` header — never as a cookie, because the harness lives on another origin.
 *
 * Every storage access is wrapped: a browser that blocks storage (private mode, a strict
 * extension) still gets a stable id for the session from the in-memory copy.
 */

import { getLogger } from './log'

const log = getLogger('web')

export const USER_KEY = 'faultline.user_id'
export const COOKIE_NAME = 'faultline_uid'
export const COOKIE_MAX_AGE_S = 31_536_000

const USER_ID_RE = /^u_[0-9a-f-]{36}$/

/** Session-scoped fallback for browsers that reject both storages; also the hook's snapshot. */
let memory: string | null = null

const listeners = new Set<() => void>()

/** Notified whenever the id changes (mint, restore from a different store, reset). */
export function subscribeIdentity(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

function notify(): void {
  for (const fn of listeners) fn()
}

export function isValidUserId(s: unknown): s is string {
  return typeof s === 'string' && USER_ID_RE.test(s)
}

function uuid4(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  // Older engines: assemble a v4 UUID from raw random bytes.
  const bytes = new Uint8Array(16)
  if (c && typeof c.getRandomValues === 'function') {
    c.getRandomValues(bytes)
  } else {
    for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256)
  }
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function mintUserId(): string {
  return `u_${uuid4()}`
}

// ------------------------------------------------------------------ storage (all guarded)

function readLocal(): string | null {
  try {
    return globalThis.localStorage?.getItem(USER_KEY) ?? null
  } catch {
    return null
  }
}

function writeLocal(id: string): boolean {
  try {
    globalThis.localStorage?.setItem(USER_KEY, id)
    return true
  } catch {
    return false
  }
}

function readCookie(): string | null {
  try {
    const raw = globalThis.document?.cookie
    if (typeof raw !== 'string' || raw.length === 0) return null
    for (const part of raw.split(';')) {
      const [k, ...rest] = part.trim().split('=')
      if (k === COOKIE_NAME) {
        try {
          return decodeURIComponent(rest.join('='))
        } catch {
          return rest.join('=')
        }
      }
    }
    return null
  } catch {
    return null
  }
}

function writeCookie(id: string): boolean {
  try {
    const doc = globalThis.document
    if (!doc) return false
    let cookie = `${COOKIE_NAME}=${encodeURIComponent(id)}; Path=/; Max-Age=${COOKIE_MAX_AGE_S}; SameSite=Lax`
    if (globalThis.location?.protocol === 'https:') cookie += '; Secure'
    doc.cookie = cookie
    return true
  } catch {
    return false
  }
}

function persist(id: string): void {
  const changed = memory !== id
  memory = id
  const local = writeLocal(id)
  const cookie = writeCookie(id)
  if (!local && !cookie) {
    log.warn('identity.storage_blocked', 'neither localStorage nor cookies writable; id is session-only')
  }
  if (changed) notify()
}

// ------------------------------------------------------------------ public API

/**
 * localStorage → cookie → in-memory → mint. Always writes the winner back to every store so the
 * three copies converge (a cookie-only id gets promoted into localStorage, and so on).
 */
export function getUserId(): string {
  const fromLocal = readLocal()
  if (isValidUserId(fromLocal)) {
    if (memory !== fromLocal || readCookie() !== fromLocal) persist(fromLocal)
    return fromLocal
  }
  const fromCookie = readCookie()
  if (isValidUserId(fromCookie)) {
    log.info('identity.restored', 'user id restored from cookie', { source: 'cookie' })
    persist(fromCookie)
    return fromCookie
  }
  if (isValidUserId(memory)) {
    persist(memory)
    return memory
  }
  const fresh = mintUserId()
  log.info('identity.minted', 'new anonymous user id', { user_id: fresh })
  persist(fresh)
  return fresh
}

/**
 * Cheap, side-effect-free read for React snapshots: the in-memory copy once one exists. Falls
 * through to getUserId() (which does the storage dance) only on the very first call.
 */
export function peekUserId(): string {
  return memory ?? getUserId()
}

/** Mint a fresh id and replace every stored copy. Returns the new id. */
export function resetUserId(): string {
  const fresh = mintUserId()
  log.info('identity.reset', 'user id reset', { user_id: fresh })
  persist(fresh)
  return fresh
}

/** Test hook: forget the in-memory copy (storage is left alone). */
export function _clearIdentityMemory(): void {
  memory = null
}

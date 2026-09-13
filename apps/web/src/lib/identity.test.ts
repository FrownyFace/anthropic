import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  COOKIE_NAME,
  USER_KEY,
  _clearIdentityMemory,
  getUserId,
  isValidUserId,
  mintUserId,
  resetUserId,
} from './identity'

function clearCookie() {
  document.cookie = `${COOKIE_NAME}=; Path=/; Max-Age=0`
}

function cookieValue(): string | null {
  const m = document.cookie.split(';').map((s) => s.trim()).find((s) => s.startsWith(`${COOKIE_NAME}=`))
  return m ? decodeURIComponent(m.slice(COOKIE_NAME.length + 1)) : null
}

beforeEach(() => {
  localStorage.clear()
  clearCookie()
  _clearIdentityMemory()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('isValidUserId', () => {
  it('accepts u_<uuid> and rejects everything else', () => {
    expect(isValidUserId('u_123e4567-e89b-12d3-a456-426614174000')).toBe(true)
    expect(isValidUserId(mintUserId())).toBe(true)
    expect(isValidUserId('123e4567-e89b-12d3-a456-426614174000')).toBe(false)
    expect(isValidUserId('u_short')).toBe(false)
    expect(isValidUserId('u_123E4567-E89B-12D3-A456-426614174000')).toBe(false) // uppercase
    expect(isValidUserId(null)).toBe(false)
    expect(isValidUserId(42)).toBe(false)
  })
})

describe('getUserId', () => {
  it('mints once and writes the id to both localStorage and the cookie', () => {
    const id = getUserId()
    expect(isValidUserId(id)).toBe(true)
    expect(localStorage.getItem(USER_KEY)).toBe(id)
    expect(cookieValue()).toBe(id)
  })

  it('re-reads the same id on subsequent calls', () => {
    const a = getUserId()
    const b = getUserId()
    expect(b).toBe(a)
  })

  it('falls back to the cookie when localStorage is empty, then promotes it back', () => {
    const fromCookie = 'u_0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f'
    document.cookie = `${COOKIE_NAME}=${fromCookie}; Path=/`
    expect(localStorage.getItem(USER_KEY)).toBeNull()

    const id = getUserId()
    expect(id).toBe(fromCookie)
    expect(localStorage.getItem(USER_KEY)).toBe(fromCookie)
  })

  it('prefers localStorage over a disagreeing cookie and re-syncs the cookie', () => {
    const local = 'u_11111111-1111-4111-8111-111111111111'
    const cookie = 'u_22222222-2222-4222-8222-222222222222'
    localStorage.setItem(USER_KEY, local)
    document.cookie = `${COOKIE_NAME}=${cookie}; Path=/`
    expect(getUserId()).toBe(local)
    expect(cookieValue()).toBe(local)
  })

  it('ignores a malformed stored value and mints a fresh one', () => {
    localStorage.setItem(USER_KEY, 'not-an-id')
    document.cookie = `${COOKIE_NAME}=garbage; Path=/`
    const id = getUserId()
    expect(isValidUserId(id)).toBe(true)
    expect(id).not.toBe('not-an-id')
    expect(localStorage.getItem(USER_KEY)).toBe(id)
  })

  it('stays stable for the session when storage throws (in-memory fallback)', () => {
    const blocked = () => {
      throw new Error('blocked')
    }
    const spies = [
      vi.spyOn(Storage.prototype, 'getItem').mockImplementation(blocked),
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(blocked),
      vi.spyOn(document, 'cookie', 'set').mockImplementation(blocked),
      vi.spyOn(document, 'cookie', 'get').mockReturnValue(''),
    ]
    try {
      const a = getUserId()
      const b = getUserId()
      expect(isValidUserId(a)).toBe(true)
      expect(b).toBe(a)
      expect(spies[2]).toHaveBeenCalled()
    } finally {
      // Accessor spies on `document.cookie` are not reliably undone by restoreAllMocks.
      for (const s of spies) s.mockRestore()
    }
  })

  it('writes the cookie with Path, Max-Age and SameSite=Lax (no Secure on http)', () => {
    const setter = vi.spyOn(document, 'cookie', 'set')
    try {
      getUserId()
      const written = String(setter.mock.calls.at(-1)![0])
      expect(written).toMatch(new RegExp(`^${COOKIE_NAME}=u_`))
      expect(written).toContain('Path=/')
      expect(written).toContain('Max-Age=31536000')
      expect(written).toContain('SameSite=Lax')
      expect(written).not.toContain('Secure')
    } finally {
      setter.mockRestore()
    }
  })
})

describe('resetUserId', () => {
  it('mints a different id and replaces both stored copies', () => {
    const before = getUserId()
    const after = resetUserId()
    expect(after).not.toBe(before)
    expect(isValidUserId(after)).toBe(true)
    expect(localStorage.getItem(USER_KEY)).toBe(after)
    expect(cookieValue()).toBe(after)
    expect(getUserId()).toBe(after)
  })
})

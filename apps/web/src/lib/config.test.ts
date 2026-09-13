import { describe, expect, it, vi } from 'vitest'

import { DEFAULT_HARNESS_URL, normaliseUrl, resolveConfig } from './config'

function res(body: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    statusText: ok ? 'OK' : 'ERR',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

const neverFetch = (() => {
  throw new Error('fetch should not have been called')
}) as unknown as typeof fetch

describe('normaliseUrl', () => {
  it('trims whitespace and trailing slashes so `${base}/runs` concatenates cleanly', () => {
    expect(normaliseUrl('  https://x.example.com///  ')).toBe('https://x.example.com')
    expect(normaliseUrl('https://x.example.com')).toBe('https://x.example.com')
  })
})

describe('resolveConfig precedence', () => {
  it('1. an injected window config wins and short-circuits the fetch', async () => {
    const out = await resolveConfig({
      win: { __FAULTLINE_CONFIG__: { harnessUrl: 'https://injected.example/' } },
      fetchImpl: neverFetch,
      buildEnvUrl: 'https://build.example',
    })
    expect(out).toEqual({ harnessUrl: 'https://injected.example', source: 'window' })
  })

  it('2. /config.json wins over the build-time env', async () => {
    const fetchImpl = vi.fn(async () => res({ harnessUrl: 'https://runtime.example/' })) as unknown as typeof fetch
    const out = await resolveConfig({
      win: {},
      fetchImpl,
      buildEnvUrl: 'https://build.example',
    })
    expect(out).toEqual({ harnessUrl: 'https://runtime.example', source: 'config.json' })
    expect((fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0]![0]).toBe('/config.json')
  })

  it('3. falls through to VITE_HARNESS_URL when /config.json is missing', async () => {
    const fetchImpl = vi.fn(async () => res({ detail: 'not found' }, false, 404)) as unknown as typeof fetch
    const out = await resolveConfig({ win: {}, fetchImpl, buildEnvUrl: 'https://build.example/' })
    expect(out).toEqual({ harnessUrl: 'https://build.example', source: 'build-env' })
  })

  it('3b. falls through when /config.json throws (pnpm dev, no file on disk)', async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }) as unknown as typeof fetch
    const out = await resolveConfig({ win: {}, fetchImpl, buildEnvUrl: 'https://build.example' })
    expect(out.source).toBe('build-env')
  })

  it('4. ends at the built-in default when nothing else is usable', async () => {
    const fetchImpl = vi.fn(async () => res({ harnessUrl: '   ' })) as unknown as typeof fetch
    const out = await resolveConfig({ win: {}, fetchImpl, buildEnvUrl: undefined })
    expect(out).toEqual({ harnessUrl: DEFAULT_HARNESS_URL, source: 'default' })
  })

  it('ignores non-http values at every level rather than producing a broken base', async () => {
    const fetchImpl = vi.fn(async () => res({ harnessUrl: 'ftp://nope' })) as unknown as typeof fetch
    const out = await resolveConfig({
      win: { __FAULTLINE_CONFIG__: { harnessUrl: 'not-a-url' } },
      fetchImpl,
      buildEnvUrl: 'also-not-a-url',
    })
    expect(out.source).toBe('default')
  })

  it('accepts a caller-supplied default (used by the replay-only mode)', async () => {
    const out = await resolveConfig({
      win: {},
      fetchImpl: vi.fn(async () => res({}, false, 404)) as unknown as typeof fetch,
      buildEnvUrl: undefined,
      defaultUrl: 'https://fallback.example/',
    })
    expect(out).toEqual({ harnessUrl: 'https://fallback.example', source: 'default' })
  })
})

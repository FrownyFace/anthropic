/**
 * Harness URL resolution.
 *
 * Precedence (first hit wins):
 *   1. window.__FAULTLINE_CONFIG__.harnessUrl   — injected inline by the host page
 *   2. GET /config.json  -> {"harnessUrl": "..."} — written at container start from $HARNESS_URL,
 *      so redeploying the harness never forces a frontend rebuild
 *   3. import.meta.env.VITE_HARNESS_URL         — baked at build time
 *   4. DEFAULT_HARNESS_URL                      — empty: nothing is baked into the bundle; an empty URL means "not configured"
 */

import { getLogger } from './log'

const log = getLogger('web')

export const DEFAULT_HARNESS_URL = ''

export interface FaultlineConfig {
  harnessUrl?: string
}

declare global {
  interface Window {
    __FAULTLINE_CONFIG__?: FaultlineConfig
  }
}

export interface ResolveOptions {
  /** Injectable for tests. */
  fetchImpl?: typeof fetch
  win?: { __FAULTLINE_CONFIG__?: FaultlineConfig } | undefined
  buildEnvUrl?: string | undefined
  configPath?: string
  defaultUrl?: string
}

/** Trailing slashes break `${base}/runs` concatenation; strip them once, here. */
export function normaliseUrl(raw: string): string {
  return raw.trim().replace(/\/+$/, '')
}

function isUsable(v: unknown): v is string {
  return typeof v === 'string' && v.trim().length > 0 && /^https?:\/\//i.test(v.trim())
}

export interface ResolvedConfig {
  harnessUrl: string
  source: 'window' | 'config.json' | 'build-env' | 'default'
}

export async function resolveConfig(opts: ResolveOptions = {}): Promise<ResolvedConfig> {
  const {
    fetchImpl = typeof fetch !== 'undefined' ? fetch : undefined,
    win = typeof window !== 'undefined' ? window : undefined,
    buildEnvUrl = typeof import.meta !== 'undefined'
      ? (import.meta.env?.VITE_HARNESS_URL as string | undefined)
      : undefined,
    configPath = '/config.json',
    defaultUrl = DEFAULT_HARNESS_URL,
  } = opts

  const injected = win?.__FAULTLINE_CONFIG__?.harnessUrl
  if (isUsable(injected)) {
    return { harnessUrl: normaliseUrl(injected), source: 'window' }
  }

  if (fetchImpl) {
    try {
      const res = await fetchImpl(configPath, { cache: 'no-store' })
      if (res.ok) {
        const body = (await res.json()) as FaultlineConfig
        if (isUsable(body?.harnessUrl)) {
          return { harnessUrl: normaliseUrl(body.harnessUrl), source: 'config.json' }
        }
      }
    } catch (err) {
      // Expected during `pnpm dev` (no /config.json on disk) — fall through quietly.
      log.debug('config.fetch_failed', 'no /config.json; falling back', {
        error: String(err),
      })
    }
  }

  if (isUsable(buildEnvUrl)) {
    return { harnessUrl: normaliseUrl(buildEnvUrl), source: 'build-env' }
  }

  return { harnessUrl: normaliseUrl(defaultUrl), source: 'default' }
}

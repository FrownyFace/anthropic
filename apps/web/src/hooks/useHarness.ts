/**
 * Resolve the harness URL once, build the client for the current identity, then keep the two
 * things Home needs from it: `/health` and `/scenarios`.
 *
 * Both are allowed to fail. A dead harness must still leave a usable page — the bundled replay
 * is the whole reason demo mode exists — so failures land in `phase` / `scenariosError`
 * rather than throwing.
 */

import { useEffect, useMemo, useState } from 'react'

import { useIdentity } from '@/hooks/useIdentity'
import { HarnessClient } from '@/lib/api'
import { resolveConfig, type ResolvedConfig } from '@/lib/config'
import { getLogger } from '@/lib/log'
import type { Health, Scenario } from '@/lib/types'

const log = getLogger('web')

const NO_URL_ERROR = 'harness URL not configured (config.json or VITE_HARNESS_URL); replays still work'

/**
 * Three states, not two: a cold harness answers `/health` in ~12 s, and during that window the
 * UI must say "checking", not "unreachable".
 */
export type HealthPhase = 'resolving' | 'checking' | 'reachable' | 'unreachable'

export function healthPhase(hasClient: boolean, health: Health | null, healthError: string | null): HealthPhase {
  if (health?.ok === true) return 'reachable'
  if (healthError !== null) return 'unreachable'
  if (health !== null) return 'unreachable' // answered, but not ok
  return hasClient ? 'checking' : 'resolving'
}

export interface HarnessState {
  /** `/health` answered and reported ok — the only gate on starting a live run. */
  reachable: boolean
  /** resolving → checking → reachable | unreachable. */
  phase: HealthPhase
  health: Health | null
  scenarios: Scenario[]
  scenariosError: string | null
  loading: boolean
  /** Bound to the current identity: every request carries `X-Faultline-User`. Null until the URL resolves. */
  client: HarnessClient | null
}

interface Probe<T> {
  client: HarnessClient
  value: T | null
  error: string | null
}

export function useHarness(): HarnessState {
  const { userId } = useIdentity()
  const [cfg, setCfg] = useState<ResolvedConfig | null>(null)
  /** Last answers, keyed by the client that produced them; a stale key reads as "not yet". */
  const [healthProbe, setHealthProbe] = useState<Probe<Health> | null>(null)
  const [scenariosProbe, setScenariosProbe] = useState<Probe<Scenario[]> | null>(null)

  // 1. Resolve the base URL once.
  useEffect(() => {
    let cancelled = false
    void resolveConfig().then((resolved) => {
      if (cancelled) return
      setCfg(resolved)
      log.info('config.resolved', 'harness URL resolved', {
        harness_url: resolved.harnessUrl,
        source: resolved.source,
      })
      if (!resolved.harnessUrl) log.warn('config.missing', 'no harness URL configured; live runs disabled')
    })
    return () => {
      cancelled = true
    }
  }, [])

  // 2. One client per (base, identity). A reset() of the id rebuilds it.
  const client = useMemo(
    () => (cfg && cfg.harnessUrl ? new HarnessClient(cfg.harnessUrl, { userId }) : null),
    [cfg, userId],
  )

  // 3. Probe /health and /scenarios whenever the client changes.
  useEffect(() => {
    if (!client) return
    let cancelled = false

    // Deliberately NOT `await Promise.all*` before updating: a cold harness answers /health in
    // ~12 s while /scenarios comes back in 0.3 s (or the other way round). Coupling them would
    // leave the whole page on a spinner for the slower of the two, so each settles on its own.
    client
      .health()
      .then((value) => {
        if (cancelled) return
        setHealthProbe({ client, value, error: null })
        log.info('health.ok', 'harness reachable', {
          model_default: value.model_default ?? null,
          has_provider_key: value.has_provider_key,
        })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setHealthProbe({ client, value: null, error: String(err) })
        log.warn('health.error', 'harness unreachable', { error: String(err) })
      })

    client
      .scenarios()
      .then((value) => {
        if (cancelled) return
        setScenariosProbe({ client, value, error: null })
        log.info('scenarios.loaded', 'catalogue loaded', { count: value.length })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setScenariosProbe({ client, value: null, error: String(err) })
        log.warn('scenarios.error', 'GET /scenarios failed', { error: String(err) })
      })

    return () => {
      cancelled = true
    }
  }, [client])

  // 2b. No URL at all (no /config.json, no VITE_HARNESS_URL): say so instead of spinning forever.
  const noUrl = !!cfg && !cfg.harnessUrl
  const healthCurrent = !!client && !!healthProbe && healthProbe.client === client
  const scenariosCurrent = !!client && !!scenariosProbe && scenariosProbe.client === client

  const health = healthCurrent ? healthProbe.value : null
  const healthError = noUrl ? NO_URL_ERROR : healthCurrent ? healthProbe.error : null
  const scenarios = scenariosCurrent ? (scenariosProbe.value ?? []) : []
  const scenariosError = scenariosCurrent ? scenariosProbe.error : null

  // Loading until the URL resolves, and again whenever the client moves on.
  const loading = !cfg || (client !== null && !(healthCurrent && scenariosCurrent))

  return {
    reachable: health?.ok === true,
    phase: healthPhase(client !== null, health, healthError),
    health,
    scenarios,
    scenariosError,
    loading,
    client,
  }
}

/**
 * Bundled replays: `public/demo/<scenario>.json` holds a RunRecord captured from a real run
 * (scripts/export_demo.py; see public/demo/README.md). `hooks/useReplay.ts` folds it through the
 * same reducer a live run goes through, one event every DEMO_STEP_MS, so the UI is fully
 * exercisable with no backend at all.
 */

import type { RunRecord } from './types'

export const DEMO_STEP_MS = 400

export interface DemoMeta {
  id: string
  label: string
  file: string
}

/** Demos shipped in public/demo. Adding a file here is all it takes to expose a new replay. */
export const DEMOS: DemoMeta[] = [
  // Real captures: claude-haiku-4-5 on the deployed stack (2026-09-12), exported with
  // scripts/export_demo.py. Ids match scenario ids so the scenario table offers them via demoFor().
  { id: 'lost-ack', label: 'lost-ack', file: '/demo/lost-ack.json' },
  { id: 'locked-file', label: 'locked-file', file: '/demo/locked-file.json' },
  { id: 'missing-config', label: 'missing-config', file: '/demo/missing-config.json' },
  { id: 'gauntlet', label: 'gauntlet', file: '/demo/gauntlet.json' },
]

export function demoFor(scenarioId: string): DemoMeta | undefined {
  return DEMOS.find((d) => d.id === scenarioId)
}

export async function loadDemo(
  file: string,
  fetchImpl: typeof fetch = fetch,
): Promise<RunRecord> {
  const res = await fetchImpl(file, { cache: 'no-store' })
  if (!res.ok) throw new Error(`demo ${file} not found (${res.status})`)
  const rec = (await res.json()) as RunRecord
  if (!Array.isArray(rec?.events)) throw new Error(`demo ${file} has no events`)
  return rec
}

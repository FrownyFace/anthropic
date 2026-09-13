/** Pure helpers for the Composer's slash menu (kept out of the component file for fast refresh). */

import type { Scenario } from '@/lib/types'

/** The slash query when the draft is exactly `/` + word characters, else null. */
export function parseSlash(draft: string): string | null {
  const m = /^\/([\w-]*)$/.exec(draft)
  return m ? m[1]!.toLowerCase() : null
}

export function filterScenarios(scenarios: readonly Scenario[], query: string): Scenario[] {
  const q = query.toLowerCase()
  return scenarios.filter((s) => s.id.toLowerCase().startsWith(q) || s.title.toLowerCase().includes(q))
}

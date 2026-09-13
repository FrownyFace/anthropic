/** Breakpoint and open state for the workspace panel (RunLayout). */

import { useCallback, useState, useSyncExternalStore } from 'react'

const WIDE_QUERY = '(min-width: 1280px)'

function subscribeWide(cb: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => undefined
  const mql = window.matchMedia(WIDE_QUERY)
  mql.addEventListener('change', cb)
  return () => mql.removeEventListener('change', cb)
}

function isWide(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(WIDE_QUERY).matches
    : true
}

/** True at ≥1280 px, where the workspace is a side column rather than an overlay sheet. */
export function useIsWide(): boolean {
  return useSyncExternalStore(subscribeWide, isWide, () => true)
}

/**
 * Workspace panel open state: open by default on wide screens (a side column), closed by default
 * on narrow ones (where it is an overlay sheet the reader must ask for). Follows the breakpoint on
 * resize so rotating a tablet does not leave a sheet covering the transcript: the user's choice is
 * kept per breakpoint, and a breakpoint change falls back to that breakpoint's default.
 */
export function useWorkspaceOpen(): [boolean, (open: boolean | ((v: boolean) => boolean)) => void] {
  const wide = useIsWide()
  const [choice, setChoice] = useState<{ wide: boolean; open: boolean } | null>(null)
  const open = choice && choice.wide === wide ? choice.open : wide
  const setOpen = useCallback(
    (next: boolean | ((v: boolean) => boolean)) =>
      setChoice((prev) => {
        const cur = prev && prev.wide === wide ? prev.open : wide
        return { wide, open: typeof next === 'function' ? next(cur) : next }
      }),
    [wide],
  )
  return [open, setOpen]
}

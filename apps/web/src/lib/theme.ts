import { useSyncExternalStore } from 'react'

/**
 * Theme preference: light, dark, or follow the OS. Persisted per browser; applied by toggling the
 * `dark` class on <html> (shadcn's `@custom-variant dark (&:is(.dark *))`). index.html runs the
 * same logic inline before first paint so there is no flash of the wrong theme.
 */
export type Theme = 'light' | 'dark' | 'system'

export const THEME_KEY = 'faultline.theme'
export const THEMES: readonly Theme[] = ['light', 'dark', 'system']
const QUERY = '(prefers-color-scheme: dark)'

let memory: Theme = 'system'
const listeners = new Set<() => void>()

function isTheme(v: unknown): v is Theme {
  return v === 'light' || v === 'dark' || v === 'system'
}

export function getTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY)
    // Storage is the source of truth when it is readable; anything unrecognised means "system".
    return isTheme(v) ? v : 'system'
  } catch {
    // Storage blocked (private mode, sandboxed frame): remember the choice for this page only.
    return memory
  }
}

export function systemPrefersDark(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(QUERY).matches
    : true
}

/** The theme actually in effect (system resolved). */
export function resolvedTheme(theme: Theme = getTheme()): 'light' | 'dark' {
  return theme === 'system' ? (systemPrefersDark() ? 'dark' : 'light') : theme
}

export function applyTheme(theme: Theme = getTheme()): void {
  if (typeof document === 'undefined') return
  const dark = resolvedTheme(theme) === 'dark'
  const root = document.documentElement
  root.classList.toggle('dark', dark)
  root.style.colorScheme = dark ? 'dark' : 'light'
  root.dataset.theme = theme
}

/**
 * Boot: honour a one-off `?theme=light|dark|system` in the URL (handy for sharing a link in a given
 * look and for screenshot tooling), persist it, then apply whatever is stored.
 */
export function initTheme(): void {
  if (typeof window === 'undefined') return
  try {
    const q = new URLSearchParams(window.location.search).get('theme')
    if (isTheme(q)) {
      setTheme(q)
      return
    }
  } catch {
    /* ignore */
  }
  applyTheme()
}

export function setTheme(theme: Theme): void {
  memory = theme
  try {
    localStorage.setItem(THEME_KEY, theme)
  } catch {
    /* ignore */
  }
  applyTheme(theme)
  for (const fn of listeners) fn()
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  let mql: MediaQueryList | null = null
  const onMedia = () => {
    if (getTheme() === 'system') applyTheme('system')
    fn()
  }
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    mql = window.matchMedia(QUERY)
    mql.addEventListener('change', onMedia)
  }
  return () => {
    listeners.delete(fn)
    mql?.removeEventListener('change', onMedia)
  }
}

export function useTheme(): { theme: Theme; resolved: 'light' | 'dark'; setTheme: (t: Theme) => void } {
  const theme = useSyncExternalStore(subscribe, getTheme, () => 'system' as Theme)
  const resolved = useSyncExternalStore(subscribe, () => resolvedTheme(getTheme()), () => 'dark' as const)
  return { theme, resolved, setTheme }
}

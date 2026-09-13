import { beforeEach, describe, expect, it } from 'vitest'

import { THEME_KEY, applyTheme, getTheme, initTheme, resolvedTheme, setTheme } from './theme'

describe('theme', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.className = ''
  })

  it('defaults to system and resolves against prefers-color-scheme', () => {
    expect(getTheme()).toBe('system')
    expect(['light', 'dark']).toContain(resolvedTheme())
  })

  it('persists an explicit choice and toggles the dark class', () => {
    setTheme('dark')
    expect(localStorage.getItem(THEME_KEY)).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(document.documentElement.dataset.theme).toBe('dark')
    setTheme('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    expect(document.documentElement.style.colorScheme).toBe('light')
  })

  it('ignores garbage in storage', () => {
    localStorage.setItem(THEME_KEY, 'neon')
    expect(getTheme()).toBe('system')
    applyTheme()
    expect(document.documentElement.dataset.theme).toBe('system')
  })

  it('honours ?theme= on boot and persists it', () => {
    window.history.replaceState({}, '', '/?theme=light')
    initTheme()
    expect(getTheme()).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    window.history.replaceState({}, '', '/')
  })
})

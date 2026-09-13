import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { FileStatusTable, sortFiles } from './FileStatusTable'
import { files } from './test-fixtures'

describe('FileStatusTable', () => {
  it('lists changed files first and shows size + short sha', () => {
    render(<FileStatusTable files={files} />)
    const rows = [...document.querySelectorAll<HTMLElement>('tbody tr[data-status]')]
    expect(rows.map((r) => r.getAttribute('data-status'))).toEqual([
      'added',
      'modified',
      'modified',
      'deleted',
      'unchanged',
      'unchanged',
    ])
    expect(rows[0]!.textContent).toContain('notes/NEW.md')
    expect(rows[0]!.textContent).toContain('2.0 KB')
    expect(rows[0]!.textContent).toContain('1111111')
    expect(rows[1]!.textContent).toContain('CHANGELOG.md')
    expect(rows[1]!.textContent).toContain('381 B')
    expect(rows[1]!.textContent).toContain('4622807')
    expect(screen.getByText('4 changed · 6 files')).toBeTruthy()
    expect(screen.getByText('1 added · 2 modified · 1 deleted')).toBeTruthy()
    // not interactive without onSelect
    expect(rows[0]!.getAttribute('tabindex')).toBeNull()
  })

  it('keeps the snapshot order inside each status group', () => {
    const sorted = sortFiles(files).map((f) => f.path)
    expect(sorted.indexOf('CHANGELOG.md')).toBeLessThan(sorted.indexOf('src/ratelimiter/version.py'))
    expect(sorted.indexOf('README.md')).toBeLessThan(sorted.indexOf('pytest.ini'))
  })

  it('makes rows selectable with mouse and keyboard when onSelect is given', () => {
    const onSelect = vi.fn()
    render(<FileStatusTable files={files} onSelect={onSelect} selected="CHANGELOG.md" />)
    const rows = [...document.querySelectorAll<HTMLElement>('tbody tr[data-status]')]
    expect(rows[1]!.getAttribute('aria-selected')).toBe('true')
    expect(rows[0]!.getAttribute('aria-selected')).toBe('false')
    expect(rows[0]!.getAttribute('tabindex')).toBe('0')
    fireEvent.click(rows[0]!)
    expect(onSelect).toHaveBeenCalledWith('notes/NEW.md')
    fireEvent.keyDown(rows[3]!, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('tests/old_test.py')
  })

  it('renders an empty state', () => {
    render(<FileStatusTable files={[]} />)
    expect(screen.getByText(/No workspace snapshot yet/)).toBeTruthy()
    expect(screen.getByText('no snapshot')).toBeTruthy()
  })
})

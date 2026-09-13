import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CodeBlock, parseUnifiedDiff } from './CodeBlock'
import { changelogDiff, versionDiff } from './test-fixtures'

afterEach(() => {
  Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
})

describe('parseUnifiedDiff', () => {
  it('numbers context/add/del lines from the hunk header', () => {
    const rows = parseUnifiedDiff(changelogDiff.unified)
    expect(rows.slice(0, 3).map((r) => r.kind)).toEqual(['meta', 'meta', 'hunk'])
    const body = rows.slice(3)
    expect(body.map((r) => r.kind)).toEqual(['ctx', 'ctx', 'add', 'add', 'add', 'add', 'ctx'])
    // context keeps both counters; adds only advance the new-file counter
    expect(body[0]).toMatchObject({ old: 1, cur: 1, text: '# Changelog' })
    expect(body[2]).toMatchObject({ old: null, cur: 3, text: '## [0.2.0] - 2026-09-12' })
    expect(body[6]).toMatchObject({ old: 3, cur: 7 })
  })

  it('handles single-line hunks and deletions', () => {
    const rows = parseUnifiedDiff(versionDiff.unified)
    const del = rows.find((r) => r.kind === 'del')!
    const add = rows.find((r) => r.kind === 'add')!
    expect(del).toMatchObject({ old: 1, cur: null, text: '__version__ = "0.1.0"' })
    expect(add).toMatchObject({ old: null, cur: 1, text: '__version__ = "0.2.0"' })
  })
})

describe('CodeBlock', () => {
  it('renders a numbered listing with the filename', () => {
    const { container } = render(<CodeBlock code={'a = 1\nb = 2\nc = 3\n'} filename="x.py" />)
    expect(screen.getByText('x.py')).toBeTruthy()
    const lines = container.querySelectorAll('[data-line-kind="code"]')
    expect(lines).toHaveLength(3)
    expect(lines[2]!.textContent).toContain('3')
    expect(lines[2]!.textContent).toContain('c = 3')
  })

  it('colours + and - lines in diff mode and shows the counts', () => {
    const { container } = render(<CodeBlock diff={changelogDiff.unified} filename="CHANGELOG.md" />)
    expect(screen.getByText('+4')).toBeTruthy()
    expect(screen.getByText('−0')).toBeTruthy()
    const adds = container.querySelectorAll('[data-line-kind="add"]')
    expect(adds).toHaveLength(4)
    for (const el of adds) expect(el.className).toContain('emerald')
    expect(container.querySelector('[data-line-kind="hunk"]')!.textContent).toContain('@@ -1,3 +1,7 @@')
    expect(container.querySelectorAll('[data-line-kind="meta"]')).toHaveLength(2)
  })

  it('tints deletions rose', () => {
    const { container } = render(<CodeBlock diff={versionDiff.unified} />)
    const del = container.querySelector('[data-line-kind="del"]')!
    expect(del.className).toContain('rose')
    expect(del.textContent).toContain('__version__ = "0.1.0"')
    expect(screen.getByText('+1')).toBeTruthy()
    expect(screen.getByText('−1')).toBeTruthy()
  })

  it('copies the raw text when the clipboard is available', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<CodeBlock code="print('hi')" />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy code' }))
    expect(writeText).toHaveBeenCalledWith("print('hi')")
    await waitFor(() => expect(screen.getByText('Copied')).toBeTruthy())
  })

  it('does not throw when the clipboard is unavailable', () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    render(<CodeBlock code="x" />)
    expect(() => fireEvent.click(screen.getByRole('button', { name: 'Copy code' }))).not.toThrow()
    expect(screen.getByText('Copy')).toBeTruthy()
  })
})

import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LoadingState } from './LoadingState'

describe('LoadingState', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('renders a status with the label, sublabel and a ticking clock', () => {
    render(<LoadingState label="Provisioning sandbox" sublabel="uploading fixture" />)
    const status = screen.getByRole('status')
    expect(status.textContent).toContain('Provisioning sandbox')
    expect(status.textContent).toContain('uploading fixture')
    expect(status.textContent).toContain('0.0s')
    act(() => vi.advanceTimersByTime(1200))
    expect(status.textContent).toContain('1.2s')
  })

  it('draws the nine-cell pixel grid and shimmers the label', () => {
    const { container } = render(<LoadingState label="Loading run" />)
    expect(container.querySelectorAll('.bui-pixel')).toHaveLength(9)
    expect(container.querySelector('.bui-shimmer')!.textContent).toBe('Loading run')
  })
})

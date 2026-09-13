import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'

import { ThinkingTrace } from './ThinkingTrace'
import { lostAckWrite, readCall, runningCall } from './test-fixtures'

function renderTrace(ui: React.ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>)
}

describe('ThinkingTrace', () => {
  it('shows the in-progress header and a spinner on the running call', () => {
    const { container } = renderTrace(
      <ThinkingTrace step={6} maxSteps={20} calls={[runningCall]} active done={false} />,
    )
    expect(screen.getByText('Step 6 of 20')).toBeTruthy()
    const status = screen.getByRole('status')
    expect(status.textContent).toBe('running 1 tool call')
    expect(container.querySelector('.bui-shimmer')).not.toBeNull()
    // open by default while active
    const toggle = screen.getByRole('button', { name: /Step 6 of 20/ })
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('img', { name: 'running' })).toBeTruthy()
    expect(screen.getByText('Run')).toBeTruthy()
    expect(screen.getByText('python -m pytest -q')).toBeTruthy()
  })

  it('settles when done, lists calls with fault + recovered badges, and toggles', () => {
    renderTrace(
      <ThinkingTrace
        step={2}
        maxSteps={20}
        calls={[readCall, lostAckWrite]}
        thinking="Inserting the 0.2.0 section directly above 0.1.0."
        active={false}
        done
      >
        <div data-testid="child">chips</div>
      </ThinkingTrace>,
    )
    expect(screen.getByRole('status').textContent).toBe('2 tool calls')
    expect(screen.getByText('1 fault')).toBeTruthy()
    expect(screen.getByText('1 read-back seen')).toBeTruthy()
    expect(screen.getByText('1 unknown')).toBeTruthy()
    expect(screen.queryByText(/error/)).toBeNull()
    expect(screen.getByText('Inserting the 0.2.0 section directly above 0.1.0.')).toBeTruthy()
    expect(screen.getByText('Read')).toBeTruthy()
    expect(screen.getByText('Write')).toBeTruthy()
    expect(screen.getByText('simulated: ack_lost')).toBeTruthy()
    expect(screen.getByText('read-back seen')).toBeTruthy()
    expect(screen.getByTestId('child')).toBeTruthy()

    // collapsed by default once settled: the panel is hidden from the accessibility tree
    const toggle = screen.getByRole('button', { name: /Step 2 of 20/ })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    const panel = document.getElementById(toggle.getAttribute('aria-controls')!)!
    expect(panel.getAttribute('aria-hidden')).toBe('true')
    expect(screen.queryByRole('img', { name: 'ok' })).toBeNull()

    // the header toggles it; once open the per-call glyphs are exposed
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(panel.getAttribute('aria-hidden')).toBe('false')
    expect(screen.getByRole('img', { name: 'ok' })).toBeTruthy()
    // the lost-ack write is an unknown outcome on the rail, not a cross
    expect(screen.getByRole('img', { name: 'unknown' })).toBeTruthy()
    expect(screen.queryByRole('img', { name: 'error' })).toBeNull()
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
  })

  it('honours defaultOpen', () => {
    renderTrace(<ThinkingTrace step={1} calls={[readCall]} active={false} done defaultOpen />)
    expect(screen.getByRole('button', { name: /Step 1/ }).getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('status').textContent).toBe('1 tool call')
  })
})

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { evaluation as partialEvaluation } from '@/components/bui/test-fixtures'
import { TooltipProvider } from '@/components/ui/tooltip'
import { loadFixture } from '@/lib/__fixtures__/load'
import { fromRunRecord, type ViewState } from '@/lib/reducer'

import { RunStatusStrip } from './RunStatusStrip'

const workerCrash = fromRunRecord(loadFixture('worker-crash-resumed'))
const sandboxLost = fromRunRecord(loadFixture('sandbox-lost-interrupted'))

function renderStrip(state: ViewState | null, source: 'record' | 'sqlite' | null = 'record') {
  return render(
    <TooltipProvider>
      <RunStatusStrip state={state} transport={null} source={source} />
    </TooltipProvider>,
  )
}

const chip = (slot: string) => document.querySelector<HTMLElement>(`[data-slot="${slot}"]`)

describe('RunStatusStrip', () => {
  it('worker-crash: "worker 2 · resumed" chip (planned chaos in the tooltip), no sandbox chip, ok · passed', () => {
    renderStrip(workerCrash)
    const worker = chip('worker-chip')!
    expect(worker.textContent).toContain('worker 2 · resumed')
    expect(worker.getAttribute('data-tone')).toBe('resumed')
    expect(worker.getAttribute('data-worker-generation')).toBe('2')
    expect(worker.getAttribute('title')).toMatch(/planned chaos/)
    expect(worker.getAttribute('title')).toMatch(/real: harness worker interrupted mid-call/)
    expect(worker.className).toContain('sky')
    expect(chip('sandbox-chip')).toBeNull()
    const status = screen.getByText('ok')
    expect(status.getAttribute('data-grade')).toBe('passed')
    expect(status.getAttribute('title')).toBe('ok · passed')
    expect(status.className).toContain('emerald')
    expect(screen.getByText('3/3 passed')).toBeTruthy()
    expect(screen.getByText('100')).toBeTruthy()
  })

  it('sandbox-loss: "interrupted at step 1" (never the bare status word) and "sandbox lost at step 1"', () => {
    renderStrip(sandboxLost)
    const worker = chip('worker-chip')!
    expect(worker.textContent).toContain('interrupted at step 1')
    expect(worker.getAttribute('data-tone')).toBe('interrupted')
    expect(worker.getAttribute('title')).toMatch(/unplanned/)
    expect(worker.className).toContain('rose')
    const sandbox = chip('sandbox-chip')!
    expect(sandbox.textContent).toContain('sandbox lost at step 1')
    expect(sandbox.getAttribute('data-sandbox-status')).toBe('terminated')
    expect(sandbox.getAttribute('title')).toMatch(/sb-DAKQezAvuNlMf/)
    expect(sandbox.getAttribute('title')).toMatch(/A real failure, not a simulated one/)
    // exactly one element reads the bare status word (the e2e locator relies on it)
    expect(screen.getAllByText(/^interrupted$/)).toHaveLength(1)
    expect(screen.getByText('interrupted').className).toContain('rose')
  })

  it('no chips for a plain run; graded-with-failures is amber, not green', () => {
    renderStrip({ ...workerCrash, interruptions: [], resumes: [], workerGeneration: 1, sandboxEvents: [], evaluation: partialEvaluation })
    expect(chip('worker-chip')).toBeNull()
    expect(chip('sandbox-chip')).toBeNull()
    const status = screen.getByText('ok')
    expect(status.getAttribute('data-grade')).toBe('partial')
    expect(status.getAttribute('title')).toBe('ok · graded with failures')
    expect(status.className).toContain('amber')
    expect(status.className).not.toContain('emerald')
    expect(screen.getByText('2/3 passed')).toBeTruthy()
  })

  it('ok without an evaluation is neutral (not graded), showing the read-back hint instead of checks', () => {
    renderStrip({ ...workerCrash, interruptions: [], resumes: [], workerGeneration: 1, sandboxEvents: [], evaluation: null })
    const status = screen.getByText('ok')
    expect(status.getAttribute('data-grade')).toBe('ungraded')
    expect(status.className).not.toContain('emerald')
    expect(screen.getByText(/seen$/)).toBeTruthy()
  })

  it('renders nothing without a state', () => {
    const { container } = renderStrip(null)
    expect(container.querySelector('[data-slot="badge"]')).toBeNull()
  })
})

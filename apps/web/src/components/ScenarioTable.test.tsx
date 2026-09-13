import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { scenarios as baseScenarios } from '@/components/bui/test-fixtures'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { Scenario } from '@/lib/types'

import { ScenarioTable } from './ScenarioTable'

const workerCrash: Scenario = {
  id: 'worker-crash',
  title: 'Worker crash',
  description: 'The harness process is killed mid-write.',
  task_prompt: 'Prepare release 0.2.0.',
  max_steps: 20,
  fault_kinds: [],
  harness_faults: [{ kind: 'worker_crash', tool: 'write_file', path: 'CHANGELOG.md', nth: 1, after_ms: 400 }],
  faults_public: [{ kind: 'worker_crash', origin: 'real', layer: 'harness', description: 'the worker really dies' }],
  checks: [{ id: 'verified_before_rewrite', description: 'read back before writing again', weight: 2 }],
}

const scenarios: Scenario[] = [...baseScenarios, workerCrash]

function renderTable(over: Partial<React.ComponentProps<typeof ScenarioTable>> = {}) {
  const props = {
    scenarios,
    selectedId: null,
    starting: null,
    canRun: true,
    onSelect: vi.fn(),
    onRun: vi.fn(),
    onReplay: vi.fn(),
    ...over,
  }
  render(
    <TooltipProvider>
      <ScenarioTable {...props} />
    </TooltipProvider>,
  )
  return props
}

const rowFor = (id: string) => screen.getByText(id).closest('tr')!

describe('ScenarioTable', () => {
  it('has six columns: scenario, what goes wrong, step budget, failure, run, replay', () => {
    renderTable()
    const headers = screen.getAllByRole('columnheader').map((h) => h.textContent?.trim())
    expect(headers).toEqual(['Scenario', 'What goes wrong', 'Step budget', 'Failure', 'Run', 'Replay'])
    expect(screen.getAllByRole('row')).toHaveLength(1 + scenarios.length)
  })

  it('every row has a Run and a Replay button; Replay is enabled only where a recording is bundled', () => {
    const props = renderTable()
    for (const s of scenarios) {
      const row = rowFor(s.id)
      expect(within(row).getByRole('button', { name: 'Run' })).toBeTruthy()
      expect(within(row).getByRole('button', { name: 'Replay' })).toBeTruthy()
    }
    const lostAck = rowFor('lost-ack')
    const replay = within(lostAck).getByRole('button', { name: 'Replay' })
    expect(replay.hasAttribute('disabled')).toBe(false)
    fireEvent.click(replay)
    expect(props.onReplay).toHaveBeenCalledWith('lost-ack')
    expect(props.onSelect).not.toHaveBeenCalled() // stopPropagation: the row click is not fired

    fireEvent.click(within(lostAck).getByRole('button', { name: 'Run' }))
    expect(props.onRun).toHaveBeenCalledWith(expect.objectContaining({ id: 'lost-ack' }))
    expect(props.onSelect).not.toHaveBeenCalled()

    fireEvent.click(lostAck)
    expect(props.onSelect).toHaveBeenCalledWith('lost-ack')
  })

  it('a scenario without a bundled recording has a disabled Replay wrapped in a tooltip trigger', async () => {
    renderTable()
    const row = rowFor('missing-file')
    const replay = within(row).getByRole('button', { name: 'Replay' })
    expect(replay.hasAttribute('disabled')).toBe(true)
    const trigger = replay.closest('[data-slot="tooltip-trigger"]')!
    expect(trigger).not.toBeNull()
    fireEvent.pointerEnter(trigger)
    fireEvent.mouseEnter(trigger)
    fireEvent.pointerMove(trigger)
    fireEvent.mouseMove(trigger)
    expect(await screen.findByText('No recording for this scenario yet.', {}, { timeout: 2000 })).toBeTruthy()
  })

  it('Run is disabled with an explanation when the harness is offline', () => {
    renderTable({ canRun: false })
    const run = within(rowFor('lost-ack')).getByRole('button', { name: 'Run' })
    expect(run.hasAttribute('disabled')).toBe(true)
    expect(run.getAttribute('title')).toBe('Live runs are offline')
    expect(screen.getByText(/Live runs are offline\. Replay still works\./)).toBeTruthy()
  })

  it('shows the failure classes by origin: a real harness interruption is red, injected is amber', () => {
    renderTable()
    const crash = within(rowFor('worker-crash')).getByText('real: worker crash')
    expect(crash.closest('[data-origin]')!.getAttribute('data-origin')).toBe('real')
    expect(crash.closest('[data-origin]')!.className).toContain('rose')
    // fallback for an older catalogue with only fault_kinds
    const lostAck = within(rowFor('lost-ack')).getByText('lost ack')
    expect(lostAck.className).toContain('amber')
    expect(lostAck.className).not.toContain('rose')
  })

  it('shows a busy Run while that scenario is starting', () => {
    renderTable({ starting: 'lost-ack' })
    expect(within(rowFor('lost-ack')).getByRole('button', { name: /Starting/ }).hasAttribute('disabled')).toBe(true)
    expect(within(rowFor('missing-file')).getByRole('button', { name: 'Run' }).hasAttribute('disabled')).toBe(false)
  })
})

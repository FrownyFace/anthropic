import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { loadFixture } from '@/lib/__fixtures__/load'
import { fromRunRecord, initialState } from '@/lib/reducer'
import { transcriptFromViewState, type Transcript } from '@/lib/transcript'

import { TranscriptView } from './TranscriptView'

const workerCrash = fromRunRecord(loadFixture('worker-crash-resumed'))
const sandboxLost = fromRunRecord(loadFixture('sandbox-lost-interrupted'))

function renderView(transcript: Transcript, extra: Partial<React.ComponentProps<typeof TranscriptView>> = {}) {
  return render(
    <TooltipProvider>
      <TranscriptView transcript={transcript} diffs={[]} maxSteps={20} {...extra} />
    </TooltipProvider>,
  )
}

function finished(over: Partial<Transcript>): Transcript {
  return { ...transcriptFromViewState(initialState(), false), status: 'ok', ...over }
}

describe('TranscriptView — interruption callout (from the worker-crash fixture)', () => {
  it('renders the callout inside step 4, linked to the dangling write, with the resume from event 27', () => {
    renderView(transcriptFromViewState(workerCrash, false))
    const step4 = screen.getByRole('region', { name: 'Step 4' })
    const box = step4.querySelector<HTMLElement>('[data-slot="interruption"]')!
    expect(box).toBeTruthy()
    expect(box.getAttribute('role')).toBe('status')
    expect(box.textContent).toContain('Real interruption: real: harness worker interrupted mid-call (outcome unknown) (planned chaos)')
    expect(box.textContent).toContain("A fresh worker (#2) resumed from event 27; the in-flight call's outcome is unknown until the ledger says otherwise.")
    expect(box.getAttribute('data-planned')).toBe('true')
    expect(box.getAttribute('data-resumed')).toBe('true')
    expect(box.getAttribute('data-layer')).toBe('harness')
    expect(box.getAttribute('data-worker-generation')).toBe('2')
    expect(box.getAttribute('data-tool-use-id')).toBe('toolu_01D1gyQSGgDbctbgj7hSbJe1')
    const link = within(box).getByRole('button', { name: 'in-flight call: write_file CHANGELOG.md' })
    // the chip it links to exists (ToolCallChips row with the same tool_use_id)
    expect(document.getElementById('tool-call-toolu_01D1gyQSGgDbctbgj7hSbJe1')).not.toBeNull()
    fireEvent.click(link) // jsdom has no scrollIntoView; must not throw
    // no other step carries a callout
    expect(document.querySelectorAll('[data-slot="interruption"]')).toHaveLength(1)
    // graded run: the verdict renders and there is no "not graded" callout
    expect(document.querySelector('[data-slot="score-rows"]')).not.toBeNull()
    expect(document.querySelector('[data-slot="not-graded"]')).toBeNull()
    // the dangling write's chip says unknown, never error
    const chip = document.getElementById('tool-call-toolu_01D1gyQSGgDbctbgj7hSbJe1')!
    expect(chip.querySelector('[data-status]')!.getAttribute('data-status')).toBe('unknown')
  })

  it('renders an unplanned, unresumed interruption at step 1 for the sandbox-loss fixture', () => {
    renderView(transcriptFromViewState(sandboxLost, false))
    const box = screen.getByRole('region', { name: 'Step 1' }).querySelector<HTMLElement>('[data-slot="interruption"]')!
    expect(box.textContent).toContain('Real interruption: real: sandbox terminated or unavailable (unplanned)')
    expect(box.textContent).toContain('No worker resumed the run; the Modal Sandbox was terminated or became unreachable')
    expect(box.getAttribute('data-planned')).toBe('false')
    expect(box.getAttribute('data-resumed')).toBe('false')
  })

  it('an interruption whose step has no turn is still shown after the transcript', () => {
    const t = transcriptFromViewState(workerCrash, false)
    renderView({ ...t, interruptions: [{ ...t.interruptions[0]!, step: 99, tool_use_id: null }] })
    const box = document.querySelector<HTMLElement>('[data-slot="interruption"]')!
    expect(box).not.toBeNull()
    expect(box.closest('section')).toBeNull()
    expect(within(box).queryByRole('button')).toBeNull()
  })
})

describe('TranscriptView — the "not graded" callout branches on status / evaluation_status / error_class', () => {
  it('interrupted: cites the ESANDBOX label, red, "skipped"', () => {
    renderView(transcriptFromViewState(sandboxLost, false))
    const box = document.querySelector<HTMLElement>('[data-slot="not-graded"]')!
    expect(box.textContent).toContain('Not graded — the run was interrupted')
    expect(box.textContent).toContain('real: sandbox terminated or unavailable')
    expect(box.textContent).toContain('Grading was skipped; there is no score.')
    expect(box.getAttribute('data-status')).toBe('interrupted')
    expect(box.getAttribute('data-evaluation-status')).toBe('skipped')
    expect(box.getAttribute('data-error-code')).toBe('ESANDBOX')
    expect(box.className).toContain('rose')
    expect(document.querySelector('[data-slot="score-rows"]')).toBeNull()
    expect(document.querySelector('[data-slot="run-error"]')).toBeNull()
  })

  it('unevaluated: the grader failed — not the agent\'s fault — with the error class label', () => {
    renderView(
      finished({
        status: 'unevaluated',
        evaluationStatus: 'failed',
        evaluationError: 'evaluate: 503 from sandbox-env',
        errorClass: { origin: 'real', layer: 'gym', code: 'EGYM', kind: null, label: 'real: gym control-plane failure', outcome_known: true, side_effect_applied: null, detail: null },
      }),
    )
    const box = document.querySelector<HTMLElement>('[data-slot="not-graded"]')!
    expect(box.textContent).toContain('Not graded — the grader failed')
    expect(box.textContent).toContain('real: gym control-plane failure')
    expect(box.textContent).toContain("not the agent's")
    expect(box.className).toContain('amber')
  })

  it('unevaluated without an error class falls back to evaluation_error, never to the presence of error text', () => {
    renderView(finished({ status: 'unevaluated', evaluationStatus: 'failed', evaluationError: 'evaluate timed out', error: null }))
    expect(document.querySelector('[data-slot="not-graded"]')!.textContent).toContain('evaluate timed out')
  })

  it('error: the run could not be executed, plus the red run-error callout', () => {
    renderView(
      finished({
        status: 'error',
        error: 'provider 529',
        errorClass: { origin: 'real', layer: 'model', code: 'EMODEL', kind: null, label: 'real: model API error', outcome_known: true, side_effect_applied: null, detail: null },
      }),
    )
    const box = document.querySelector<HTMLElement>('[data-slot="not-graded"]')!
    expect(box.textContent).toContain('Not graded — the run could not be executed')
    expect(box.textContent).toContain('real: model API error')
    const err = document.querySelector<HTMLElement>('[data-slot="run-error"]')!
    expect(err.textContent).toContain('The run ended with an error')
    expect(err.textContent).toContain('real: model API error')
  })

  it('ok without an evaluation: pending or unavailable, not a pass; a bare score is never a grade', () => {
    renderView(finished({ status: 'ok', score: null }))
    expect(document.querySelector('[data-slot="not-graded"]')!.textContent).toContain('grading is pending or unavailable, not a pass')
    const { unmount } = renderView(finished({ status: 'ok', score: 100 }))
    expect(document.querySelectorAll('[data-slot="not-graded"]')[1]!.textContent).toContain('carries a score of 100 but no evaluation detail')
    unmount()
  })

  it('never shows the callout while the run is live or when an evaluation exists', () => {
    renderView({ ...transcriptFromViewState(initialState(), true), status: 'running' })
    expect(document.querySelector('[data-slot="not-graded"]')).toBeNull()
    renderView(transcriptFromViewState(workerCrash, false))
    expect(document.querySelector('[data-slot="not-graded"]')).toBeNull()
  })

  it('keeps the load-failure and not-found callouts', () => {
    renderView(transcriptFromViewState(initialState(), false), { error: '503 from /runs/r_1' })
    expect(screen.getByText('Could not load this run')).toBeTruthy()
    renderView(transcriptFromViewState(initialState(), false), { notFound: true })
    expect(screen.getByText('Run not found')).toBeTruthy()
  })
})

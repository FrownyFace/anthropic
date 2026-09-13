/**
 * The persisted (SQLite) projection of the two real-failure specimens, exactly as
 * GET /conversations/{id} returns it to its owner (src/lib/__fixtures__/conversation_*.json).
 * This is the default view of every finished run after a reload, and its tool_result blocks carry
 * only `is_error`, `fault` and the ToolError text — no outcome / error_class. The pills must still
 * read "unknown" for the write a dead worker left in flight and "not executed" for the read that
 * hit a dead sandbox — never a red "error" that blames the agent for infrastructure.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TranscriptView } from '@/components/transcript/TranscriptView'
import { TooltipProvider } from '@/components/ui/tooltip'

import { loadConversation } from './__fixtures__/load'
import { callStatus } from './callStatus'
import { transcriptFromMessages, type Transcript } from './transcript'

function renderView(t: Transcript) {
  return render(
    <TooltipProvider>
      <main>
        <TranscriptView transcript={t} diffs={[]} maxSteps={20} />
      </main>
    </TooltipProvider>,
  )
}

describe('persisted conversation_worker-crash (c_1a0982afb68bab29b5bc380 / r_2991dd9a680a)', () => {
  const d = loadConversation('conversation_worker-crash')
  const run = d.runs.find((r) => r.id === 'r_2991dd9a680a')!
  const t = transcriptFromMessages(d.messages, run)

  it('blocks carry no outcome / error_class (what the Store projects today)', () => {
    const results = d.messages.flatMap((m) => m.blocks).filter((b) => b.type === 'tool_result')
    expect(results.length).toBeGreaterThan(5)
    expect(results.every((b) => b.outcome === undefined && b.error_class === undefined)).toBe(true)
    const err = results.find((b) => b.is_error)!
    expect(err.fault).toBeNull()
    expect(JSON.parse(err.text!).code).toBe('EHARNESS')
  })

  it('the EHARNESS write is UNKNOWN with a real/harness class; no call is a red error', () => {
    const calls = t.turns.flatMap((x) => x.calls)
    expect(calls.map((c) => callStatus(c).attr)).not.toContain('error')
    const write = calls.find((c) => c.result?.errorCode === 'EHARNESS')!
    expect(write.tool).toBe('write_file')
    expect(write.path).toBe('CHANGELOG.md')
    expect(callStatus(write)).toMatchObject({ kind: 'unknown', attr: 'unknown', label: 'unknown' })
    expect(write.result?.errorClass).toMatchObject({
      origin: 'real',
      layer: 'harness',
      code: 'EHARNESS',
      outcome_known: false,
      label: 'real: harness worker interrupted mid-call (outcome unknown)',
    })
    // the agent read CHANGELOG.md back before touching it again
    expect(write.recovered).toBe(true)
    expect(t.status).toBe('ok')
    expect(t.score).toBe(100)
  })

  it('renders with no [data-status="error"] pill, one unknown pill and the real-origin badge', () => {
    renderView(t)
    expect(document.querySelectorAll('main [data-status="error"]')).toHaveLength(0)
    expect(document.querySelectorAll('main [data-status="unknown"]')).toHaveLength(1)
    expect(screen.getByText('real: EHARNESS')).toBeTruthy()
    expect(screen.getByText('read-back seen')).toBeTruthy()
    // the persisted projection alone has no evaluation: "not graded" until the record overlays it
    expect(document.querySelector('[data-slot="not-graded"]')!.textContent).toContain('carries a score of 100 but no evaluation detail')
  })
})

describe('persisted conversation_sandbox-lost (c_1a0982c9806c48810779d36 / r_77368c6c998d)', () => {
  const d = loadConversation('conversation_sandbox-lost')
  const run = d.runs.find((r) => r.id === 'r_77368c6c998d')!
  const t = transcriptFromMessages(d.messages, run)

  it('the ESANDBOX read is NOT EXECUTED with a real/sandbox class; interrupted, no score, not live', () => {
    const calls = t.turns.flatMap((x) => x.calls)
    expect(calls.map((c) => callStatus(c).attr)).not.toContain('error')
    const read = calls.find((c) => c.result?.errorCode === 'ESANDBOX')!
    expect(read.tool).toBe('read_file')
    expect(callStatus(read)).toMatchObject({ kind: 'not_executed', attr: 'not-executed', tone: 'error' })
    expect(read.result?.errorClass?.label).toBe('real: sandbox terminated or unavailable')
    expect(read.result?.errorClass?.outcome_known).toBe(true)
    expect(t.status).toBe('interrupted')
    expect(t.score).toBeNull()
    expect(t.live).toBe(false)
  })

  it('renders a not-executed pill, no error pill, and the "Not graded — the run was interrupted" callout', () => {
    renderView(t)
    expect(document.querySelectorAll('main [data-status="error"]')).toHaveLength(0)
    expect(document.querySelectorAll('main [data-status="not-executed"]').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('real: ESANDBOX')).toBeTruthy()
    const box = document.querySelector<HTMLElement>('[data-slot="not-graded"]')!
    expect(box.textContent).toContain('Not graded — the run was interrupted')
    expect(box.textContent).toContain('Grading was skipped; there is no score.')
    expect(box.getAttribute('data-status')).toBe('interrupted')
    expect(document.querySelector('[data-slot="score-rows"]')).toBeNull()
  })
})

import { describe, expect, it } from 'vitest'

import { callStatus, effectiveOutcome } from './callStatus'
import { parseToolResult, type ToolCallView } from './reducer'
import type { ErrorClass } from './types'

function call(over: Partial<ToolCallView> = {}): ToolCallView {
  return {
    seq: 0,
    step: 1,
    toolUseId: 'tu',
    tool: 'write_file',
    input: { path: 'CHANGELOG.md' },
    path: 'CHANGELOG.md',
    command: null,
    mutating: true,
    read: false,
    result: null,
    fault: null,
    recovered: false,
    ...over,
  }
}

const ok = parseToolResult('tu', JSON.stringify({ bytes_written: 3 }), false, 5)
const exit0 = parseToolResult('tu', JSON.stringify({ stdout: '', exit_code: 0 }), false, 5)
const exit2 = parseToolResult('tu', JSON.stringify({ stdout: '', stderr: 'nope', exit_code: 2 }), false, 5)
const err504 = parseToolResult('tu', JSON.stringify({ error: '504 Gateway Timeout', code: 'ETIMEDOUT' }), true, 3000)
const enoent = parseToolResult('tu', JSON.stringify({ error: 'No such file', code: 'ENOENT' }), true, 3)

const real = (over: Partial<ErrorClass>): ErrorClass => ({
  origin: 'real',
  layer: 'transport',
  code: 'ETRANSPORT',
  kind: null,
  label: 'real: transport failure harness<->sandbox-env (outcome unknown)',
  outcome_known: false,
  side_effect_applied: null,
  detail: null,
  ...over,
})

describe('callStatus', () => {
  it('running / pending when there is no result', () => {
    expect(callStatus(call(), true).kind).toBe('running')
    expect(callStatus(call(), false).kind).toBe('pending')
  })

  it('ok and failed from the exit code and is_error when nothing structured says otherwise', () => {
    expect(callStatus(call({ result: ok }))).toMatchObject({ kind: 'ok', label: 'ok', attr: 'ok' })
    expect(callStatus(call({ result: exit0 }))).toMatchObject({ kind: 'ok', label: 'exit 0' })
    expect(callStatus(call({ result: exit2 }))).toMatchObject({ kind: 'failed', label: 'exit 2', attr: 'error' })
    expect(callStatus(call({ result: enoent }))).toMatchObject({ kind: 'failed', label: 'error' })
  })

  it('a lost ack is UNKNOWN ("no ack"), never failed — the write may well have landed', () => {
    const st = callStatus(call({ result: err504, fault: { step: 1, kind: 'ack_lost', path: 'CHANGELOG.md', mode: 'transient' } }))
    expect(st).toMatchObject({ kind: 'unknown', label: 'no ack', tone: 'unknown', attr: 'unknown' })
    expect(st.detail).toMatch(/cannot know/)
  })

  it('outcome_known:false on the error class means unknown, even with no fault', () => {
    const st = callStatus(call({ result: { ...err504, errorClass: real({}) } }))
    expect(st).toMatchObject({ kind: 'unknown', label: 'unknown' })
    const landed = callStatus(call({ result: { ...err504, errorClass: real({ side_effect_applied: true }) } }))
    expect(landed.detail).toMatch(/had landed/)
  })

  it('the harness-reported outcome wins over everything else', () => {
    expect(callStatus(call({ result: { ...err504, outcome: 'unknown' } })).kind).toBe('unknown')
    expect(callStatus(call({ result: { ...enoent, outcome: 'not_executed' } })).kind).toBe('not_executed')
    expect(callStatus(call({ result: { ...enoent, outcome: 'failed' } })).kind).toBe('failed')
    expect(callStatus(call({ result: { ...ok, outcome: 'executed' } })).kind).toBe('ok')
    // an ack_lost fault with an explicit "executed" outcome: the harness's word stands
    expect(
      callStatus(call({ result: { ...ok, outcome: 'executed' }, fault: { step: 1, kind: 'ack_lost', path: 'x', mode: 'transient' } })).kind,
    ).toBe('ok')
  })

  it('an injected missing_file / denied_write is "not executed" (the sandbox never saw it); a staged one is a real failure', () => {
    const injected = callStatus(call({ result: enoent, fault: { step: 1, kind: 'missing_file', path: 'README.md', mode: 'transient' } }))
    expect(injected).toMatchObject({ kind: 'not_executed', label: 'not executed', tone: 'unknown', attr: 'not-executed' })
    const staged = callStatus(
      call({ result: enoent, fault: { step: 1, kind: 'missing_file', path: 'README.md', mode: 'sticky', origin: 'staged', layer: 'filesystem' } }),
    )
    expect(staged.kind).toBe('failed')
    const denied = callStatus(call({ result: exit2, fault: { step: 1, kind: 'denied_write', path: 'x', mode: 'transient', origin: 'injected' } }))
    expect(denied.kind).toBe('not_executed')
  })

  it('a real refusal at the boundary is not_executed in red', () => {
    const st = callStatus(
      call({ result: { ...enoent, outcome: 'not_executed', errorClass: real({ layer: 'boundary', code: 'EINVAL', outcome_known: true }) } }),
    )
    expect(st).toMatchObject({ kind: 'not_executed', tone: 'error' })
  })

  it('effectiveOutcome never looks at the error text', () => {
    const text = parseToolResult('tu', 'sandbox terminated; the operation may or may not have completed', true, 1)
    expect(effectiveOutcome(text, null)).toBeNull()
    expect(callStatus(call({ result: text })).kind).toBe('failed')
  })
})

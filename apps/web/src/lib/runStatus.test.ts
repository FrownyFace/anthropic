import { describe, expect, it } from 'vitest'

import { gradeOf, statusDot, statusTitle, statusTone } from './runStatus'
import type { EvaluateResponse } from './types'

const evaluation = (over: Partial<EvaluateResponse>): EvaluateResponse => ({
  episode_id: 'ep',
  score: 100,
  passed: true,
  checks: [{ id: 'a', ok: true, weight: 1, detail: '' }],
  tests: { passed: 6, failed: 0, errors: 0, output: '' },
  ledger: [],
  ...over,
})

describe('gradeOf', () => {
  it('uses the evaluation when present: every test and check must pass', () => {
    expect(gradeOf(evaluation({}))).toBe('passed')
    expect(gradeOf(evaluation({ passed: false, score: 40 }))).toBe('partial')
    expect(gradeOf(evaluation({ checks: [{ id: 'a', ok: false, weight: 1, detail: '' }], score: 60 }))).toBe('partial')
  })

  it('falls back to the score (100 = everything passed; less = something failed; null = ungraded)', () => {
    expect(gradeOf(null, 100)).toBe('passed')
    expect(gradeOf(null, 72)).toBe('partial')
    expect(gradeOf(null, 0)).toBe('partial')
    expect(gradeOf(null, null)).toBe('ungraded')
    expect(gradeOf(null, undefined)).toBe('ungraded')
    expect(gradeOf(undefined)).toBe('ungraded')
  })
})

describe('status tones', () => {
  it('ok is green only when passed; amber with failures; neutral when ungraded', () => {
    expect(statusTone('ok', 'passed')).toMatch(/emerald/)
    expect(statusTone('ok', 'partial')).toMatch(/amber/)
    expect(statusTone('ok', 'partial')).not.toMatch(/emerald/)
    expect(statusTone('ok', 'ungraded')).not.toMatch(/emerald/)
    expect(statusTone('ok')).not.toMatch(/emerald/)
    expect(statusDot('ok', 'passed')).toMatch(/emerald/)
    expect(statusDot('ok', 'partial')).toMatch(/amber/)
    expect(statusDot('ok', 'ungraded')).not.toMatch(/emerald/)
  })

  it('other statuses keep their own colour regardless of grade', () => {
    expect(statusTone('error', 'passed')).toMatch(/rose/)
    expect(statusTone('unevaluated', 'passed')).toMatch(/violet/)
    expect(statusTone('truncated', 'passed')).toMatch(/amber/)
    expect(statusDot('running')).toMatch(/sky/)
  })

  it('says what the chip means', () => {
    expect(statusTitle('ok', 'passed')).toBe('ok · passed')
    expect(statusTitle('ok', 'partial')).toBe('ok · graded with failures')
    expect(statusTitle('ok', 'ungraded')).toBe('ok · not graded')
    expect(statusTitle('interrupted')).toBe('interrupted')
  })
})

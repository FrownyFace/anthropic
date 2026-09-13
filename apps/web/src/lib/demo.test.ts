/**
 * The bundled replay is a product surface, not a fixture: if it drifts from the reducer the
 * Replay page silently shows something wrong. `lost-ack.json` is a real capture
 * (run r_e9bc5c8c6739, claude-haiku-4-5 on the deployed stack, exported with
 * scripts/export_demo.py). Fold it and assert the story it is supposed to tell.
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { allCalls, faultCount, fromRunRecord, recoveredCount } from './reducer'
import type { RunRecord } from './types'

// jsdom gives import.meta.url an http: scheme, so resolve off the vitest project root instead.
const record = JSON.parse(
  readFileSync(resolve(process.cwd(), 'public/demo/lost-ack.json'), 'utf8'),
) as RunRecord

describe('public/demo/lost-ack.json', () => {
  it('is a RunRecord with a monotonic, gap-free event id sequence', () => {
    expect(record.run_id).toBeTruthy()
    expect(record.scenario_id).toBe('lost-ack')
    expect(record.events.length).toBeGreaterThan(12)
    record.events.forEach((e, i) => {
      expect(e.id).toBe(i)
      expect(e.run_id).toBe(record.run_id)
      expect(typeof e.ts).toBe('string')
    })
  })

  it('carries no provider credential and masks the workspace id', () => {
    const blob = JSON.stringify(record)
    expect(blob).not.toMatch(/sk-ant-/)
    expect(record).not.toHaveProperty('sandbox_id')
    const started = record.events.find((e) => e.type === 'run.started')
    const ws = (started?.data as { anthropic_workspace?: string } | undefined)?.anthropic_workspace
    if (ws !== undefined) expect(ws).toBe('****')
  })

  it('folds into the lost-ack story: one ack_lost, read back, no second write, 100/100', () => {
    const s = fromRunRecord(record)
    expect(s.status).toBe('ok')
    expect(s.model).toBe('claude-haiku-4-5')

    // Exactly one injected fault, and it is the ack_lost on CHANGELOG.md.
    expect(faultCount(s)).toBe(1)
    const faulted = allCalls(s).find((c) => c.fault)!
    expect(faulted.fault?.kind).toBe('ack_lost')
    expect(faulted.fault?.path).toBe('CHANGELOG.md')
    expect(faulted.result?.isError).toBe(true)

    // The agent verified before doing anything else on that path.
    expect(recoveredCount(s)).toBe(1)
    expect(faulted.recovered).toBe(true)

    // …and never wrote CHANGELOG.md a second time.
    const changelogWrites = allCalls(s).filter((c) => c.mutating && c.path === 'CHANGELOG.md')
    expect(changelogWrites).toHaveLength(1)

    // The changelog ends up with exactly one 0.2.0 heading.
    // The verification read may be read_file or `cat`; either way the file shows one heading.
    const readBack = allCalls(s)
      .filter((c) => c.read && c.path === 'CHANGELOG.md' && c.result && !c.result.isError)
      .at(-1)
    const body = readBack?.result?.content ?? readBack?.result?.stdout ?? ''
    const headings = body.match(/^## \[0\.2\.0\]/gm) ?? []
    expect(headings).toHaveLength(1)

    // Tests ran and the grader is green.
    expect(s.evaluation?.score).toBe(100)
    expect(s.evaluation?.passed).toBe(true)
    expect(s.evaluation?.checks.every((c) => c.ok)).toBe(true)
    // 6 visible fixture tests + the hidden grader tests uploaded at evaluate time.
    expect(s.evaluation?.tests.passed).toBeGreaterThanOrEqual(6)
    expect(s.evaluation?.tests.failed).toBe(0)

    // The workspace panel has something to show.
    expect(s.diffs.map((d) => d.path).sort()).toEqual(['CHANGELOG.md', 'src/ratelimiter/version.py'])
    expect(s.files.filter((f) => f.status !== 'unchanged').map((f) => f.path).sort()).toEqual([
      'CHANGELOG.md',
      'src/ratelimiter/version.py',
    ])

    // And the Logs tab is not empty.
    expect(s.logs.length).toBeGreaterThan(3)
  })
})

/**
 * Every bundled replay, not just lost-ack: the invariants the reducer and SSE resume depend on,
 * plus "this run is worth shipping" (graded, at least one fault fired, nothing sensitive).
 */
import { DEMOS } from './replay'

describe('every bundled replay', () => {
  for (const d of DEMOS) {
    const rec = JSON.parse(readFileSync(resolve(process.cwd(), `public${d.file}`), 'utf8')) as RunRecord
    it(`${d.id}: contiguous ids, graded, at least one fault, no secrets`, () => {
      expect(rec.scenario_id).toBe(d.id)
      rec.events.forEach((e, i) => {
        expect(e.id).toBe(i)
        expect(e.run_id).toBe(rec.run_id)
      })
      const s = fromRunRecord(rec)
      expect(['ok', 'truncated']).toContain(s.status)
      expect(s.evaluation).not.toBeNull()
      expect(s.evaluation!.score).toBeGreaterThan(0)
      expect(faultCount(s)).toBeGreaterThanOrEqual(1)
      expect(JSON.stringify(rec)).not.toMatch(/sk-ant-/)
      expect(rec).not.toHaveProperty('sandbox_id')
    })
  }
})

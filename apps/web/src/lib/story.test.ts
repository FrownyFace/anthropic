import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { buildStory, checkpointAt } from './story'
import type { RunRecord, Scenario } from './types'

function load(name: string): RunRecord {
  return JSON.parse(readFileSync(resolve(process.cwd(), `public/demo/${name}.json`), 'utf8')) as RunRecord
}

const lostAck: Scenario = {
  id: 'lost-ack',
  title: 'Release 0.2.0 when the write ack is lost',
  description: '',
  task_prompt: 'Prepare release 0.2.0.',
  max_steps: 20,
  fault_kinds: ['ack_lost'],
}

describe('buildStory', () => {
  it('walks the lost-ack recording: prologue, one checkpoint per step, verdict', () => {
    const rec = load('lost-ack')
    const story = buildStory(rec.events, lostAck)
    const steps = new Set(rec.events.filter((e) => typeof e.step === 'number' && e.step > 0 && e.type !== 'log').map((e) => e.step))
    expect(story.checkpoints.length).toBe(steps.size + 2)
    expect(story.checkpoints[0]!.label).toBe('Start')
    expect(story.checkpoints[0]!.text).toMatch(/lost ack/)
    expect(story.checkpoints.at(-1)!.label).toBe('Verdict')
    expect(story.checkpoints.at(-1)!.text).toMatch(/Score 100\/100/)
    // indices are monotonic and end at the event count
    const idx = story.checkpoints.map((c) => c.index)
    expect([...idx].sort((a, b) => a - b)).toEqual(idx)
    expect(idx.at(-1)).toBe(rec.events.length)
  })

  it('narrates the injected fault and the read-back recovery from structured fields', () => {
    const rec = load('lost-ack')
    const story = buildStory(rec.events, lostAck)
    const faultStep = story.checkpoints.find((c) => c.tone === 'warn' && /lost ack/.test(c.text))
    expect(faultStep).toBeDefined()
    expect(faultStep!.text).toMatch(/cannot know whether the write landed/)
    const recovery = story.checkpoints.find((c) => /read-back is the right move/.test(c.text))
    expect(recovery).toBeDefined()
    expect(recovery!.step!).toBeGreaterThan(faultStep!.step!)
    expect(recovery!.tone).toBe('ok')
  })

  it('reports what the test command exited, not that "the tests passed" (that is the grader\'s call)', () => {
    const rec = load('lost-ack')
    const story = buildStory(rec.events, lostAck)
    const texts = story.checkpoints.map((c) => c.text)
    expect(texts.some((t) => /the test command exited 0/.test(t))).toBe(true)
    expect(texts.some((t) => /test suite passed|the tests failed/.test(t))).toBe(false)
  })

  it('classifies mutating commands with the reducer (shell redirection counts) and matches the read-back by argv', () => {
    const rec = load('lost-ack')
    const base = rec.events.filter((e) => e.type === 'run.started' || e.type === 'episode.reset')
    const mk = (id: number, step: number, type: 'tool.call' | 'tool.result', data: Record<string, unknown>) => ({
      id,
      ts: '2026-09-12T00:00:00Z',
      run_id: rec.run_id,
      type,
      step,
      data,
    })
    const ackLost = { step: 1, kind: 'ack_lost', path: 'CHANGELOG.md', mode: 'transient' }
    const events = [
      ...base,
      mk(100, 1, 'tool.call', { tool: 'run_command', input: { command: 'echo x >> CHANGELOG.md' }, tool_use_id: 'a' }),
      mk(101, 1, 'tool.result', { tool_use_id: 'a', output: JSON.stringify({ error: '504', code: 'ETIMEDOUT' }), is_error: true, duration_ms: 1, fault: ackLost }),
      mk(102, 2, 'tool.call', { tool: 'run_command', input: { command: 'cat CHANGELOG.md' }, tool_use_id: 'b' }),
      mk(103, 2, 'tool.result', { tool_use_id: 'b', output: JSON.stringify({ stdout: 'x', exit_code: 0 }), is_error: false, duration_ms: 1 }),
    ]
    const story = buildStory(events, lostAck)
    const s1 = story.checkpoints.find((c) => c.step === 1)!
    const s2 = story.checkpoints.find((c) => c.step === 2)!
    expect(s1.text).toMatch(/cannot know whether the write landed/)
    expect(s2.text).toMatch(/read-back is the right move/)
    expect(s2.tone).toBe('ok')
  })

  it('never fabricates a classification when the record has none', () => {
    const rec = load('lost-ack')
    // strip fault/error_class off every error result: the story must say "unknown", not guess
    const events = rec.events.map((e) =>
      e.type === 'tool.result' && e.data.is_error
        ? { ...e, data: { ...e.data, fault: undefined, error_class: undefined, output: 'sandbox terminated' } }
        : e.type === 'fault.fired'
          ? { ...e, type: 'log' as const }
          : e,
    )
    const story = buildStory(events, lostAck)
    const texts = story.checkpoints.map((c) => c.text).join(' ')
    expect(texts).toMatch(/what happened is unknown/)
    expect(texts).not.toMatch(/sandbox terminated/)
  })

  it('checkpointAt maps a cursor to the enclosing checkpoint', () => {
    const rec = load('lost-ack')
    const story = buildStory(rec.events, lostAck)
    expect(checkpointAt(story, 0)).toBe(0)
    expect(checkpointAt(story, rec.events.length)).toBe(story.checkpoints.length - 1)
    const mid = story.checkpoints[2]!
    expect(checkpointAt(story, mid.index)).toBe(2)
    expect(checkpointAt(story, mid.index - 1)).toBe(1)
  })

  it('works for every bundled replay', () => {
    for (const name of ['lost-ack', 'locked-file', 'missing-config', 'gauntlet']) {
      const rec = load(name)
      const story = buildStory(rec.events, null)
      expect(story.checkpoints.length).toBeGreaterThan(3)
      expect(story.checkpoints.some((c) => c.tone === 'warn')).toBe(true)
      expect(story.checkpoints.at(-1)!.text).toMatch(/Score \d+\/100/)
    }
  })
})

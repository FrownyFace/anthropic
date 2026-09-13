/**
 * End to end from real harness records (src/lib/__fixtures__) through the reducer, the call
 * status, the harness-state selectors and the replay story. These are the runs the product is
 * about: a worker really killed mid-write and resumed by a fresh worker, and a sandbox really
 * lost with no one to resume.
 */
import { describe, expect, it } from 'vitest'

import { loadFixture } from './__fixtures__/load'
import { callStatus } from './callStatus'
import { interruptionNote, sandboxChipText, sandboxLoss, workerChip } from './interruptions'
import { allCalls, faultCount, fromRunRecord, reduceAll } from './reducer'
import { ungradedNote } from './runStatus'
import { buildStory } from './story'
import { transcriptFromMessages, transcriptFromViewState } from './transcript'
import type { Event, Message, RunRecord, RunSummary } from './types'

const workerCrash = loadFixture('worker-crash-resumed')
const sandboxLost = loadFixture('sandbox-lost-interrupted')
const lostAckLive = loadFixture('lost-ack-live')

const DANGLING = 'toolu_01D1gyQSGgDbctbgj7hSbJe1'

describe('worker-crash run (r_1b83648dc693): killed mid-write, resumed by worker 2, graded 100', () => {
  const s = fromRunRecord(workerCrash)

  it('folds the interruption, the resume and the sandbox notice', () => {
    expect(s.status).toBe('ok')
    expect(s.workerGeneration).toBe(2)
    expect(s.interruptions).toHaveLength(1)
    const it = s.interruptions[0]!
    expect(it).toMatchObject({ step: 4, layer: 'harness', code: 'EHARNESS', planned: true, resumed: true, tool: 'write_file', path: 'CHANGELOG.md', tool_use_id: DANGLING, outcome_known: false })
    expect(it.label).toBe('real: harness worker interrupted mid-call (outcome unknown)')
    expect(s.resumes).toEqual([
      { worker_generation: 2, resumed_from_event_id: 27, dangling_tool_use_id: DANGLING, resumed_at: '2026-09-13T00:35:31.508Z' },
    ])
    expect(s.sandboxEvents).toEqual([{ sandbox_id: 'sb-PXk0uM6Ql3AhA', status: 'alive', reason: 'resumed by worker 2', step: 4 }])
    expect(s.sandboxAlive).toBe(true)
    expect(s.errorClass).toBeNull()
    expect(s.evaluationStatus).toBe('ok')
    expect(s.evaluation?.score).toBe(100)
  })

  it('the dangling write is UNKNOWN (never a failure) and the ledger later resolves it as landed', () => {
    const write = allCalls(s).find((c) => c.toolUseId === DANGLING)!
    expect(write.step).toBe(4)
    expect(write.result?.outcome).toBe('unknown')
    expect(write.result?.errorClass?.label).toBe('real: harness worker interrupted mid-call (outcome unknown)')
    expect(write.result?.errorClass?.origin).toBe('real')
    const st = callStatus(write)
    expect(st.kind).toBe('unknown')
    expect(st.attr).toBe('unknown')
    // `log` ev=ledger.resolution (after grading) upgraded side_effect_applied on this very call
    expect(write.result?.errorClass?.side_effect_applied).toBe(true)
    expect(st.detail).toMatch(/The ledger later confirmed it had landed/)
    // the resolution never rewrote what the agent saw
    expect(write.result?.isError).toBe(true)
    expect(write.result?.outcome).toBe('unknown')
    // the read-back at step 5 makes it "recovered" by the call-order heuristic too
    expect(write.recovered).toBe(true)
  })

  it('before the ledger.resolution event the side effect is still unresolved', () => {
    const idx = workerCrash.events.findIndex((e) => e.type === 'log' && e.data.ev === 'ledger.resolution')
    expect(idx).toBeGreaterThan(0)
    const before = reduceAll(workerCrash.events.slice(0, idx))
    const write = allCalls(before).find((c) => c.toolUseId === DANGLING)!
    expect(write.result?.errorClass?.side_effect_applied).toBeNull()
    expect(callStatus(write).detail).not.toMatch(/ledger later/)
  })

  it('header chips: "worker 2 · resumed", no sandbox loss', () => {
    expect(workerChip(s)).toMatchObject({ text: 'worker 2 · resumed', tone: 'resumed' })
    expect(workerChip(s)!.title).toMatch(/planned chaos/)
    expect(workerChip(s)!.title).toMatch(/at step 4/)
    expect(sandboxLoss(s)).toBeNull()
  })

  it('transcript callout: planned real failure, fresh worker from event 27, outcome unknown until the ledger says', () => {
    const note = interruptionNote(s.interruptions[0]!, s.resumes)
    expect(note.title).toBe('Real interruption: real: harness worker interrupted mid-call (outcome unknown) (planned chaos)')
    expect(note.body).toBe("A fresh worker (#2) resumed from event 27; the in-flight call's outcome is unknown until the ledger says otherwise.")
    expect(note.callLabel).toBe('write_file CHANGELOG.md')
    expect(note.toolUseId).toBe(DANGLING)
    expect(note.resumed).toBe(true)
    expect(note.workerGeneration).toBe(2)
  })

  it('story: the interruption, the resume, the read-back, and a passing verdict that mentions both workers', () => {
    const story = buildStory(workerCrash.events, null)
    const step4 = story.checkpoints.find((c) => c.step === 4)!
    expect(step4.kind).toBe('real-failure')
    expect(step4.tone).toBe('fail')
    expect(step4.text).toMatch(
      /The harness worker was really killed mid-write — a planned real failure\. Worker 2 picked the run up from event 27 and continued; the write's fate is unknown to the agent\./,
    )
    // no second, redundant failure sentence for the same call
    expect(step4.text).not.toMatch(/This was a real failure/)
    const step5 = story.checkpoints.find((c) => c.step === 5)!
    expect(step5.kind).toBe('recovery')
    expect(step5.text).toMatch(/read-back is the right move after an interrupted write/)
    const verdict = story.checkpoints.at(-1)!
    expect(verdict.kind).toBe('verdict')
    expect(verdict.tone).toBe('ok')
    expect(verdict.text).toMatch(/It took 2 harness workers to finish this run: 1 real interruption along the way\./)
    expect(verdict.text).toMatch(/Score 100\/100/)
    expect(verdict.text).toMatch(/The ledger later confirmed that the write to CHANGELOG\.md whose outcome the agent never learned had landed\./)
  })

  it('is graded, so there is no "not graded" note', () => {
    expect(ungradedNote(transcriptFromViewState(s, false))).toBeNull()
  })
})

describe('sandbox-loss run (r_ea3204c9e1e7): sandbox really lost at step 1, nobody resumed, not graded', () => {
  const s = fromRunRecord(sandboxLost)

  it('folds the run-level error class, the interruption and the sandbox notice', () => {
    expect(s.status).toBe('interrupted')
    expect(s.workerGeneration).toBe(1)
    expect(s.errorClass?.label).toBe('real: sandbox terminated or unavailable')
    expect(s.errorClass?.code).toBe('ESANDBOX')
    expect(s.interruptions).toHaveLength(1)
    expect(s.interruptions[0]).toMatchObject({ step: 1, layer: 'sandbox', code: 'ESANDBOX', planned: false, resumed: false, outcome_known: true })
    expect(s.resumes).toEqual([])
    expect(s.sandboxEvents).toEqual([
      { sandbox_id: 'sb-DAKQezAvuNlMf', status: 'terminated', reason: 'sandbox-env reported the sandbox as unavailable', step: 1 },
    ])
    expect(s.sandboxAlive).toBe(false)
    expect(s.evaluation).toBeNull()
    expect(s.evaluationStatus).toBe('skipped')
  })

  it('the read that hit the dead sandbox is "not executed" with a real origin — not the agent\'s failure', () => {
    const read = allCalls(s).find((c) => c.result?.isError)!
    expect(read.tool).toBe('read_file')
    expect(read.result?.outcome).toBe('not_executed')
    expect(read.result?.errorClass?.origin).toBe('real')
    expect(read.result?.sandbox).toEqual({ id: 'sb-DAKQezAvuNlMf', alive: false })
    const st = callStatus(read)
    expect(st.kind).toBe('not_executed')
    expect(st.tone).toBe('error')
  })

  it('header chips: "interrupted at step 1" (never the bare status word) and "sandbox lost at step 1"', () => {
    expect(workerChip(s)).toMatchObject({ text: 'interrupted at step 1', tone: 'interrupted' })
    expect(workerChip(s)!.title).toMatch(/unplanned/)
    const loss = sandboxLoss(s)!
    expect(loss).toMatchObject({ step: 1, status: 'terminated', sandboxId: 'sb-DAKQezAvuNlMf' })
    expect(sandboxChipText(loss)).toBe('sandbox lost at step 1')
  })

  it('transcript callout: unplanned, no worker resumed, the detail says why', () => {
    const note = interruptionNote(s.interruptions[0]!, s.resumes)
    expect(note.title).toBe('Real interruption: real: sandbox terminated or unavailable (unplanned)')
    expect(note.body).toBe('No worker resumed the run; the Modal Sandbox was terminated or became unreachable; the remaining steps could not have executed.')
    expect(note.resumed).toBe(false)
  })

  it('not graded — the run was interrupted, citing the ESANDBOX label', () => {
    const note = ungradedNote(transcriptFromViewState(s, false))!
    expect(note.tone).toBe('error')
    expect(note.title).toBe('Not graded — the run was interrupted')
    expect(note.body).toMatch(/real: sandbox terminated or unavailable/)
    expect(note.body).toMatch(/Grading was skipped; there is no score\./)
  })

  it('story: the sandbox loss at step 1 and an interrupted, ungraded verdict', () => {
    const story = buildStory(sandboxLost.events, null)
    const step1 = story.checkpoints.find((c) => c.step === 1)!
    expect(step1.kind).toBe('real-failure')
    expect(step1.text).toMatch(/The sandbox was really lost mid-read — an unplanned real failure\. No worker picked the run up; the read never ran\./)
    const verdict = story.checkpoints.at(-1)!
    expect(verdict.tone).toBe('fail')
    expect(verdict.text).toMatch(/The run ended interrupted: real: sandbox terminated or unavailable\./)
    expect(verdict.text).toMatch(/Grading was skipped, so there is no score\./)
    expect(verdict.text).not.toMatch(/Score \d+\/100/)
  })
})

describe('lost-ack live run (r_9606e7fe615f): fault.fired is attached by the harness turn, not the ledger index', () => {
  const WRITE = 'toolu_01TC81xqcTnP1ePo9MkkEddo'

  it('the record echoes the fault on tool.result AND emits fault.fired with data.step (ledger 6) != event.step (turn 4)', () => {
    const fired = lostAckLive.events.find((e) => e.type === 'fault.fired')!
    expect(fired.step).toBe(4)
    expect(fired.data.step).toBe(6)
    const s = fromRunRecord(lostAckLive)
    const write = allCalls(s).find((c) => c.toolUseId === WRITE)!
    expect(write.step).toBe(4)
    expect(write.fault?.kind).toBe('ack_lost')
    expect(write.fault?.origin).toBe('injected')
    expect(faultCount(s)).toBe(1)
    expect(s.faults).toHaveLength(1)
    expect(callStatus(write)).toMatchObject({ kind: 'unknown', label: 'no ack' })
    // ledger.resolution after grading: the write had landed
    expect(write.result?.errorClass?.side_effect_applied).toBe(true)
    expect(callStatus(write).detail).toMatch(/ledger later confirmed it had landed/)
  })

  it('with only the fault.fired event (no tool.result echo) the badge still lands on the step-4 write', () => {
    const events = lostAckLive.events.map((e) =>
      e.type === 'tool.result' && e.data.fault ? { ...e, data: { ...e.data, fault: undefined } } : e,
    )
    const s = reduceAll(events)
    const write = allCalls(s).find((c) => c.toolUseId === WRITE)!
    expect(write.fault?.kind).toBe('ack_lost')
    expect(allCalls(s).filter((c) => c.fault)).toHaveLength(1)
    expect(faultCount(s)).toBe(1)
    // a step-6 call (the pytest run) must NOT have picked it up
    const step6 = allCalls(s).filter((c) => c.step === 6)
    expect(step6.length).toBeGreaterThan(0)
    expect(step6.every((c) => c.fault === null)).toBe(true)
  })

  it('a fault.fired that arrives before its call is dropped, not mis-attached, and still counted', () => {
    const fired = lostAckLive.events.find((e) => e.type === 'fault.fired')!
    const s = reduceAll([fired])
    expect(s.faults).toHaveLength(1)
    expect(allCalls(s)).toHaveLength(0)
  })
})

// --------------------------------------------------------------------------- persisted parity

/**
 * The Store's messages projection of a run, built from its events the way ARCHITECTURE.md §4.5
 * describes: per step one assistant message (text + tool_use blocks) and one user message with
 * the tool_result blocks — `is_error`, `fault` and the ToolError text, and (as live today) NO
 * outcome / error_class / attempts / sandbox.
 */
function messagesFromEvents(rec: RunRecord): Message[] {
  const messages: Message[] = []
  let seq = 0
  let blockSeq = 0
  const mk = (role: Message['role'], step: number | null, blocks: Message['blocks']): Message => ({
    id: `m_${++seq}`,
    conversation_id: 'c_x',
    run_id: rec.run_id,
    seq,
    role,
    step,
    created_at: rec.created_at,
    blocks,
  })
  const prompt = rec.events.find((e) => e.type === 'episode.reset')?.data.task_prompt
  if (typeof prompt === 'string') {
    messages.push(mk('user', null, [{ id: `b_${++blockSeq}`, seq: blockSeq, type: 'text', text: prompt, truncated: false }]))
  }
  const steps = [...new Set(rec.events.filter((e) => typeof e.step === 'number' && e.step > 0).map((e) => e.step as number))]
  for (const step of steps) {
    const evs = rec.events.filter((e) => e.step === step)
    const assistant: Message['blocks'] = []
    const results: Message['blocks'] = []
    for (const e of evs) {
      if (e.type === 'turn.text') assistant.push({ id: `b_${++blockSeq}`, seq: blockSeq, type: 'text', text: String(e.data.text), truncated: false })
      if (e.type === 'tool.call') {
        assistant.push({
          id: `b_${++blockSeq}`,
          seq: blockSeq,
          type: 'tool_use',
          tool_use_id: String(e.data.tool_use_id),
          tool_name: String(e.data.tool),
          input: e.data.input as Record<string, unknown>,
          truncated: false,
        })
      }
      if (e.type === 'tool.result') {
        results.push({
          id: `b_${++blockSeq}`,
          seq: blockSeq,
          type: 'tool_result',
          tool_use_id: String(e.data.tool_use_id),
          tool_name: String(e.data.tool),
          text: String(e.data.output ?? ''),
          is_error: e.data.is_error === true,
          exit_code: null,
          duration_ms: Number(e.data.duration_ms ?? 0),
          fault: (e.data.fault as Message['blocks'][number]['fault']) ?? null,
          truncated: false,
        })
      }
    }
    if (assistant.length) messages.push(mk('assistant', step, assistant))
    if (results.length) messages.push(mk('user', step, results))
  }
  return messages
}

function summaryOf(rec: RunRecord): RunSummary {
  return {
    id: rec.run_id,
    status: rec.status,
    scenario_id: rec.scenario_id,
    model: rec.model,
    score: rec.evaluation?.score ?? null,
    created_at: rec.created_at,
    finished_at: rec.finished_at ?? null,
  }
}

describe('persisted (messages) and live (events) views agree on every call status', () => {
  for (const [name, rec] of [
    ['worker-crash-resumed', workerCrash],
    ['sandbox-lost-interrupted', sandboxLost],
    ['lost-ack-live', lostAckLive],
  ] as const) {
    it(`${name}: same kind / label / tone / origin per call, never a red "error" for a dead worker or sandbox`, () => {
      const live = transcriptFromViewState(reduceAll(rec.events.filter((e) => !(e.type === 'log' && e.data.ev === 'ledger.resolution'))), false)
      const persisted = transcriptFromMessages(messagesFromEvents(rec), summaryOf(rec))
      const shape = (t: typeof live) =>
        t.turns.flatMap((x) =>
          x.calls.map((c) => {
            const st = callStatus(c)
            return {
              step: x.step,
              id: c.toolUseId,
              tool: c.tool,
              kind: st.kind,
              label: st.label,
              tone: st.tone,
              attr: st.attr,
              origin: c.result?.errorClass?.origin ?? null,
              layer: c.result?.errorClass?.layer ?? null,
              fault: c.fault?.kind ?? null,
              recovered: c.recovered,
            }
          }),
        )
      const a = shape(live)
      const b = shape(persisted)
      expect(b).toEqual(a)
      expect(a.some((c) => c.attr === 'error')).toBe(false)
    })
  }

  it('worker-crash: the EHARNESS write is "unknown" with a real/harness class on the persisted path', () => {
    const t = transcriptFromMessages(messagesFromEvents(workerCrash), summaryOf(workerCrash))
    const write = t.turns.flatMap((x) => x.calls).find((c) => c.toolUseId === DANGLING)!
    expect(write.result?.errorCode).toBe('EHARNESS')
    expect(callStatus(write)).toMatchObject({ kind: 'unknown', attr: 'unknown' })
    expect(write.result?.errorClass).toMatchObject({ origin: 'real', layer: 'harness', code: 'EHARNESS', outcome_known: false })
    expect(write.result?.errorClass?.label).toBe('real: harness worker interrupted mid-call (outcome unknown)')
  })

  it('sandbox-loss: the ESANDBOX read is "not executed" with a real/sandbox class on the persisted path', () => {
    const t = transcriptFromMessages(messagesFromEvents(sandboxLost), summaryOf(sandboxLost))
    const read = t.turns.flatMap((x) => x.calls).find((c) => c.result?.isError)!
    expect(callStatus(read)).toMatchObject({ kind: 'not_executed', attr: 'not-executed', tone: 'error' })
    expect(read.result?.errorClass?.label).toBe('real: sandbox terminated or unavailable')
  })

  it('never guesses an origin for an ambiguous code (ENOENT / EACCES without a fault stay unclassified)', () => {
    const events: Event[] = [
      { id: 0, ts: 't', run_id: 'r', type: 'tool.call', step: 1, data: { tool: 'read_file', input: { path: 'x' }, tool_use_id: 'a' } },
      { id: 1, ts: 't', run_id: 'r', type: 'tool.result', step: 1, data: { tool_use_id: 'a', tool: 'read_file', output: JSON.stringify({ error: 'no such file', code: 'ENOENT' }), is_error: true, duration_ms: 1 } },
    ]
    const rec: RunRecord = { ...lostAckLive, run_id: 'r', events, evaluation: null }
    const t = transcriptFromMessages(messagesFromEvents(rec), summaryOf(rec))
    const read = t.turns[0]!.calls[0]!
    expect(read.result?.errorClass).toBeNull()
    expect(callStatus(read).kind).toBe('failed')
  })
})

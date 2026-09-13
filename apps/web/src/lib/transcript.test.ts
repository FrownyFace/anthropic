import { describe, expect, it } from 'vitest'

import { initialState, reduceAll } from './reducer'
import { transcriptFromMessages, transcriptFromViewState } from './transcript'
import type { Block, ConversationDetail, Event, Message, RunSummary } from './types'

// --------------------------------------------------------------------------- fixture builders

let blockSeq = 0
function block(partial: Partial<Block> & { type: Block['type'] }): Block {
  blockSeq += 1
  return { id: `b_${blockSeq}`, seq: blockSeq, truncated: false, ...partial }
}

let msgSeq = 0
function message(
  runId: string,
  role: Message['role'],
  step: number | null,
  blocks: Block[],
  conversationId = 'c_1',
): Message {
  msgSeq += 1
  return {
    id: `m_${msgSeq}`,
    conversation_id: conversationId,
    run_id: runId,
    seq: msgSeq,
    role,
    step,
    created_at: new Date(1_789_491_600_000 + msgSeq * 1000).toISOString(),
    blocks,
  }
}

const toolUse = (id: string, tool: string, input: Record<string, unknown>) =>
  block({ type: 'tool_use', tool_use_id: id, tool_name: tool, input })

const toolResult = (
  id: string,
  output: unknown,
  extra: Partial<Block> = {},
) =>
  block({
    type: 'tool_result',
    tool_use_id: id,
    text: typeof output === 'string' ? output : JSON.stringify(output),
    is_error: false,
    duration_ms: 120,
    ...extra,
  })

const ACK_LOST = { step: 1, kind: 'ack_lost', path: 'CHANGELOG.md', mode: 'transient' } as const

function run(partial: Partial<RunSummary> = {}): RunSummary {
  return {
    id: 'r_1',
    status: 'ok',
    scenario_id: 'lost-ack',
    model: 'claude-haiku-4-5',
    score: 100,
    created_at: '2026-09-12T17:00:00.000Z',
    finished_at: '2026-09-12T17:01:00.000Z',
    ...partial,
  }
}

/**
 * The lost-ack story as the Store projects it: task prompt → (think, text, write) → 504 with the
 * fault → read back → second write succeeds → pytest → summary. Plus one message from another run
 * that must be ignored.
 */
function lostAckDetail(): ConversationDetail {
  blockSeq = 0
  msgSeq = 0
  const r = 'r_1'
  const messages: Message[] = [
    message(r, 'user', null, [block({ type: 'text', text: 'Add a 0.2.0 changelog entry.' })]),
    message(r, 'assistant', 1, [
      block({ type: 'thinking', text: 'Read first, then write.' }),
      block({ type: 'thinking', text: 'Actually just write.' }),
      block({ type: 'text', text: 'Writing the changelog.' }),
      toolUse('tu_w1', 'write_file', { path: 'CHANGELOG.md', content: '## [0.2.0]\n' }),
    ]),
    message(r, 'user', 1, [
      toolResult('tu_w1', { error: '504 Gateway Timeout', code: 'ETIMEDOUT', path: 'CHANGELOG.md' }, {
        is_error: true,
        duration_ms: 3042,
        fault: ACK_LOST,
      }),
    ]),
    message(r, 'assistant', 2, [
      block({ type: 'text', text: 'That timed out; checking what landed.' }),
      toolUse('tu_r1', 'read_file', { path: 'CHANGELOG.md' }),
    ]),
    message(r, 'user', 2, [
      toolResult('tu_r1', { path: 'CHANGELOG.md', content: '## [0.2.0]\n', size: 11, sha256: 'abc' }),
    ]),
    message(r, 'assistant', 3, [
      toolUse('tu_w2', 'write_file', { path: 'CHANGELOG.md', content: '## [0.2.0]\n- x\n' }),
    ]),
    message(r, 'user', 3, [
      toolResult('tu_w2', { path: 'CHANGELOG.md', bytes_written: 15, sha256: 'def' }),
    ]),
    message(r, 'assistant', 4, [toolUse('tu_t', 'run_command', { command: 'python -m pytest -q' })]),
    message(r, 'user', 4, [
      toolResult('tu_t', { stdout: '6 passed', stderr: '', exit_code: 0, duration_ms: 900 }, { exit_code: 0 }),
    ]),
    message(r, 'assistant', null, [block({ type: 'text', text: 'Done: one 0.2.0 heading, tests green.' })]),
    // Another run in the same conversation.
    message('r_0', 'user', null, [block({ type: 'text', text: 'older prompt' })]),
    message('r_0', 'assistant', 1, [block({ type: 'text', text: 'older answer' })]),
  ]
  return {
    conversation: {
      id: 'c_1',
      user_id: 'u_x',
      scenario_id: 'lost-ack',
      title: 'lost-ack',
      created_at: '2026-09-12T17:00:00.000Z',
      updated_at: '2026-09-12T17:01:00.000Z',
      archived_at: null,
    },
    runs: [run({ id: 'r_0', score: 40 }), run()],
    messages,
  }
}

// --------------------------------------------------------------------------- persisted path

describe('transcriptFromMessages', () => {
  it('groups blocks into turns, attaches results by tool_use_id and takes the task prompt', () => {
    const d = lostAckDetail()
    const t = transcriptFromMessages(d.messages, d.runs[1]!)

    expect(t.runId).toBe('r_1')
    expect(t.status).toBe('ok')
    expect(t.scenarioId).toBe('lost-ack')
    expect(t.model).toBe('claude-haiku-4-5')
    expect(t.score).toBe(100)
    expect(t.evaluation).toBeNull()
    expect(t.error).toBeNull()
    expect(t.live).toBe(false)
    expect(t.taskPrompt).toBe('Add a 0.2.0 changelog entry.')

    expect(t.turns.map((x) => x.step)).toEqual([1, 2, 3, 4])
    const [s1, s2, , s4] = t.turns
    expect(s1!.thinking).toBe('Read first, then write.\n\nActually just write.')
    expect(s1!.texts).toEqual(['Writing the changelog.'])
    expect(s1!.calls).toHaveLength(1)
    expect(s2!.thinking).toBeNull()
    // The step-less summary joined the last turn, as the reducer does for a step-less event.
    expect(s4!.texts).toEqual(['Done: one 0.2.0 heading, tests green.'])

    const w1 = s1!.calls[0]!
    expect(w1.tool).toBe('write_file')
    expect(w1.path).toBe('CHANGELOG.md')
    expect(w1.mutating).toBe(true)
    expect(w1.read).toBe(false)
    expect(w1.result?.isError).toBe(true)
    expect(w1.result?.errorCode).toBe('ETIMEDOUT')
    expect(w1.result?.durationMs).toBe(3042)
    expect(w1.fault?.kind).toBe('ack_lost')

    const r1 = s2!.calls[0]!
    expect(r1.read).toBe(true)
    expect(r1.result?.content).toBe('## [0.2.0]\n')

    const pytest = s4!.calls[0]!
    expect(pytest.command).toBe('python -m pytest -q')
    expect(pytest.read).toBe(true)
    expect(pytest.result?.stdout).toBe('6 passed')
    expect(pytest.result?.exitCode).toBe(0)

    // Global call order is monotonic across turns.
    const seqs = t.turns.flatMap((x) => x.calls.map((c) => c.seq))
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b))
    expect(new Set(seqs).size).toBe(seqs.length)
  })

  it('marks the ack_lost write recovered: verified by a read, then a successful write', () => {
    const d = lostAckDetail()
    const t = transcriptFromMessages(d.messages, d.runs[1]!)
    const faulted = t.turns.flatMap((x) => x.calls).filter((c) => c.fault)
    expect(faulted).toHaveLength(1)
    expect(faulted[0]!.recovered).toBe(true)
  })

  it('ignores messages from other runs in the same conversation', () => {
    const d = lostAckDetail()
    const t = transcriptFromMessages(d.messages, d.runs[0]!)
    expect(t.runId).toBe('r_0')
    expect(t.taskPrompt).toBe('older prompt')
    expect(t.turns).toHaveLength(1)
    expect(t.turns[0]!.texts).toEqual(['older answer'])
    expect(t.score).toBe(40)
  })

  it('does NOT mark a blind retry as recovered (parity with the reducer)', () => {
    blockSeq = 0
    msgSeq = 0
    const messages: Message[] = [
      message('r_1', 'user', null, [block({ type: 'text', text: 'p' })]),
      message('r_1', 'assistant', 1, [toolUse('tu_1', 'write_file', { path: 'src/limits.py', content: 'a' })]),
      message('r_1', 'user', 1, [
        toolResult('tu_1', { error: 'Permission denied', code: 'EACCES' }, {
          is_error: true,
          fault: { step: 1, kind: 'denied_write', path: 'src/limits.py', mode: 'transient' },
        }),
      ]),
      message('r_1', 'assistant', 2, [toolUse('tu_2', 'write_file', { path: 'src/limits.py', content: 'a' })]),
      message('r_1', 'user', 2, [toolResult('tu_2', { path: 'src/limits.py', bytes_written: 1, sha256: 'y' })]),
    ]
    const t = transcriptFromMessages(messages, run())
    const faulted = t.turns.flatMap((x) => x.calls).find((c) => c.fault)!
    expect(faulted.fault?.kind).toBe('denied_write')
    expect(faulted.recovered).toBe(false)
  })

  it('synthesises a call for an orphan tool_result and tolerates unsorted input', () => {
    blockSeq = 0
    msgSeq = 0
    const messages: Message[] = [
      message('r_1', 'user', 3, [
        toolResult('tu_orphan', { stdout: 'x', stderr: '', exit_code: 0, duration_ms: 1 }, { tool_name: 'run_command' }),
      ]),
      message('r_1', 'user', null, [block({ type: 'text', text: 'prompt' })]),
    ]
    const t = transcriptFromMessages([...messages].reverse(), run({ status: 'running', finished_at: null }))
    expect(t.taskPrompt).toBe('prompt')
    expect(t.live).toBe(true)
    expect(t.turns).toHaveLength(1)
    expect(t.turns[0]!.step).toBe(3)
    expect(t.turns[0]!.calls[0]!.toolUseId).toBe('tu_orphan')
    expect(t.turns[0]!.calls[0]!.result?.stdout).toBe('x')
  })

  it("overlays the record's evaluation, error and status (the messages projection has none)", () => {
    const d = lostAckDetail()
    const evaluation = {
      episode_id: 'ep_1',
      score: 72,
      passed: true,
      checks: [{ id: 'no_duplicate_entry', ok: false, weight: 2, detail: 'two headings' }],
      tests: { passed: 6, failed: 0, errors: 0, output: '6 passed' },
      ledger: [],
    }
    const t = transcriptFromMessages(d.messages, d.runs[1]!, { evaluation, error: 'evaluate: 503', status: 'unevaluated' })
    expect(t.evaluation).toBe(evaluation)
    expect(t.score).toBe(72)
    expect(t.error).toBe('evaluate: 503')
    expect(t.status).toBe('unevaluated')
    expect(t.live).toBe(false)
    // an overlay that says nothing changes nothing
    const u = transcriptFromMessages(d.messages, d.runs[1]!, {})
    expect(u.status).toBe('ok')
    expect(u.evaluation).toBeNull()
  })

  it('never fabricates a grade: a finished run with no evaluation and no score stays ungraded', () => {
    const d = lostAckDetail()
    const t = transcriptFromMessages(d.messages, run({ score: null }))
    expect(t.evaluation).toBeNull()
    expect(t.score).toBeNull()
    expect(t.status).toBe('ok')
  })

  it('returns an empty transcript for a run with no messages yet', () => {
    const t = transcriptFromMessages([], run({ status: 'queued', score: null, finished_at: null }))
    expect(t.turns).toEqual([])
    expect(t.taskPrompt).toBeNull()
    expect(t.score).toBeNull()
    expect(t.live).toBe(true)
  })
})

// --------------------------------------------------------------------------- reducer path

let evId = 0
function ev(type: Event['type'], data: Record<string, unknown>, step?: number): Event {
  evId += 1
  return { id: evId, ts: new Date(1_789_491_600_000 + evId * 1000).toISOString(), run_id: 'r_1', type, step: step ?? null, data }
}

describe('transcriptFromViewState', () => {
  it('maps steps to turns and carries status, prompt, evaluation and the live flag', () => {
    evId = 0
    const state = reduceAll([
      ev('run.started', { scenario_id: 'lost-ack', model: 'claude-haiku-4-5' }),
      ev('episode.reset', { episode_id: 'ep', task_prompt: 'do it', files: [] }),
      ev('turn.thinking', { text: 'hmm' }, 1),
      ev('turn.text', { text: 'hello' }, 1),
      ev('tool.call', { tool: 'read_file', input: { path: 'README.md' }, tool_use_id: 'tu_1' }, 1),
      ev('tool.result', { tool_use_id: 'tu_1', output: JSON.stringify({ content: 'x' }), is_error: false, duration_ms: 1 }, 1),
      ev('episode.evaluated', { episode_id: 'ep', score: 60, passed: true, checks: [], tests: {}, ledger: [] }),
    ])
    const t = transcriptFromViewState(state, true)
    expect(t.runId).toBe('r_1')
    expect(t.status).toBe('running')
    expect(t.taskPrompt).toBe('do it')
    expect(t.live).toBe(true)
    expect(t.score).toBe(60)
    expect(t.evaluation?.score).toBe(60)
    expect(t.turns).toHaveLength(1)
    expect(t.turns[0]).toMatchObject({ step: 1, thinking: 'hmm', texts: ['hello'] })
    expect(t.turns[0]!.calls[0]!.result?.content).toBe('x')
  })

  it('is empty for the initial state', () => {
    const t = transcriptFromViewState(initialState(), false)
    expect(t.turns).toEqual([])
    expect(t.score).toBeNull()
    expect(t.error).toBeNull()
    expect(t.status).toBe('queued')
  })

  it('carries the run error from run.finished', () => {
    evId = 0
    const state = reduceAll([ev('run.finished', { status: 'error', error: 'provider 529' })])
    const t = transcriptFromViewState(state, false)
    expect(t.status).toBe('error')
    expect(t.error).toBe('provider 529')
  })
})

// --------------------------------------------------------------------------- parity

describe('live and persisted paths agree', () => {
  it('derives the same tool/path/fault/recovered sequence for the lost-ack story', () => {
    evId = 0
    const live = reduceAll([
      ev('episode.reset', { episode_id: 'ep', task_prompt: 'Add a 0.2.0 changelog entry.', files: [] }),
      ev('turn.thinking', { text: 'Read first, then write.' }, 1),
      ev('turn.thinking', { text: 'Actually just write.' }, 1),
      ev('turn.text', { text: 'Writing the changelog.' }, 1),
      ev('tool.call', { tool: 'write_file', input: { path: 'CHANGELOG.md', content: '## [0.2.0]\n' }, tool_use_id: 'tu_w1' }, 1),
      ev('tool.result', { tool_use_id: 'tu_w1', output: JSON.stringify({ error: '504', code: 'ETIMEDOUT' }), is_error: true, duration_ms: 3042, fault: ACK_LOST }, 1),
      ev('turn.text', { text: 'That timed out; checking what landed.' }, 2),
      ev('tool.call', { tool: 'read_file', input: { path: 'CHANGELOG.md' }, tool_use_id: 'tu_r1' }, 2),
      ev('tool.result', { tool_use_id: 'tu_r1', output: JSON.stringify({ content: '## [0.2.0]\n' }), is_error: false, duration_ms: 5 }, 2),
      ev('tool.call', { tool: 'write_file', input: { path: 'CHANGELOG.md', content: '## [0.2.0]\n- x\n' }, tool_use_id: 'tu_w2' }, 3),
      ev('tool.result', { tool_use_id: 'tu_w2', output: JSON.stringify({ bytes_written: 15 }), is_error: false, duration_ms: 5 }, 3),
      ev('tool.call', { tool: 'run_command', input: { command: 'python -m pytest -q' }, tool_use_id: 'tu_t' }, 4),
      ev('tool.result', { tool_use_id: 'tu_t', output: JSON.stringify({ stdout: '6 passed', exit_code: 0 }), is_error: false, duration_ms: 900 }, 4),
      ev('turn.text', { text: 'Done: one 0.2.0 heading, tests green.' }),
    ])
    const a = transcriptFromViewState(live, false)
    const d = lostAckDetail()
    const b = transcriptFromMessages(d.messages, d.runs[1]!)

    const shape = (t: typeof a) =>
      t.turns.map((x) => ({
        step: x.step,
        thinking: x.thinking,
        texts: x.texts,
        calls: x.calls.map((c) => ({
          tool: c.tool,
          path: c.path,
          command: c.command,
          mutating: c.mutating,
          read: c.read,
          isError: c.result?.isError ?? null,
          fault: c.fault?.kind ?? null,
          recovered: c.recovered,
        })),
      }))
    expect(shape(b)).toEqual(shape(a))
    expect(b.taskPrompt).toBe(a.taskPrompt)
  })
})

import { describe, expect, it } from 'vitest'
import {
  allCalls,
  argvTokens,
  asErrorClass,
  asRunStatus,
  callTouches,
  classifyCall,
  commandIsMutating,
  commandIsRead,
  elapsedMs,
  faultCount,
  fromRunRecord,
  initialState,
  isTerminal,
  normalisePath,
  parseToolResult,
  recoveredCount,
  reduce,
  reduceAll,
  sumLlmUsage,
} from './reducer'
import type { Event, RunRecord } from './types'

let nextId = 0
function ev(type: Event['type'], data: Record<string, unknown>, step?: number): Event {
  return {
    id: nextId++,
    ts: new Date(1_789_491_600_000 + nextId * 1000).toISOString(),
    run_id: 'r_test',
    type,
    step: step ?? null,
    data,
  }
}
function reset() {
  nextId = 0
}

function call(step: number, tool: string, input: Record<string, unknown>, id: string): Event {
  return ev('tool.call', { tool, input, tool_use_id: id }, step)
}
function result(
  step: number,
  id: string,
  output: unknown,
  isError = false,
  fault?: Record<string, unknown>,
): Event {
  return ev(
    'tool.result',
    {
      tool_use_id: id,
      output: typeof output === 'string' ? output : JSON.stringify(output),
      is_error: isError,
      duration_ms: 120,
      ...(fault ? { fault } : {}),
    },
    step,
  )
}

describe('path + verb helpers (must agree with GRADING.md)', () => {
  it('normalises quoted, ./ and /workspace/ prefixed paths', () => {
    expect(normalisePath('"config/settings.json"')).toBe('config/settings.json')
    expect(normalisePath("'./src/limits.py'")).toBe('src/limits.py')
    expect(normalisePath('/workspace/CHANGELOG.md')).toBe('CHANGELOG.md')
    expect(normalisePath('src/ratelimiter/')).toBe('src/ratelimiter')
  })

  it('splits argv honouring quotes', () => {
    expect(argvTokens(`grep -c '^## \\[0.2.0\\]' CHANGELOG.md`)).toEqual([
      'grep',
      '-c',
      '^## \\[0.2.0\\]',
      'CHANGELOG.md',
    ])
  })

  it('classifies mutating vs read commands', () => {
    expect(commandIsMutating('echo hi >> CHANGELOG.md')).toBe(true)
    expect(commandIsMutating("sed -i 's/a/b/' src/limits.py")).toBe(true)
    expect(commandIsMutating('cat README.md')).toBe(false)
    expect(commandIsRead('cat README.md')).toBe(true)
    expect(commandIsRead('python -m pytest -q')).toBe(true)
    // A mutating command is never also a "read", even when it mentions a read verb.
    expect(commandIsRead('cat a.txt > b.txt')).toBe(false)
  })
})

describe('parseToolResult', () => {
  it('pulls stdout/stderr/exit_code out of run_command JSON', () => {
    const r = parseToolResult(
      't1',
      JSON.stringify({ stdout: 'ok\n', stderr: '', exit_code: 0, duration_ms: 5 }),
      false,
      9,
    )
    expect(r.stdout).toBe('ok\n')
    expect(r.exitCode).toBe(0)
    expect(r.errorText).toBeNull()
  })

  it('pulls the ToolError shape out of an injected failure', () => {
    const r = parseToolResult(
      't2',
      JSON.stringify({
        error: 'cat: config/settings.json: No such file or directory',
        code: 'ENOENT',
        path: 'config/settings.json',
      }),
      true,
      3,
    )
    expect(r.errorCode).toBe('ENOENT')
    expect(r.errorText).toContain('No such file or directory')
  })

  it('keeps plain-text error output rather than dropping it', () => {
    const r = parseToolResult('t3', 'boom', true, 1)
    expect(r.errorText).toBe('boom')
    expect(r.errorCode).toBeNull()
  })
})

describe('fold', () => {
  it('records run metadata and marks the run running', () => {
    reset()
    const s = reduceAll([
      ev('run.started', {
        scenario_id: 'lost-ack',
        model: 'claude-haiku-4-5',
        seed: 7,
        max_steps: 20,
      }),
      ev('episode.reset', { episode_id: 'ep_1', task_prompt: 'do the thing', files: [] }),
    ])
    expect(s.scenarioId).toBe('lost-ack')
    expect(s.model).toBe('claude-haiku-4-5')
    expect(s.maxSteps).toBe(20)
    expect(s.episodeId).toBe('ep_1')
    expect(s.taskPrompt).toBe('do the thing')
    expect(s.status).toBe('running')
  })

  it('pairs a tool.result with its tool.call by tool_use_id', () => {
    reset()
    const s = reduceAll([
      call(1, 'read_file', { path: 'README.md' }, 'tu_1'),
      result(1, 'tu_1', { path: 'README.md', content: 'hi', size: 2, sha256: 'x' }),
    ])
    const calls = allCalls(s)
    expect(calls).toHaveLength(1)
    expect(calls[0]!.result?.content).toBe('hi')
    expect(calls[0]!.read).toBe(true)
  })

  it('synthesises a call when only the result survived a reconnect', () => {
    reset()
    const s = reduceAll([result(3, 'tu_orphan', { stdout: 'x', exit_code: 0 })])
    const calls = allCalls(s)
    expect(calls).toHaveLength(1)
    expect(calls[0]!.toolUseId).toBe('tu_orphan')
    expect(calls[0]!.result?.stdout).toBe('x')
  })

  it('ignores a duplicated tool.call (same tool_use_id in the same step)', () => {
    reset()
    const c = call(1, 'read_file', { path: 'README.md' }, 'tu_1')
    const s = reduceAll([c, { ...c, id: 99 }])
    expect(allCalls(s)).toHaveLength(1)
  })

  it('collects turn text per step and keeps steps ordered', () => {
    reset()
    const s = reduceAll([
      ev('turn.text', { text: 'second' }, 2),
      ev('turn.text', { text: 'first' }, 1),
      ev('turn.text', { text: 'also first' }, 1),
    ])
    expect(s.steps.map((x) => x.step)).toEqual([1, 2])
    expect(s.steps[0]!.texts).toEqual(['first', 'also first'])
    expect(s.step).toBe(2)
  })

  it('attaches a fault from tool.result.fault and from a fault.fired event', () => {
    reset()
    const s = reduceAll([
      call(1, 'write_file', { path: 'CHANGELOG.md', content: 'x' }, 'tu_w'),
      result(1, 'tu_w', { error: '504 …', code: 'ETIMEDOUT' }, true, {
        step: 1,
        kind: 'ack_lost',
        path: 'CHANGELOG.md',
        mode: 'transient',
      }),
      call(2, 'read_file', { path: 'README.md' }, 'tu_r'),
      ev('fault.fired', { step: 2, kind: 'missing_file', path: 'README.md', mode: 'transient' }, 2),
    ])
    const calls = allCalls(s)
    expect(calls[0]!.fault?.kind).toBe('ack_lost')
    expect(calls[1]!.fault?.kind).toBe('missing_file')
    expect(faultCount(s)).toBe(2)
    expect(s.faults.map((f) => f.kind).sort()).toEqual(['ack_lost', 'missing_file'])
  })

  it('records the evaluation and the terminal status', () => {
    reset()
    const s = reduceAll([
      ev('episode.evaluated', {
        episode_id: 'ep_1',
        score: 100,
        passed: true,
        checks: [{ id: 'no_duplicate_entry', ok: true, weight: 2, detail: 'one heading' }],
        tests: { passed: 6, failed: 0, errors: 0, output: '6 passed' },
        ledger: [],
      }),
      ev('run.finished', {
        status: 'ok',
        usage: { input_tokens: 10, output_tokens: 2 },
        duration_ms: 3000,
      }),
    ])
    expect(s.evaluation?.score).toBe(100)
    expect(s.evaluation?.checks[0]!.id).toBe('no_duplicate_entry')
    expect(s.status).toBe('ok')
    expect(isTerminal(s.status)).toBe(true)
    expect(s.usage.input_tokens).toBe(10)
    expect(elapsedMs(s)).toBe(3000)
  })

  it('run.finished with a missing or unknown status keeps the previous status — never defaults to ok', () => {
    reset()
    const running = reduceAll([ev('run.started', { scenario_id: 'lost-ack', model: 'm' })])
    expect(reduce(running, ev('run.finished', {})).status).toBe('running')
    expect(reduce(running, ev('run.finished', { status: 'done' })).status).toBe('running')
    expect(reduce(running, ev('run.finished', { status: 'unevaluated' })).status).toBe('unevaluated')
    expect(asRunStatus('ok')).toBe('ok')
    expect(asRunStatus('OK')).toBeNull()
    expect(asRunStatus(undefined)).toBeNull()
  })

  it('fromRunRecord ignores an unrecognised record status', () => {
    reset()
    const rec = {
      run_id: 'r_x',
      status: 'weird' as unknown as RunRecord['status'],
      scenario_id: 's',
      model: 'm',
      max_steps: 5,
      created_at: '2026-09-12T00:00:00Z',
      events: [ev('run.started', { scenario_id: 's', model: 'm' })],
      usage: { input_tokens: 0, output_tokens: 0 },
    }
    expect(fromRunRecord(rec).status).toBe('running')
  })

  it('treats a missing outcome_known on error_class as unknown (unknown execution is not failed execution)', () => {
    const base = { origin: 'real', layer: 'transport', code: 'ETRANSPORT', label: 'x' }
    expect(asErrorClass(base)?.outcome_known).toBe(false)
    expect(asErrorClass({ ...base, outcome_known: true })?.outcome_known).toBe(true)
    expect(asErrorClass({ ...base, outcome_known: false })?.outcome_known).toBe(false)
    expect(asErrorClass({ ...base, outcome_known: 'yes' })?.outcome_known).toBe(false)
  })

  it('buffers mirrored log lines', () => {
    reset()
    const s = reduceAll([
      ev('log', { svc: 'sandbox-env', lvl: 'warn', ev: 'fault.fired', msg: 'ack withheld' }),
    ])
    expect(s.logs).toHaveLength(1)
    expect(s.logs[0]!.svc).toBe('sandbox-env')
  })

  it('never mutates the state it is given', () => {
    reset()
    const before = initialState()
    const frozen = Object.freeze({ ...before, steps: Object.freeze([...before.steps]) })
    const after = reduce(frozen as typeof before, ev('run.started', { scenario_id: 'x', model: 'm' }))
    expect(before.scenarioId).toBeNull()
    expect(after.scenarioId).toBe('x')
  })
})

describe('recovery derivation', () => {
  it('marks ack_lost recovered when the agent reads the file back', () => {
    reset()
    const s = reduceAll([
      call(1, 'write_file', { path: 'CHANGELOG.md', content: 'v2' }, 'tu_w'),
      result(1, 'tu_w', { error: '504 …', code: 'ETIMEDOUT' }, true, {
        step: 1,
        kind: 'ack_lost',
        path: 'CHANGELOG.md',
        mode: 'transient',
      }),
      call(2, 'read_file', { path: 'CHANGELOG.md' }, 'tu_r'),
      result(2, 'tu_r', { path: 'CHANGELOG.md', content: 'v2', size: 2, sha256: 'x' }),
    ])
    expect(recoveredCount(s)).toBe(1)
  })

  it('does NOT mark a blind retry as recovered', () => {
    reset()
    const s = reduceAll([
      call(1, 'write_file', { path: 'src/limits.py', content: 'a' }, 'tu_1'),
      result(1, 'tu_1', { error: 'Permission denied', code: 'EACCES' }, true, {
        step: 1,
        kind: 'denied_write',
        path: 'src/limits.py',
        mode: 'transient',
      }),
      // straight back to writing, with no verification read in between
      call(2, 'write_file', { path: 'src/limits.py', content: 'a' }, 'tu_2'),
      result(2, 'tu_2', { path: 'src/limits.py', bytes_written: 1, sha256: 'y' }),
    ])
    expect(faultCount(s)).toBe(1)
    expect(recoveredCount(s)).toBe(0)
  })

  it('marks denied_write recovered when a read precedes the successful retry', () => {
    reset()
    const s = reduceAll([
      call(1, 'write_file', { path: 'src/limits.py', content: 'a' }, 'tu_1'),
      result(1, 'tu_1', { error: 'Permission denied', code: 'EACCES' }, true, {
        step: 1,
        kind: 'denied_write',
        path: 'src/limits.py',
        mode: 'transient',
      }),
      call(2, 'run_command', { command: 'cat src/limits.py' }, 'tu_2'),
      result(2, 'tu_2', { stdout: 'old', stderr: '', exit_code: 0, duration_ms: 4 }),
      call(3, 'write_file', { path: 'src/limits.py', content: 'a' }, 'tu_3'),
      result(3, 'tu_3', { path: 'src/limits.py', bytes_written: 1, sha256: 'y' }),
    ])
    expect(recoveredCount(s)).toBe(1)
  })

  it('matches run_command argv tokens against the fault path', () => {
    reset()
    const s = reduceAll([call(1, 'run_command', { command: 'cat /workspace/README.md' }, 'tu_1')])
    const c = allCalls(s)[0]!
    expect(callTouches(c, 'README.md')).toBe(true)
    expect(callTouches(c, 'CHANGELOG.md')).toBe(false)
  })

  it('leaves a fault unrecovered when nothing later touches the path', () => {
    reset()
    const s = reduceAll([
      call(1, 'read_file', { path: 'config/settings.json' }, 'tu_1'),
      result(1, 'tu_1', { error: 'No such file', code: 'ENOENT' }, true, {
        step: 1,
        kind: 'missing_file',
        path: 'config/settings.json',
        mode: 'sticky',
      }),
      call(2, 'run_command', { command: 'python -m pytest -q' }, 'tu_2'),
      result(2, 'tu_2', { stdout: 'fail', stderr: '', exit_code: 1, duration_ms: 9 }),
    ])
    expect(recoveredCount(s)).toBe(0)
  })
})

describe('thinking + llm.call (additive)', () => {
  const llm = (step: number, attempt: number, usage: Record<string, number>, extra: Record<string, unknown> = {}) =>
    ev('llm.call', { attempt, model: 'claude-haiku-4-5', stop_reason: 'tool_use', usage, duration_ms: 400, ...extra }, step)

  it('classifyCall is the same classification the fold applies', () => {
    expect(classifyCall('write_file', { path: './CHANGELOG.md' })).toEqual({
      path: 'CHANGELOG.md',
      command: null,
      mutating: true,
      read: false,
    })
    expect(classifyCall('run_command', { command: 'cat README.md' })).toMatchObject({
      command: 'cat README.md',
      mutating: false,
      read: true,
    })
    expect(classifyCall('list_dir', {})).toMatchObject({ path: '.', read: true })
  })

  it('collects turn.thinking per step, concatenating several blocks', () => {
    reset()
    const s = reduceAll([
      ev('turn.thinking', { text: 'first thought' }, 1),
      ev('turn.thinking', { text: 'second thought' }, 1),
      ev('turn.thinking', { text: '   ' }, 1), // blank is ignored
      ev('turn.text', { text: 'hello' }, 1),
      ev('turn.text', { text: 'no thinking here' }, 2),
    ])
    expect(s.steps[0]!.thinking).toBe('first thought\n\nsecond thought')
    expect(s.steps[0]!.texts).toEqual(['hello'])
    expect(s.steps[1]!.thinking).toBeNull()
    expect(initialState().llmCalls).toEqual([])
  })

  it('records llm.call views and accumulates usage while the run is in flight', () => {
    reset()
    const s = reduceAll([
      ev('run.started', { scenario_id: 'lost-ack', model: 'claude-haiku-4-5' }),
      llm(1, 1, { input_tokens: 100, output_tokens: 10, cache_read_input_tokens: 50 }, { request_id: 'req_1' }),
      llm(2, 1, { input_tokens: 200, output_tokens: 20, cache_read_input_tokens: 60 }),
    ])
    expect(s.llmCalls).toHaveLength(2)
    expect(s.llmCalls[0]).toEqual({
      step: 1,
      attempt: 1,
      model: 'claude-haiku-4-5',
      stopReason: 'tool_use',
      usage: { input_tokens: 100, output_tokens: 10, cache_read_input_tokens: 50 },
      durationMs: 400,
      requestId: 'req_1',
      error: null,
    })
    expect(s.usage).toEqual({
      input_tokens: 300,
      output_tokens: 30,
      cache_read_input_tokens: 110,
      cache_creation_input_tokens: 0,
    })
  })

  it('counts only the last attempt of a retried step', () => {
    reset()
    const s = reduceAll([
      llm(1, 1, { input_tokens: 100, output_tokens: 0 }, { error: '429 rate limited', stop_reason: null }),
      llm(1, 2, { input_tokens: 100, output_tokens: 10 }),
      llm(2, 1, { input_tokens: 150, output_tokens: 15 }),
    ])
    expect(s.llmCalls.map((c) => [c.step, c.attempt])).toEqual([
      [1, 1],
      [1, 2],
      [2, 1],
    ])
    expect(s.llmCalls[0]!.error).toBe('429 rate limited')
    expect(s.usage).toEqual({ input_tokens: 250, output_tokens: 25 })
    expect(sumLlmUsage(s.llmCalls)).toEqual({ input_tokens: 250, output_tokens: 25 })
  })

  it('replaces a replayed (step, attempt) instead of appending it', () => {
    reset()
    const a = llm(1, 1, { input_tokens: 100, output_tokens: 10 })
    const s = reduceAll([a, { ...a, id: 99 }])
    expect(s.llmCalls).toHaveLength(1)
    expect(s.usage).toEqual({ input_tokens: 100, output_tokens: 10 })
  })

  it('lets run.finished usage win over the accumulated sum, and stops accumulating after it', () => {
    reset()
    const s = reduceAll([
      llm(1, 1, { input_tokens: 100, output_tokens: 10 }),
      ev('run.finished', { status: 'ok', usage: { input_tokens: 999, output_tokens: 99, cache_read_input_tokens: 7 }, duration_ms: 1 }),
      llm(2, 1, { input_tokens: 5, output_tokens: 5 }), // late frame after finish: recorded, not summed
    ])
    expect(s.usage).toEqual({ input_tokens: 999, output_tokens: 99, cache_read_input_tokens: 7 })
    expect(s.llmCalls).toHaveLength(2)
  })

  it('run.finished without usage keeps what was accumulated', () => {
    reset()
    const s = reduceAll([
      llm(1, 1, { input_tokens: 100, output_tokens: 10 }),
      ev('run.finished', { status: 'error', error: 'boom' }),
    ])
    expect(s.usage).toEqual({ input_tokens: 100, output_tokens: 10 })
    expect(s.error).toBe('boom')
  })

  it('fromRunRecord: record usage wins when terminal or non-zero; folded llm sums fill an in-flight {0,0}', () => {
    reset()
    const events = [llm(1, 1, { input_tokens: 100, output_tokens: 10 })]
    const base: RunRecord = {
      run_id: 'r_1',
      status: 'running',
      scenario_id: 'lost-ack',
      model: 'claude-haiku-4-5',
      max_steps: 20,
      created_at: '2026-09-12T17:00:00.000Z',
      events,
      usage: { input_tokens: 0, output_tokens: 0 },
      task_prompt: 'from the record',
    }
    const inFlight = fromRunRecord(base)
    expect(inFlight.usage).toEqual({ input_tokens: 100, output_tokens: 10 })
    expect(inFlight.llmCalls).toHaveLength(1)
    expect(inFlight.taskPrompt).toBe('from the record')

    const serverTotals = fromRunRecord({ ...base, usage: { input_tokens: 120, output_tokens: 12 } })
    expect(serverTotals.usage).toEqual({ input_tokens: 120, output_tokens: 12 })

    const finished = fromRunRecord({ ...base, status: 'ok', usage: { input_tokens: 0, output_tokens: 0 } })
    expect(finished.usage).toEqual({ input_tokens: 0, output_tokens: 0 })
  })
})

describe('fromRunRecord', () => {
  it('lets record-level fields win over the folded events', () => {
    reset()
    const rec: RunRecord = {
      run_id: 'r_1',
      status: 'truncated',
      scenario_id: 'locked-file',
      model: 'claude-sonnet-5',
      seed: 3,
      max_steps: 5,
      episode_id: 'ep_9',
      created_at: '2026-09-12T17:00:00.000Z',
      finished_at: '2026-09-12T17:00:30.000Z',
      events: [ev('run.started', { scenario_id: 'locked-file', model: 'claude-haiku-4-5' })],
      evaluation: null,
      usage: { input_tokens: 5, output_tokens: 1 },
      error: null,
    }
    const s = fromRunRecord(rec)
    expect(s.status).toBe('truncated')
    expect(s.model).toBe('claude-sonnet-5')
    expect(s.maxSteps).toBe(5)
    expect(elapsedMs(s)).toBe(30_000)
  })
})

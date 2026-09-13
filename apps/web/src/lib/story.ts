/**
 * Plain-English tour of a run, one checkpoint per step, built ONLY from structured event fields
 * (tool names and inputs, is_error, fault / error_class / outcome, exit codes, the grader's
 * checks). Message text is never parsed; the agent's own prose is only quoted. Calls are
 * classified by the reducer's own `classifyCall` / `callTouches`, so "mutating" and "touches this
 * path" mean the same thing here as on the transcript.
 */

import { checkText } from './checks'
import { codeText, taxonomyLabel } from './codes'
import { firstSentence } from './format'
import { callTouches, classifyCall, normalisePath, parseToolResult } from './reducer'
import type {
  ErrorClass,
  EvaluateResponse,
  Event,
  FaultFired,
  Scenario,
  ToolCallData,
  ToolResultData,
} from './types'

export type StoryTone = 'info' | 'ok' | 'warn' | 'fail'

export interface Checkpoint {
  /** How many events are folded into the view at this checkpoint. */
  index: number
  /** Harness step, or null for the prologue / epilogue. */
  step: number | null
  /** Short label for the scrubber ("Start", "Step 4", "Verdict"). */
  label: string
  title: string
  text: string
  tone: StoryTone
}

export interface Story {
  checkpoints: Checkpoint[]
}

interface CallFacts {
  toolUseId: string
  tool: string
  path: string | null
  command: string | null
  mutating: boolean
}

function describeCall(c: CallFacts): string {
  switch (c.tool) {
    case 'write_file':
      return `wrote ${c.path ?? 'a file'}`
    case 'read_file':
      return `read ${c.path ?? 'a file'}`
    case 'list_dir':
      return `listed ${c.path && c.path !== '.' ? c.path : 'the workspace'}`
    case 'submit':
      return 'submitted its summary'
    case 'run_command':
      return c.command ? `ran \`${c.command.length > 72 ? `${c.command.slice(0, 69)}…` : c.command}\`` : 'ran a command'
    default:
      return `called ${c.tool}`
  }
}

function callFacts(d: ToolCallData): CallFacts {
  const cls = classifyCall(String(d.tool), d.input ?? {})
  return { toolUseId: d.tool_use_id, tool: String(d.tool), path: cls.path, command: cls.command, mutating: cls.mutating }
}

/** The exit code the tool reported, via the reducer's own result parser (structured JSON only). */
function exitCodeOf(r: ToolResultData): number | null {
  return parseToolResult(r.tool_use_id, r.output ?? '', r.is_error === true, r.duration_ms ?? 0).exitCode
}

function isTestCommand(c: CallFacts): boolean {
  return !!c.command && /\bpytest\b/.test(c.command)
}

function faultSentence(fault: FaultFired | null | undefined, ec: ErrorClass | null | undefined): { text: string; tone: StoryTone; unknown: boolean } | null {
  if (ec) {
    const label = ec.label
    const code = codeText(ec.code)
    if (ec.origin === 'real') {
      return {
        text: `This was a real failure, not a simulated one: ${label}.${ec.outcome_known === false ? ' Whether the operation ran is unknown.' : ''}`,
        tone: 'fail',
        unknown: ec.outcome_known === false,
      }
    }
    return {
      text: `The environment intercepted it: ${label}.${ec.outcome_known === false ? ' The agent cannot know whether the write landed.' : ''}${code ? ` (${code.plain})` : ''}`,
      tone: 'warn',
      unknown: ec.outcome_known === false,
    }
  }
  if (fault) {
    const label = taxonomyLabel(fault.origin ?? 'injected', fault.kind, fault.layer ?? null) ?? `simulated: ${fault.kind}`
    const unknown = fault.kind === 'ack_lost'
    return {
      text: `The environment intercepted it: ${label}.${unknown ? ' The agent cannot know whether the write landed.' : ''}`,
      tone: 'warn',
      unknown,
    }
  }
  return null
}

function evaluationSentences(ev: EvaluateResponse): string[] {
  const out: string[] = []
  const t = ev.tests
  out.push(`The grader uploaded hidden tests into the sandbox and ran them: ${t.passed} passed, ${t.failed} failed${t.errors ? `, ${t.errors} errors` : ''}.`)
  const ok = ev.checks.filter((c) => c.ok)
  const bad = ev.checks.filter((c) => !c.ok)
  if (ev.checks.length) {
    out.push(`Recovery checks from the ledger: ${ok.length}/${ev.checks.length} passed${bad.length ? ` — failed: ${bad.map((c) => checkText(c.id).whenFailed).join('; ')}` : ''}.`)
  }
  out.push(`Score ${Math.round(ev.score)}/100.`)
  return out
}

export function buildStory(events: Event[], scenario: Scenario | null): Story {
  const checkpoints: Checkpoint[] = []
  if (events.length === 0) return { checkpoints }

  // ---------------------------------------------------------------- prologue
  let taskPrompt: string | null = null
  let model: string | null = null
  let firstStepIdx = events.findIndex((e) => typeof e.step === 'number' && e.step > 0 && e.type !== 'log')
  if (firstStepIdx < 0) firstStepIdx = events.length
  for (const e of events.slice(0, firstStepIdx)) {
    if (e.type === 'episode.reset' && typeof e.data.task_prompt === 'string') taskPrompt = e.data.task_prompt
    if (e.type === 'run.started' && typeof e.data.model === 'string') model = e.data.model
  }
  const injected = (scenario?.fault_kinds ?? []).map((k) => taxonomyLabel('injected', k, 'boundary') ?? `simulated: ${k}`)
  const real = scenario?.harness_faults ?? []
  const pro: string[] = []
  pro.push(taskPrompt ? `The task: ${firstSentence(taskPrompt, 220)}` : 'The agent is given a task in a sandboxed copy of a small Python repo.')
  if (model) pro.push(`The model is ${model}.`)
  if (injected.length) pro.push(`Somewhere along the way the environment will inject ${injected.length === 1 ? 'one failure' : `${injected.length} failures`}: ${injected.join('; ')}.`)
  if (real.length) pro.push(`It also includes a real harness interruption (${real.map((f) => f.kind.replace('_', ' ')).join(', ')}) — the worker really is killed.`)
  pro.push('The agent is not told any of this.')
  checkpoints.push({
    index: Math.max(firstStepIdx, 0),
    step: null,
    label: 'Start',
    title: scenario?.title ?? 'The task',
    text: pro.join(' '),
    tone: 'info',
  })

  // ---------------------------------------------------------------- steps
  const stepOrder: number[] = []
  const byStep = new Map<number, Event[]>()
  for (const e of events) {
    if (typeof e.step !== 'number' || e.step <= 0 || e.type === 'log') continue
    if (!byStep.has(e.step)) {
      byStep.set(e.step, [])
      stepOrder.push(e.step)
    }
    byStep.get(e.step)!.push(e)
  }
  const lastIndexOfStep = new Map<number, number>()
  events.forEach((e, i) => {
    if (typeof e.step === 'number' && e.step > 0) lastIndexOfStep.set(e.step, i + 1)
  })

  const calls = new Map<string, CallFacts>()
  /** paths whose last write ended with an unknown outcome and have not been read back since */
  const unverified = new Map<string, number>()
  const lastEvaluated = events.filter((e) => e.type === 'episode.evaluated').at(-1)
  const evaluation = lastEvaluated ? (lastEvaluated.data as unknown as EvaluateResponse) : null

  for (const step of stepOrder) {
    const evs = byStep.get(step)!
    const sentences: string[] = []
    let tone: StoryTone = 'info'
    const prose = evs.find((e) => e.type === 'turn.text' && typeof e.data.text === 'string')
    if (prose) {
      const quote = firstSentence(String(prose.data.text), 160)
      if (quote) sentences.push(`The agent: “${quote}”`)
    }
    const stepCalls = evs.filter((e) => e.type === 'tool.call').map((e) => callFacts(e.data as unknown as ToolCallData))
    for (const c of stepCalls) calls.set(c.toolUseId, c)
    const results = evs.filter((e) => e.type === 'tool.result').map((e) => e.data as unknown as ToolResultData)
    const faultsFired = evs.filter((e) => e.type === 'fault.fired').map((e) => e.data as unknown as FaultFired)

    const actions: string[] = []
    for (const c of stepCalls) {
      const r = results.find((x) => x.tool_use_id === c.toolUseId)
      let s = `It ${describeCall(c)}`
      if (r) {
        const ec = r.error_class ?? null
        const fault =
          r.fault ?? faultsFired.find((f) => f.step === step && (!c.path || normalisePath(f.path) === c.path)) ?? null
        const exit = exitCodeOf(r)
        if (r.is_error) {
          const fs = faultSentence(fault, ec)
          if (fs) {
            s += `. ${fs.text}`
            tone = fs.tone === 'fail' ? 'fail' : tone === 'fail' ? 'fail' : 'warn'
            // The path whose write is now in doubt: the call's own, else the one the fault names.
            const doubted = c.path ?? (fault?.path ? normalisePath(fault.path) : null)
            if (fs.unknown && doubted) unverified.set(doubted, step)
          } else {
            s += '. It failed — the record carries no classification, so what happened is unknown.'
            tone = tone === 'fail' ? 'fail' : 'warn'
          }
        } else if (exit !== null && exit !== 0) {
          // A test command's exit status is a fact; whether "the tests failed" is the grader's call.
          s += ` — ${isTestCommand(c) ? 'the test command' : 'it'} exited ${exit}.`
          tone = tone === 'info' ? 'warn' : tone
        } else if (exit === 0 && isTestCommand(c)) {
          s += ' — the test command exited 0.'
          tone = tone === 'info' ? 'ok' : tone
        } else {
          s += '.'
        }
        // recovery observations (derived from call order, not from text)
        if (!r.is_error) {
          const doubted = [...unverified.keys()].find((p) => callTouches(c, p))
          if (doubted) {
            if (!c.mutating) {
              s += ` That read-back is the right move after a lost acknowledgement: now it knows the file's real contents.`
              unverified.delete(doubted)
              tone = tone === 'fail' ? 'fail' : 'ok'
            } else {
              s += ` It wrote ${doubted} again without reading it back — if the first write landed, this duplicates it.`
              tone = 'warn'
            }
          }
        }
      } else {
        s += ' (no result recorded).'
      }
      actions.push(s)
    }
    sentences.push(...actions)
    if (stepCalls.length === 0 && !prose) sentences.push('Nothing observable happened in this step.')

    checkpoints.push({
      index: lastIndexOfStep.get(step) ?? events.length,
      step,
      label: `Step ${step}`,
      title: `Step ${step}`,
      text: sentences.join(' '),
      tone,
    })
  }

  // ---------------------------------------------------------------- epilogue
  const finished = events.filter((e) => e.type === 'run.finished').at(-1)
  const status = finished ? String(finished.data.status ?? '') : ''
  const epi: string[] = []
  let epiTone: StoryTone = 'info'
  if (evaluation) {
    epi.push(...evaluationSentences(evaluation))
    epiTone = evaluation.passed && evaluation.checks.every((c) => c.ok) ? 'ok' : 'warn'
  }
  if (status && status !== 'ok') {
    const ec = (finished?.data.error_class as ErrorClass | undefined) ?? null
    epi.push(`The run ended ${status}${ec ? `: ${ec.label}` : typeof finished?.data.error === 'string' ? `: ${firstSentence(String(finished.data.error), 140)}` : ''}.`)
    epiTone = status === 'truncated' ? 'warn' : 'fail'
  }
  if (!evaluation && !status) epi.push('The run has not been graded.')
  checkpoints.push({
    index: events.length,
    step: null,
    label: 'Verdict',
    title: evaluation ? `Verdict: ${Math.round(evaluation.score)}/100` : 'Verdict',
    text: epi.join(' '),
    tone: epiTone,
  })

  return { checkpoints }
}

/** The checkpoint the cursor is currently inside (the last one whose index <= cursor). */
export function checkpointAt(story: Story, cursor: number): number {
  let idx = 0
  story.checkpoints.forEach((c, i) => {
    if (c.index <= cursor) idx = i
  })
  return idx
}

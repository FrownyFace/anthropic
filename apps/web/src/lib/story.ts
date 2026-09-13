/**
 * Plain-English tour of a run, one checkpoint per step, built ONLY from structured event fields
 * (tool names and inputs, is_error, fault / error_class / outcome, exit codes, interruptions,
 * resumes, sandbox notices, the grader's checks). Message text is never parsed; the agent's own
 * prose is only quoted. Calls are classified by the reducer's own `classifyCall` / `callTouches`
 * and their status by `lib/callStatus.ts`, so "mutating", "touches this path" and "unknown" mean
 * the same thing here as on the transcript.
 *
 * Each checkpoint carries a `kind` (what sort of moment it is) next to its `tone` (colour). The
 * StoryBar's word comes from the kind, never from the colour: "recovered" only for a read-back
 * after an unknown outcome and for a passing verdict; "fault" for an injected / staged fault;
 * "real failure" for a real one; nothing for a test run or an ordinary step.
 */

import { callStatus, effectiveOutcome } from './callStatus'
import { checkText } from './checks'
import { codeText, taxonomyLabel } from './codes'
import { firstSentence } from './format'
import { callNoun, interruptionSentence, resumeFor } from './interruptions'
import {
  asErrorClass,
  asInterruption,
  asToolOutcome,
  callTouches,
  classifyCall,
  normalisePath,
  parseToolResult,
  type ToolResultView,
} from './reducer'
import type {
  ErrorClass,
  EvaluateResponse,
  Event,
  FaultFired,
  HarnessFaultKind,
  Interruption,
  RunResumedData,
  Scenario,
  ToolCallData,
  ToolOutcome,
  ToolResultData,
} from './types'

export type StoryTone = 'info' | 'ok' | 'warn' | 'fail'

/** What kind of moment a checkpoint is, from structured facts only. The StoryBar's word comes from this. */
export type CheckpointKind = 'info' | 'fault' | 'real-failure' | 'recovery' | 'tests' | 'verdict'

export interface Checkpoint {
  /** How many events are folded into the view at this checkpoint. */
  index: number
  /** Harness step, or null for the prologue / epilogue. */
  step: number | null
  /** Short label for the scrubber ("Start", "Step 4", "Verdict"). */
  label: string
  title: string
  text: string
  /** Colour. */
  tone: StoryTone
  /** Word. */
  kind: CheckpointKind
}

export interface Story {
  checkpoints: Checkpoint[]
}

/** The word the StoryBar shows next to the title; '' for nothing. */
export function checkpointWord(kind: CheckpointKind, tone: StoryTone): string {
  switch (kind) {
    case 'recovery':
      return 'recovered'
    case 'fault':
      return 'fault'
    case 'real-failure':
      return 'real failure'
    case 'verdict':
      return tone === 'ok' ? 'recovered' : ''
    default:
      return ''
  }
}

// --------------------------------------------------------------------------- precedence

const KIND_RANK: Record<CheckpointKind, number> = { info: 0, tests: 1, recovery: 2, fault: 3, 'real-failure': 4, verdict: 5 }
const TONE_RANK: Record<StoryTone, number> = { info: 0, ok: 1, warn: 2, fail: 3 }

function stronger(a: CheckpointKind, b: CheckpointKind): CheckpointKind {
  return KIND_RANK[b] > KIND_RANK[a] ? b : a
}

function darker(a: StoryTone, b: StoryTone): StoryTone {
  return TONE_RANK[b] > TONE_RANK[a] ? b : a
}

// --------------------------------------------------------------------------- facts

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

/** The reducer's own result view (structured JSON, error_class, outcome) so status means the same thing as on the transcript. */
function resultView(r: ToolResultData): ToolResultView {
  const v = parseToolResult(r.tool_use_id, r.output ?? '', r.is_error === true, r.duration_ms ?? 0)
  v.errorClass = r.is_error ? asErrorClass(r.error_class) : null
  v.outcome = asToolOutcome(r.outcome)
  return v
}

function isTestCommand(c: CallFacts): boolean {
  return !!c.command && /\bpytest\b/.test(c.command)
}

function asResume(data: Record<string, unknown>, fallbackAt: string): RunResumedData | null {
  const gen = typeof data.worker_generation === 'number' ? data.worker_generation : null
  if (gen === null) return null
  return {
    worker_generation: gen,
    resumed_from_event_id: typeof data.resumed_from_event_id === 'number' ? data.resumed_from_event_id : -1,
    dangling_tool_use_id: typeof data.dangling_tool_use_id === 'string' ? data.dangling_tool_use_id : null,
    resumed_at: typeof data.resumed_at === 'string' ? data.resumed_at : fallbackAt,
  }
}

interface FaultNote {
  text: string
  tone: StoryTone
  kind: CheckpointKind
}

function faultSentence(fault: FaultFired | null | undefined, ec: ErrorClass | null | undefined, outcome: ToolOutcome | null): FaultNote | null {
  const unknown = outcome === 'unknown'
  if (ec) {
    if (ec.origin === 'real') {
      return {
        text: `This was a real failure, not a simulated one: ${ec.label}.${unknown ? ' Whether the operation ran is unknown.' : ''}`,
        tone: 'fail',
        kind: 'real-failure',
      }
    }
    const code = codeText(ec.code)
    const lead = ec.origin === 'staged' ? 'The scenario staged it at reset' : 'The environment intercepted it'
    return {
      text: `${lead}: ${ec.label}.${unknown ? ' The agent cannot know whether the write landed.' : ''}${code ? ` (${code.plain})` : ''}`,
      tone: 'warn',
      kind: 'fault',
    }
  }
  if (fault) {
    const origin = fault.origin ?? 'injected'
    const label = taxonomyLabel(origin, fault.kind, fault.layer ?? null) ?? `simulated: ${fault.kind}`
    const lead = origin === 'staged' ? 'The scenario staged it at reset' : 'The environment intercepted it'
    return {
      text: `${lead}: ${label}.${unknown ? ' The agent cannot know whether the write landed.' : ''}`,
      tone: 'warn',
      kind: 'fault',
    }
  }
  return null
}

/** Why a path's last write is in doubt, for the read-back sentence. */
function doubtWord(fault: FaultFired | null, ec: ErrorClass | null, it: Interruption | null): string {
  if (fault?.kind === 'ack_lost' || ec?.kind === 'ack_lost') return 'a lost acknowledgement'
  const layer = it?.layer ?? ec?.layer ?? null
  if (layer === 'harness') return 'an interrupted write'
  if (layer === 'transport') return 'a transport failure'
  return 'an unknown outcome'
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

const REAL_FAULT_TEXT: Record<HarnessFaultKind, string> = {
  worker_crash: 'worker crash — the worker really is killed after dispatching a call, and a fresh worker has to pick the run up',
  transport_abort: 'transport abort — the request is really cancelled mid-flight; the server still completes it',
}

// --------------------------------------------------------------------------- the story

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
  // The catalogue's per-origin list is the truth (a staged deletion and a simulated refusal are
  // different failures); `fault_kinds` is the fallback for an older harness.
  const pub = scenario?.faults_public ?? []
  const planted = pub.length
    ? pub.filter((f) => f.origin !== 'real').map((f) => taxonomyLabel(f.origin, f.kind, f.layer) ?? `${f.origin}: ${f.kind}`)
    : (scenario?.fault_kinds ?? []).map((k) => taxonomyLabel('injected', k, 'boundary') ?? `simulated: ${k}`)
  const realKinds: HarnessFaultKind[] = (scenario?.harness_faults ?? []).length
    ? (scenario?.harness_faults ?? []).map((f) => f.kind)
    : pub.filter((f) => f.origin === 'real').map((f) => f.kind as HarnessFaultKind)
  const pro: string[] = []
  pro.push(taskPrompt ? `The task: ${firstSentence(taskPrompt, 220)}` : 'The agent is given a task in a sandboxed copy of a small Python repo.')
  if (model) pro.push(`The model is ${model}.`)
  if (planted.length) pro.push(`Somewhere along the way the environment will fail the agent on purpose (${planted.length === 1 ? 'one failure' : `${planted.length} failures`}): ${planted.join('; ')}.`)
  if (realKinds.length) {
    pro.push(`It also includes a real harness interruption: ${realKinds.map((k) => REAL_FAULT_TEXT[k] ?? String(k).replace('_', ' ')).join('; ')}.`)
  }
  pro.push('The agent is not told any of this.')
  checkpoints.push({
    index: Math.max(firstStepIdx, 0),
    step: null,
    label: 'Start',
    title: scenario?.title ?? 'The task',
    text: pro.join(' '),
    tone: 'info',
    kind: 'info',
  })

  // ---------------------------------------------------------------- indexes
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

  // Which step a call belongs to, so interruptions / resumes that only name a tool_use_id land
  // on the right checkpoint.
  const callStep = new Map<string, number>()
  for (const e of events) {
    if (e.type === 'tool.call' && typeof e.data.tool_use_id === 'string' && typeof e.step === 'number') {
      callStep.set(e.data.tool_use_id, e.step)
    }
  }
  const stepOf = (e: Event, toolUseId: string | null | undefined): number | null => {
    if (typeof e.data.step === 'number') return e.data.step
    if (toolUseId && callStep.has(toolUseId)) return callStep.get(toolUseId)!
    return typeof e.step === 'number' ? e.step : null
  }

  const interruptions: Interruption[] = []
  const interruptionsByStep = new Map<number, Interruption[]>()
  const resumes: RunResumedData[] = []
  const resumesByStep = new Map<number, RunResumedData[]>()
  const sandboxByStep = new Map<number, { status: string; reason: string | null }[]>()
  for (const e of events) {
    if (e.type === 'interruption') {
      const it = asInterruption(e.data, typeof e.step === 'number' ? e.step : null)
      if (!it) continue
      interruptions.push(it)
      const step = typeof it.step === 'number' ? it.step : stepOf(e, it.tool_use_id)
      if (step !== null) {
        if (!interruptionsByStep.has(step)) interruptionsByStep.set(step, [])
        interruptionsByStep.get(step)!.push(it)
      }
    } else if (e.type === 'run.resumed') {
      const r = asResume(e.data, e.ts)
      if (!r) continue
      resumes.push(r)
      const step = stepOf(e, r.dangling_tool_use_id)
      if (step !== null) {
        if (!resumesByStep.has(step)) resumesByStep.set(step, [])
        resumesByStep.get(step)!.push(r)
      }
    } else if (e.type === 'episode.sandbox') {
      const status = String(e.data.status ?? '')
      if (status !== 'terminated' && status !== 'replaced') continue
      const step = stepOf(e, null)
      if (step !== null) {
        if (!sandboxByStep.has(step)) sandboxByStep.set(step, [])
        sandboxByStep.get(step)!.push({ status, reason: typeof e.data.reason === 'string' ? e.data.reason : null })
      }
    }
  }
  const narratedResumes = new Set<RunResumedData>()

  const calls = new Map<string, CallFacts>()
  /** paths whose last write ended with an unknown outcome and have not been read back since */
  const unverified = new Map<string, { step: number; why: string }>()
  const lastEvaluated = events.filter((e) => e.type === 'episode.evaluated').at(-1)
  const evaluation = lastEvaluated ? (lastEvaluated.data as unknown as EvaluateResponse) : null

  // ---------------------------------------------------------------- steps
  for (const step of stepOrder) {
    const evs = byStep.get(step)!
    const sentences: string[] = []
    let tone: StoryTone = 'info'
    let kind: CheckpointKind = 'info'
    const prose = evs.find((e) => e.type === 'turn.text' && typeof e.data.text === 'string')
    if (prose) {
      const quote = firstSentence(String(prose.data.text), 160)
      if (quote) sentences.push(`The agent: “${quote}”`)
    }
    const stepCalls = evs.filter((e) => e.type === 'tool.call').map((e) => callFacts(e.data as unknown as ToolCallData))
    for (const c of stepCalls) calls.set(c.toolUseId, c)
    const results = evs.filter((e) => e.type === 'tool.result').map((e) => e.data as unknown as ToolResultData)
    const faultsFired = evs.filter((e) => e.type === 'fault.fired').map((e) => e.data as unknown as FaultFired)
    const stepInterruptions = interruptionsByStep.get(step) ?? []
    const narratedInterruptions = new Set<Interruption>()

    const actions: string[] = []
    for (const c of stepCalls) {
      const r = results.find((x) => x.tool_use_id === c.toolUseId)
      let s = `It ${describeCall(c)}`
      const it = stepInterruptions.find((x) => x.tool_use_id === c.toolUseId) ?? null
      if (r) {
        const view = resultView(r)
        const ec = view.errorClass
        // `evs` already holds this step's events; `f.step` is the ledger index, never compared.
        const fault = r.fault ?? faultsFired.find((f) => !c.path || normalisePath(f.path) === c.path) ?? null
        const outcome = effectiveOutcome(view, fault)
        const status = callStatus({ result: view, fault })
        const exit = view.exitCode
        if (it) {
          // The interruption sentence tells the whole story of this call; no second failure sentence.
          const resume = resumeFor(it, resumes)
          if (resume) narratedResumes.add(resume)
          narratedInterruptions.add(it)
          s += `. ${interruptionSentence(it, resume, outcome)}`
          tone = darker(tone, 'fail')
          kind = stronger(kind, 'real-failure')
          const doubted = c.path ?? it.path ?? null
          if (outcome === 'unknown' && doubted && c.mutating) unverified.set(normalisePath(doubted), { step, why: doubtWord(fault, ec, it) })
        } else if (r.is_error) {
          const fs = faultSentence(fault, ec, outcome)
          if (fs) {
            s += `. ${fs.text}`
            tone = darker(tone, fs.tone)
            kind = stronger(kind, fs.kind)
            // The path whose write is now in doubt: the call's own, else the one the fault names.
            const doubted = c.path ?? (fault?.path ? normalisePath(fault.path) : null)
            if (outcome === 'unknown' && doubted) unverified.set(doubted, { step, why: doubtWord(fault, ec, null) })
          } else {
            s += '. It failed — the record carries no classification, so what happened is unknown.'
            tone = darker(tone, 'warn')
          }
        } else if (exit !== null && exit !== 0) {
          // A test command's exit status is a fact; whether "the tests failed" is the grader's call.
          s += ` — ${isTestCommand(c) ? 'the test command' : 'it'} exited ${exit}.`
          tone = darker(tone, 'warn')
          if (isTestCommand(c)) kind = stronger(kind, 'tests')
        } else if (exit === 0 && isTestCommand(c)) {
          s += ' — the test command exited 0.'
          tone = darker(tone, 'ok')
          kind = stronger(kind, 'tests')
        } else {
          s += '.'
        }
        // recovery observations (derived from call order and status, not from text)
        if (status.kind === 'ok') {
          const doubted = [...unverified.keys()].find((p) => callTouches(c, p))
          if (doubted) {
            if (!c.mutating) {
              const why = unverified.get(doubted)!.why
              s += ` That read-back is the right move after ${why}: now it knows the file's real contents.`
              unverified.delete(doubted)
              tone = darker(tone, 'ok')
              kind = stronger(kind, 'recovery')
            } else {
              s += ` It wrote ${doubted} again without reading it back — if the first write landed, this duplicates it.`
              tone = darker(tone, 'warn')
            }
          }
        }
      } else if (it) {
        const resume = resumeFor(it, resumes)
        if (resume) narratedResumes.add(resume)
        narratedInterruptions.add(it)
        s += `. ${interruptionSentence(it, resume, null)}`
        tone = darker(tone, 'fail')
        kind = stronger(kind, 'real-failure')
        const doubted = c.path ?? it.path ?? null
        if (!it.outcome_known && doubted && c.mutating) unverified.set(normalisePath(doubted), { step, why: doubtWord(null, null, it) })
      } else {
        s += ' (no result recorded).'
      }
      actions.push(s)
    }
    sentences.push(...actions)

    // Interruptions, resumes and sandbox notices that no call in this step accounted for.
    for (const it of stepInterruptions) {
      if (narratedInterruptions.has(it)) continue
      const resume = resumeFor(it, resumes)
      if (resume) narratedResumes.add(resume)
      sentences.push(interruptionSentence(it, resume, null))
      tone = darker(tone, 'fail')
      kind = stronger(kind, 'real-failure')
    }
    for (const r of resumesByStep.get(step) ?? []) {
      if (narratedResumes.has(r)) continue
      narratedResumes.add(r)
      sentences.push(`Worker ${r.worker_generation} picked the run up from event ${r.resumed_from_event_id} and continued — a real interruption had ended the previous worker.`)
      tone = darker(tone, 'warn')
      kind = stronger(kind, 'real-failure')
    }
    if (!stepInterruptions.some((it) => it.layer === 'sandbox')) {
      for (const n of sandboxByStep.get(step) ?? []) {
        sentences.push(`The environment reported the sandbox ${n.status}${n.reason ? ` — ${n.reason}` : ''}. A real failure, not a simulated one.`)
        tone = darker(tone, 'fail')
        kind = stronger(kind, 'real-failure')
      }
    }

    if (sentences.length === 0) sentences.push('Nothing observable happened in this step.')

    checkpoints.push({
      index: lastIndexOfStep.get(step) ?? events.length,
      step,
      label: `Step ${step}`,
      title: `Step ${step}`,
      text: sentences.join(' '),
      tone,
      kind,
    })
  }

  // ---------------------------------------------------------------- epilogue
  const finished = events.filter((e) => e.type === 'run.finished').at(-1)
  const status = finished ? String(finished.data.status ?? '') : ''
  const epi: string[] = []
  let epiTone: StoryTone = 'info'
  const workerGeneration = Math.max(
    typeof finished?.data.worker_generation === 'number' ? finished.data.worker_generation : 1,
    ...resumes.map((r) => r.worker_generation),
  )
  if (workerGeneration > 1) {
    const n = interruptions.length || (typeof finished?.data.interruptions === 'number' ? finished.data.interruptions : workerGeneration - 1)
    epi.push(`It took ${workerGeneration} harness workers to finish this run: ${n} real interruption${n === 1 ? '' : 's'} along the way.`)
  }
  if (evaluation) {
    epi.push(...evaluationSentences(evaluation))
    epiTone = evaluation.passed && evaluation.checks.every((c) => c.ok) ? 'ok' : 'warn'
  }
  if (status && status !== 'ok') {
    const ec = asErrorClass(finished?.data.error_class)
    epi.push(`The run ended ${status}${ec ? `: ${ec.label}` : typeof finished?.data.error === 'string' ? `: ${firstSentence(String(finished.data.error), 140)}` : ''}.`)
    epiTone = status === 'truncated' ? 'warn' : 'fail'
  }
  if (!evaluation && finished) {
    const es = String(finished.data.evaluation_status ?? '')
    const err = typeof finished.data.evaluation_error === 'string' ? finished.data.evaluation_error : null
    if (es === 'skipped' || es === 'failed') {
      epi.push(`Grading was ${es === 'skipped' ? 'skipped' : 'attempted but failed'}${err ? ` (${firstSentence(err, 140)})` : ''}, so there is no score.`)
    }
  }
  // `ledger.resolution` (after grading): the ground truth for the calls whose outcome the agent never learned.
  for (const e of events) {
    if (e.type !== 'log' || e.data.ev !== 'ledger.resolution' || !Array.isArray(e.data.resolutions)) continue
    for (const raw of e.data.resolutions) {
      const r = raw as Record<string, unknown>
      if (typeof r.side_effect_applied !== 'boolean') continue
      const what = `${callNoun(typeof r.tool === 'string' ? r.tool : null)}${typeof r.path === 'string' ? ` to ${r.path}` : ''}`
      epi.push(`The ledger later confirmed that the ${what} whose outcome the agent never learned had ${r.side_effect_applied ? '' : 'not '}landed.`)
    }
  }
  if (!evaluation && !status) epi.push('The run has not been graded.')
  checkpoints.push({
    index: events.length,
    step: null,
    label: 'Verdict',
    title: evaluation ? `Verdict: ${Math.round(evaluation.score)}/100` : 'Verdict',
    text: epi.join(' '),
    tone: epiTone,
    kind: 'verdict',
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

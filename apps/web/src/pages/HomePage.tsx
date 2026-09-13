import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle } from 'lucide-react'

import { Composer } from '@/components/bui'
import { TopBar } from '@/components/layout/TopBar'
import { ScenarioTable } from '@/components/ScenarioTable'
import type { ConversationsState } from '@/hooks/useConversations'
import type { HarnessState } from '@/hooks/useHarness'
import { useStartRun } from '@/hooks/useStartRun'
import { navigate } from '@/lib/router'
import { allowedModel, DEFAULT_MODEL, MODEL_ALLOWLIST, type Scenario } from '@/lib/types'

/** The failures the scenarios inject, in the words a reader meets them (see services/sandbox-env/FAULTS.md). */
const FAILURES = [
  'an expected file does not exist',
  'write permission is refused',
  'the reply to a write times out, even though the write landed',
  'the harness process is killed mid-write (real, not simulated)',
]

/** Mirrors README.md "Try it in 60 seconds". */
const STEPS: [string, string][] = [
  ['Pick a scenario.', 'Press Run on a row below, or type / in the box to choose one and edit its task first.'],
  ['Watch the run.', 'Commands, outputs, the failure when it hits, and the file changes stream in. A live run takes one to two minutes.'],
  ['Read the score.', 'Hidden tests are worth 60 points and recovery checks 40, so finishing the task is not enough.'],
  ['No API quota?', 'Press Replay on a row instead: a recorded run plays in the browser and needs no server.'],
]

export function HomePage({
  harness,
  conversations,
}: {
  harness: HarnessState
  conversations: ConversationsState
}) {
  const [scenarioId, setScenarioId] = useState<string | null>(null)
  // The user's pick wins; before one, the harness's default (once /health answers) — but only when
  // it is on the allowlist — else ours.
  const [modelChoice, setModel] = useState<string | null>(null)
  const model = allowedModel(modelChoice) ?? allowedModel(harness.health?.model_default) ?? DEFAULT_MODEL
  const [seed, setSeed] = useState<number | null>(null)
  const [prompt, setPrompt] = useState('')
  const { start, busy, error } = useStartRun(harness, conversations)

  useEffect(() => {
    document.title = 'Faultline — new run'
  }, [])

  const scenario = useMemo(
    () => harness.scenarios.find((s) => s.id === scenarioId) ?? null,
    [harness.scenarios, scenarioId],
  )

  const pick = (id: string) => {
    setScenarioId(id)
    const s = harness.scenarios.find((x) => x.id === id)
    if (s) setPrompt(s.task_prompt)
  }

  const submit = () => {
    if (!scenario || busy) return
    void start({
      scenarioId: scenario.id,
      model,
      seed,
      prompt: prompt.trim() === scenario.task_prompt.trim() ? null : prompt,
      title: scenario.title,
    })
  }

  const runScenario = (s: Scenario) => {
    setScenarioId(s.id)
    setPrompt(s.task_prompt)
    void start({ scenarioId: s.id, model, seed, prompt: null, title: s.title })
  }

  const canRun = harness.reachable && !!harness.client
  const hint =
    harness.phase === 'resolving'
      ? 'connecting…'
      : harness.phase === 'checking'
        ? 'waking the harness… (first load takes about 10 s)'
        : harness.phase === 'unreachable'
          ? 'live runs are offline · Replay still works'
          : scenario
            ? `${scenario.title} · up to ${scenario.max_steps} steps · Enter to run`
            : 'type / to pick a scenario, or press Run on a row below'

  return (
    <div className="flex h-dvh min-h-0 flex-col">
      <TopBar crumbs={[{ label: 'New run' }]} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-6xl flex-col items-center px-4 pt-14 pb-16 md:px-8">
          <p className="mb-3 text-center text-[12px] text-muted-foreground" data-slot="kicker">
            Anthropic Platform SWE take-home · Theme 3: Systems &amp; Reliability, with a Theme 4 Evaluation twist
          </p>
          <h1 className="text-center text-2xl font-medium tracking-tight">
            An agent harness where the environment fights back.
          </h1>
          <p className="mt-3 max-w-2xl text-center text-[14px] leading-relaxed text-muted-foreground">
            A Claude agent edits a small Python repo with real shell commands inside an isolated sandbox.
            Faultline breaks things on purpose and shows whether the agent recovers.
          </p>

          <div className="mt-8 grid w-full max-w-3xl gap-8 md:grid-cols-2">
            <section aria-labelledby="what-goes-wrong">
              <h2 id="what-goes-wrong" className="text-sm font-medium">
                What goes wrong
              </h2>
              <ul aria-label="What goes wrong" className="mt-2 space-y-1.5 text-[13.5px] leading-relaxed text-muted-foreground">
                {FAILURES.map((line) => (
                  <li key={line} className="flex gap-2.5">
                    <span aria-hidden className="mt-[9px] size-1 shrink-0 rounded-full bg-muted-foreground/60" />
                    <span>{line}</span>
                  </li>
                ))}
              </ul>
              <p className="mt-3 text-[13px] leading-relaxed text-muted-foreground">
                Retry a timed-out write without checking and you can write the change twice.
              </p>
            </section>

            <section aria-labelledby="try-it">
              <h2 id="try-it" className="text-sm font-medium">
                Try it in 60 seconds
              </h2>
              <ol aria-label="Try it in 60 seconds" className="mt-2 space-y-1.5 text-[13.5px] leading-relaxed text-muted-foreground">
                {STEPS.map(([lead, rest], i) => (
                  <li key={lead} className="flex gap-2.5">
                    <span className="w-4 shrink-0 font-mono text-[12px] text-muted-foreground/80 tabular-nums">{i + 1}.</span>
                    <span>
                      <span className="font-medium text-foreground">{lead}</span> {rest}
                    </span>
                  </li>
                ))}
              </ol>
            </section>
          </div>

          <div className="mt-8 w-full max-w-4xl">
            <Composer
              scenarios={harness.scenarios}
              scenarioId={scenarioId}
              onScenarioChange={pick}
              models={MODEL_ALLOWLIST}
              model={model}
              onModelChange={setModel}
              seed={seed}
              onSeedChange={setSeed}
              prompt={prompt}
              onPromptChange={setPrompt}
              onSubmit={submit}
              disabled={!canRun || !scenario}
              busy={busy}
              placeholder="Type / to pick a scenario. Its task fills in here; edit it if you like, then press Enter."
              hint={hint}
            />
            {error ? (
              <div className="mt-3 flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-[13px] text-destructive">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
                <span className="break-all">Could not start the run: {error}</span>
              </div>
            ) : null}
          </div>

          <div className="mt-10 w-full">
            <ScenarioTable
              scenarios={harness.scenarios}
              loading={harness.loading && harness.scenarios.length === 0}
              error={harness.scenarios.length === 0 && !harness.loading ? harness.scenariosError : null}
              selectedId={scenarioId}
              starting={busy ? scenarioId : null}
              canRun={canRun}
              onSelect={pick}
              onRun={runScenario}
              onReplay={(demoId) => navigate({ kind: 'replay', demoId })}
            />
          </div>
        </div>
      </div>
    </div>
  )
}

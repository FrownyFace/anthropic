import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle } from 'lucide-react'

import { Composer } from '@/components/bui'
import { TopBar } from '@/components/layout/TopBar'
import { ScenarioTable } from '@/components/ScenarioTable'
import type { ConversationsState } from '@/hooks/useConversations'
import type { HarnessState } from '@/hooks/useHarness'
import { useStartRun } from '@/hooks/useStartRun'
import { navigate } from '@/lib/router'
import { DEFAULT_MODEL, MODEL_ALLOWLIST, type Scenario } from '@/lib/types'

const WHAT_IT_IS = [
  'A Claude agent gets real shell tools inside an isolated sandbox holding a small Python repo.',
  'The environment injects one failure at the tool boundary: a missing file, a denied write, or a write whose acknowledgement is lost.',
  'Every command, its output, the fault, and the workspace diff stream into this transcript as they happen.',
  'A grader outside the agent’s reach runs hidden tests and scores how the agent recovered, not just whether it finished.',
  'Runs are saved to this browser’s conversation history; replays need no backend at all.',
]

export function HomePage({
  harness,
  conversations,
}: {
  harness: HarnessState
  conversations: ConversationsState
}) {
  const [scenarioId, setScenarioId] = useState<string | null>(null)
  // The user's pick wins; before one, the harness's default (once /health answers), else ours.
  const [modelChoice, setModel] = useState<string | null>(null)
  const model = modelChoice ?? harness.health?.model_default ?? DEFAULT_MODEL
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
      ? 'resolving the harness URL…'
      : harness.phase === 'checking'
        ? 'checking the harness… (a cold start takes ~10 s)'
        : harness.phase === 'unreachable'
          ? 'harness unreachable — replays in the sidebar still work'
          : scenario
            ? `${scenario.title} · max ${scenario.max_steps} steps · enter to run`
            : 'type / to pick a scenario, or use Run in the table below'

  return (
    <div className="flex h-dvh min-h-0 flex-col">
      <TopBar crumbs={[{ label: 'New run' }]} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-6xl flex-col items-center px-4 pt-14 pb-16 md:px-8">
          <h1 className="text-center text-2xl font-medium tracking-tight">What should the agent survive today?</h1>
          <ul className="mt-5 w-full max-w-3xl space-y-1.5 text-[13.5px] leading-relaxed text-muted-foreground">
            {WHAT_IT_IS.map((line) => (
              <li key={line} className="flex gap-2.5">
                <span aria-hidden className="mt-[9px] size-1 shrink-0 rounded-full bg-muted-foreground/60" />
                <span>{line}</span>
              </li>
            ))}
          </ul>

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
              placeholder="Type / to choose a scenario, edit the task if you like, then press enter"
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

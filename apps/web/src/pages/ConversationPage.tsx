import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle } from 'lucide-react'

import { Composer } from '@/components/bui'
import { linkProps } from '@/components/layout/Link'
import { RunStatusStrip, type TranscriptSource } from '@/components/layout/RunStatusStrip'
import { TopBar } from '@/components/layout/TopBar'
import { TranscriptView } from '@/components/transcript/TranscriptView'
import { RunLayout } from '@/components/workspace/RunLayout'
import { useWorkspaceOpen } from '@/hooks/useWorkspaceLayout'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { NOT_FOUND, useConversation } from '@/hooks/useConversation'
import type { ConversationsState } from '@/hooks/useConversations'
import type { HarnessState } from '@/hooks/useHarness'
import { useRunView } from '@/hooks/useRunView'
import { useStartRun } from '@/hooks/useStartRun'
import { getLogger } from '@/lib/log'
import { isTerminal } from '@/lib/reducer'
import { gradeOf, statusTitle, statusTone } from '@/lib/runStatus'
import { overlayFromViewState, transcriptFromMessages, transcriptFromViewState } from '@/lib/transcript'
import { allowedModel, DEFAULT_MODEL, MODEL_ALLOWLIST, type RunSummary } from '@/lib/types'

const log = getLogger('web')

function RunSwitcher({
  conversationId,
  runs,
  selectedId,
}: {
  conversationId: string
  runs: RunSummary[]
  selectedId: string | null
}) {
  if (runs.length <= 1) return null
  return (
    <div className="flex flex-wrap items-center gap-1.5" aria-label="Runs in this conversation">
      <span className="text-[11px] text-muted-foreground">runs</span>
      {runs.map((r, i) => (
        <a
          key={r.id}
          {...linkProps({ kind: 'conversation', id: conversationId, runId: r.id })}
          aria-current={r.id === selectedId ? 'page' : undefined}
          className="rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          title={statusTitle(r.status, gradeOf(null, r.score))}
        >
          <Badge
            variant="outline"
            className={`font-mono ${statusTone(r.status, gradeOf(null, r.score))} ${r.id === selectedId ? 'ring-1 ring-ring/60' : 'opacity-80'}`}
          >
            #{i + 1} · {r.status}
            {r.score !== null && r.score !== undefined ? ` · ${Math.round(r.score)}` : ''}
          </Badge>
        </a>
      ))}
    </div>
  )
}

export function ConversationPage({
  harness,
  conversations,
  conversationId,
  runId,
}: {
  harness: HarnessState
  conversations: ConversationsState
  conversationId: string
  runId: string | null
}) {
  const { detail, loading, error, refresh } = useConversation(harness.client, conversationId)
  const [workspaceOpen, setWorkspaceOpen] = useWorkspaceOpen()
  // The user's pick wins; before one, the harness's default (once /health answers) — but only when
  // it is on the allowlist — else ours.
  const [modelChoice, setModel] = useState<string | null>(null)
  const model = allowedModel(modelChoice) ?? allowedModel(harness.health?.model_default) ?? DEFAULT_MODEL
  const [seed, setSeed] = useState<number | null>(null)
  const [prompt, setPrompt] = useState('')
  const { start, busy, error: startError } = useStartRun(harness, conversations)

  const runs = useMemo(
    () => [...(detail?.runs ?? [])].sort((a, b) => a.created_at.localeCompare(b.created_at)),
    [detail?.runs],
  )
  const selected: RunSummary | null = useMemo(() => {
    if (runs.length === 0) return null
    return (runId ? runs.find((r) => r.id === runId) : null) ?? runs[runs.length - 1] ?? null
  }, [runs, runId])

  const source = useMemo(
    () => (selected ? ({ kind: 'live', runId: selected.id } as const) : ({ kind: 'none' } as const)),
    [selected],
  )
  const rv = useRunView(harness.client, source)
  const live = !!selected && (rv.state.status === 'running' || rv.state.status === 'queued')

  // When the live run reaches a terminal state, reload the conversation from the store so the
  // transcript switches to the persisted (SQLite) projection and the rail updates its status dot.
  const refreshedFor = useRef<string | null>(null)
  useEffect(() => {
    if (!selected || !isTerminal(rv.state.status) || rv.state.runId !== selected.id) return
    if (refreshedFor.current === selected.id) return
    refreshedFor.current = selected.id
    if (!isTerminal(selected.status)) {
      log.info('conversation.refresh', 'run finished; reloading persisted transcript', {
        conversation_id: conversationId,
        run_id: selected.id,
        status: rv.state.status,
      })
      refresh()
      conversations.refresh()
    }
  }, [rv.state.status, rv.state.runId, selected, conversationId, refresh, conversations])

  const scenario = useMemo(
    () => harness.scenarios.find((s) => s.id === detail?.conversation.scenario_id) ?? null,
    [harness.scenarios, detail?.conversation.scenario_id],
  )
  useEffect(() => {
    if (scenario && prompt === '') setPrompt(scenario.task_prompt)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scenario?.id])

  useEffect(() => {
    document.title = detail ? `${detail.conversation.title} — Faultline` : 'Faultline'
  }, [detail])

  const messagesForRun = useMemo(
    () => (detail && selected ? detail.messages.filter((m) => m.run_id === selected.id) : []),
    [detail, selected],
  )
  const persisted = !!detail && !!selected && isTerminal(selected.status) && messagesForRun.length > 0 && !live
  // The messages projection has no evaluation, run error, error class, interruptions or resumes;
  // overlay them from the run record the view was seeded with (only when that record is this
  // run's — the view resets on switch).
  const seeded = !!selected && rv.state.runId === selected.id
  const transcript = useMemo(
    () =>
      persisted && detail && selected
        ? transcriptFromMessages(messagesForRun, selected, seeded ? overlayFromViewState(rv.state) : {})
        : transcriptFromViewState(rv.state, live),
    [persisted, detail, selected, messagesForRun, rv.state, live, seeded],
  )
  const transcriptSource: TranscriptSource | null = persisted
    ? 'sqlite'
    : live
      ? rv.transport?.mode === 'poll'
        ? 'poll'
        : 'sse'
      : selected
        ? 'record'
        : null

  const submit = () => {
    if (!detail || busy) return
    void start({
      scenarioId: detail.conversation.scenario_id,
      model,
      seed,
      prompt: scenario && prompt.trim() === scenario.task_prompt.trim() ? null : prompt,
      conversationId: detail.conversation.id,
    })
  }

  const title = detail?.conversation.title ?? (loading ? 'Loading…' : 'Conversation')

  return (
    <div className="flex h-dvh min-h-0 flex-col">
      <TopBar
        crumbs={[{ label: 'Conversations', route: { kind: 'home' } }, { label: title }]}
        onToggleWorkspace={() => setWorkspaceOpen((v) => !v)}
        workspaceOpen={workspaceOpen}
      >
        <RunStatusStrip
          state={selected ? rv.state : null}
          transport={rv.transport}
          source={transcriptSource}
        />
      </TopBar>

      <RunLayout state={selected ? rv.state : null} live={live} open={workspaceOpen} onOpenChange={setWorkspaceOpen}>
          {loading && !detail ? (
            <div className="mx-auto w-full max-w-3xl space-y-4 px-4 py-8">
              <Skeleton className="ml-auto h-16 w-2/3" />
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-32 w-full" />
            </div>
          ) : error && !detail ? (
            <div className="mx-auto w-full max-w-3xl px-4 py-8">
              <div
                className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-[13px] text-amber-800 dark:text-amber-200"
                role="status"
                data-slot={error === NOT_FOUND ? 'not-found' : 'load-error'}
              >
                <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
                <div>
                  <div className="font-medium">{error === NOT_FOUND ? 'Conversation not found' : 'Conversation not available'}</div>
                  <div className="mt-0.5 font-mono text-[11.5px] break-all opacity-80">
                    {error === NOT_FOUND
                      ? 'The harness has no conversation with this id for this browser identity — it may belong to another browser, or the id is wrong.'
                      : error}
                  </div>
                  <a className="mt-1 inline-block underline" {...linkProps({ kind: 'home' })}>
                    Start a new run
                  </a>
                </div>
              </div>
            </div>
          ) : (
            <TranscriptView
              transcript={transcript}
              diffs={rv.state.diffs}
              maxSteps={rv.state.maxSteps || scenario?.max_steps || 20}
              loading={!!selected && rv.loading}
              loadingLabel="Loading run…"
              error={rv.loadError}
              notFound={rv.notFound}
              transport={rv.transport}
              footer={
                <div className="space-y-3 pt-2">
                  {detail ? (
                    <RunSwitcher conversationId={detail.conversation.id} runs={runs} selectedId={selected?.id ?? null} />
                  ) : null}
                  {detail && runs.length === 0 ? (
                    <p className="text-[13px] text-muted-foreground">
                      No runs in this conversation yet — send the task below to start one.
                    </p>
                  ) : null}
                </div>
              }
            />
          )}

          {detail ? (
            <div className="shrink-0 border-t border-border bg-background/90 px-4 py-3 backdrop-blur md:px-6">
              <div className="mx-auto w-full max-w-3xl space-y-2">
                <Composer
                  scenarios={scenario ? [scenario] : harness.scenarios}
                  scenarioId={detail.conversation.scenario_id}
                  onScenarioChange={() => undefined}
                  models={MODEL_ALLOWLIST}
                  model={model}
                  onModelChange={setModel}
                  seed={seed}
                  onSeedChange={setSeed}
                  prompt={prompt}
                  onPromptChange={setPrompt}
                  onSubmit={submit}
                  disabled={!harness.reachable || live || busy}
                  busy={busy}
                  placeholder={live ? 'A run is in progress…' : 'Edit the task and press enter to run it again in this conversation'}
                  hint={
                    live
                      ? 'wait for the current run to finish'
                      : `${detail.conversation.scenario_id} · another run in this conversation`
                  }
                />
                {startError ? (
                  <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-[13px] text-destructive">
                    <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
                    <span className="break-all">Could not start the run: {startError}</span>
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}
      </RunLayout>
    </div>
  )
}

import { useEffect, useMemo } from 'react'

import { RunStatusStrip, type TranscriptSource } from '@/components/layout/RunStatusStrip'
import { TopBar } from '@/components/layout/TopBar'
import { TranscriptView } from '@/components/transcript/TranscriptView'
import { RunLayout } from '@/components/workspace/RunLayout'
import { useWorkspaceOpen } from '@/hooks/useWorkspaceLayout'
import type { HarnessState } from '@/hooks/useHarness'
import { useRunView } from '@/hooks/useRunView'
import { getLogger } from '@/lib/log'
import { navigate } from '@/lib/router'
import { transcriptFromViewState } from '@/lib/transcript'

const log = getLogger('web')

/**
 * A run addressed by id alone. Upgrades itself to /conversations/{id} as soon as the seed record
 * (the one useRunView fetched — no second GET) says which conversation it belongs to. A 404 is
 * its own state: the run does not exist, or belongs to another browser identity.
 */
export function RunPage({ harness, runId }: { harness: HarnessState; runId: string }) {
  const source = useMemo(() => ({ kind: 'live', runId }) as const, [runId])
  const rv = useRunView(harness.client, source)
  const [workspaceOpen, setWorkspaceOpen] = useWorkspaceOpen()
  const live = !rv.notFound && (rv.state.status === 'running' || rv.state.status === 'queued')

  const conversationId = rv.record?.conversation_id ?? null
  useEffect(() => {
    if (!conversationId) return
    log.info('run.upgrade', 'run belongs to a conversation; redirecting', {
      run_id: runId,
      conversation_id: conversationId,
    })
    navigate({ kind: 'conversation', id: conversationId, runId }, { replace: true })
  }, [conversationId, runId])

  useEffect(() => {
    document.title = `${rv.state.scenarioId ?? 'run'} · ${runId} — Faultline`
  }, [rv.state.scenarioId, runId])

  const transcript = useMemo(() => transcriptFromViewState(rv.state, live), [rv.state, live])
  const src: TranscriptSource = live ? (rv.transport?.mode === 'poll' ? 'poll' : 'sse') : 'record'

  return (
    <div className="flex h-dvh min-h-0 flex-col">
      <TopBar
        crumbs={[{ label: 'Runs', route: { kind: 'home' } }, { label: rv.state.scenarioId ? `${rv.state.scenarioId} · ${runId}` : runId }]}
        onToggleWorkspace={() => setWorkspaceOpen((v) => !v)}
        workspaceOpen={workspaceOpen}
      >
        {rv.notFound ? null : <RunStatusStrip state={rv.state} transport={rv.transport} source={src} />}
      </TopBar>
      <RunLayout state={rv.notFound ? null : rv.state} live={live} open={workspaceOpen} onOpenChange={setWorkspaceOpen}>
          <TranscriptView
            transcript={transcript}
            diffs={rv.state.diffs}
            maxSteps={rv.state.maxSteps}
            loading={rv.loading}
            loadingLabel="Loading run…"
            error={rv.loadError}
            notFound={rv.notFound}
            transport={rv.transport}
          />
      </RunLayout>
    </div>
  )
}

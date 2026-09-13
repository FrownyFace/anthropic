import { useEffect, useMemo } from 'react'

import { Badge } from '@/components/ui/badge'
import { TopBar } from '@/components/layout/TopBar'
import { ReplayScrubber } from '@/components/replay/ReplayScrubber'
import { StoryBar } from '@/components/replay/StoryBar'
import { TranscriptView } from '@/components/transcript/TranscriptView'
import { RunLayout } from '@/components/workspace/RunLayout'
import { useWorkspaceOpen } from '@/hooks/useWorkspaceLayout'
import type { HarnessState } from '@/hooks/useHarness'
import { useReplay } from '@/hooks/useReplay'
import { DEMOS } from '@/lib/replay'
import { buildStory, checkpointAt } from '@/lib/story'
import { transcriptFromViewState } from '@/lib/transcript'

/** Bundled recorded run: a scrubbable replay with a plain-English tour of each step. */
export function ReplayPage({ harness, demoId }: { harness: HarnessState; demoId: string }) {
  const rp = useReplay(demoId)
  const [workspaceOpen, setWorkspaceOpen] = useWorkspaceOpen()
  const meta = DEMOS.find((d) => d.id === demoId)

  useEffect(() => {
    document.title = `replay · ${demoId} — Faultline`
  }, [demoId])

  const scenario = useMemo(
    () => harness.scenarios.find((s) => s.id === (rp.record?.scenario_id ?? rp.state.scenarioId)) ?? null,
    [harness.scenarios, rp.record?.scenario_id, rp.state.scenarioId],
  )
  const story = useMemo(() => buildStory(rp.events, scenario), [rp.events, scenario])
  const at = checkpointAt(story, rp.cursor)
  const transcript = useMemo(() => transcriptFromViewState(rp.state, rp.live), [rp.state, rp.live])
  const current = story.checkpoints[at] ?? null
  const focusStep = rp.playing
    ? null
    : current === null
      ? null
      : current.step !== null
        ? current.step
        : at === 0
          ? 'start'
          : 'end'

  const goTo = (i: number) => {
    const cp = story.checkpoints[Math.max(0, Math.min(i, story.checkpoints.length - 1))]
    if (cp) rp.setCursor(cp.index)
  }

  return (
    <div className="flex h-dvh min-h-0 flex-col">
      <TopBar
        crumbs={[{ label: 'Replays', route: { kind: 'home' } }, { label: meta?.label ?? demoId }]}
        onToggleWorkspace={() => setWorkspaceOpen((v) => !v)}
        workspaceOpen={workspaceOpen}
      >
        <Badge variant="outline" className="font-mono text-muted-foreground" title="A recorded run playing back in the browser; no backend involved.">
          recorded replay
        </Badge>
        {story.checkpoints.length > 0 ? (
          <ReplayScrubber
            story={story}
            cursor={rp.cursor}
            onCursorChange={rp.setCursor}
            playing={rp.playing}
            onPlayingChange={rp.setPlaying}
            onRestart={rp.restart}
            done={rp.done}
          />
        ) : null}
      </TopBar>
      <StoryBar
        checkpoint={story.checkpoints[at] ?? null}
        position={at}
        total={story.checkpoints.length}
        onPrev={() => goTo(at - 1)}
        onNext={() => goTo(at + 1)}
      />
      <RunLayout state={rp.state} live={rp.live} open={workspaceOpen} onOpenChange={setWorkspaceOpen}>
        <TranscriptView
          transcript={transcript}
          diffs={rp.state.diffs}
          maxSteps={rp.state.maxSteps}
          loading={rp.loading}
          loadingLabel="Loading bundled run…"
          error={rp.error}
          focusStep={focusStep}
        />
      </RunLayout>
    </div>
  )
}

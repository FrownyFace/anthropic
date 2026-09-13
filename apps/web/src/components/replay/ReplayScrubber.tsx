import { Pause, Play, RotateCcw } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Slider } from '@/components/ui/slider'
import type { Story } from '@/lib/story'
import { checkpointAt } from '@/lib/story'

/** Top-bar transport: play/pause, restart, and a slider over the story's checkpoints. */
export function ReplayScrubber({
  story,
  cursor,
  onCursorChange,
  playing,
  onPlayingChange,
  onRestart,
  done,
}: {
  story: Story
  cursor: number
  onCursorChange: (n: number) => void
  playing: boolean
  onPlayingChange: (p: boolean) => void
  onRestart: () => void
  done: boolean
}) {
  const n = story.checkpoints.length
  const at = checkpointAt(story, cursor)
  const current = story.checkpoints[at]
  const steps = story.checkpoints.filter((c) => c.step !== null).length

  return (
    <div className="flex items-center gap-2" aria-label="Replay controls">
      <Button
        size="sm"
        variant="ghost"
        className="h-8 w-8 p-0"
        onClick={() => (done ? onRestart() : onPlayingChange(!playing))}
        aria-label={done ? 'Restart replay' : playing ? 'Pause replay' : 'Play replay'}
        title={done ? 'Restart' : playing ? 'Pause' : 'Play'}
      >
        {done ? <RotateCcw aria-hidden /> : playing ? <Pause aria-hidden /> : <Play aria-hidden />}
      </Button>
      <div className="w-[260px] shrink-0 md:w-[340px]">
        <Slider
          aria-label="Replay position"
          min={0}
          max={Math.max(n - 1, 0)}
          step={1}
          value={[at]}
          onValueChange={(v) => {
            const idx = Array.isArray(v) ? Number(v[0]) : Number(v)
            const cp = story.checkpoints[idx]
            if (cp) onCursorChange(cp.index)
          }}
        />
      </div>
      <span className="w-[92px] shrink-0 font-mono text-[11px] whitespace-nowrap text-muted-foreground tabular-nums">
        {current?.step !== null && current?.step !== undefined
          ? `step ${current.step} / ${steps}`
          : (current?.label.toLowerCase() ?? '')}
      </span>
    </div>
  )
}

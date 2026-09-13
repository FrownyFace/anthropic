import { ChevronLeft, ChevronRight } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { checkpointWord, type Checkpoint, type StoryTone } from '@/lib/story'

/** Colour by tone; the word next to the title comes from the checkpoint's `kind` (story.ts). */
const TONE: Record<StoryTone, { dot: string; ring: string }> = {
  info: { dot: 'bg-muted-foreground', ring: 'border-border' },
  ok: { dot: 'bg-emerald-400', ring: 'border-emerald-500/30' },
  warn: { dot: 'bg-amber-400', ring: 'border-amber-500/30' },
  fail: { dot: 'bg-rose-400', ring: 'border-rose-500/30' },
}

/** The tour guide: what happened at the current checkpoint, in plain English, with prev/next. */
export function StoryBar({
  checkpoint,
  position,
  total,
  onPrev,
  onNext,
}: {
  checkpoint: Checkpoint | null
  position: number
  total: number
  onPrev: () => void
  onNext: () => void
}) {
  if (!checkpoint) return null
  const tone = TONE[checkpoint.tone]
  const word = checkpointWord(checkpoint.kind, checkpoint.tone)
  return (
    <div
      // Fixed height so the transcript below never jumps as narrations change length;
      // a long checkpoint scrolls inside its own box instead of growing the bar.
      className={`h-[108px] shrink-0 border-b bg-card/40 ${tone.ring}`}
      role="region"
      aria-label="What happened"
      aria-live="polite"
      data-kind={checkpoint.kind}
      data-tone={checkpoint.tone}
    >
      <div className="mx-auto flex h-full w-full max-w-4xl items-stretch gap-3 px-4 py-2.5 md:px-6">
        <Button
          size="sm"
          variant="ghost"
          className="h-7 w-7 shrink-0 self-center p-0"
          onClick={onPrev}
          disabled={position <= 0}
          aria-label="Previous step"
        >
          <ChevronLeft aria-hidden />
        </Button>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="flex shrink-0 items-center gap-2 text-[11px] tracking-wide text-muted-foreground uppercase">
            <span aria-hidden className={`inline-block size-1.5 rounded-full ${tone.dot}`} />
            <span>{checkpoint.title}</span>
            {word ? (
              <span className="font-mono normal-case" data-slot="story-word">
                · {word}
              </span>
            ) : null}
          </div>
          <p
            key={`${checkpoint.index}-${checkpoint.title}`}
            className="mt-1 min-h-0 flex-1 overflow-y-auto pr-2 text-[13.5px] leading-relaxed text-foreground/90 [scrollbar-width:thin]"
          >
            {checkpoint.text}
          </p>
        </div>
        <Button
          size="sm"
          variant="ghost"
          className="h-7 w-7 shrink-0 self-center p-0"
          onClick={onNext}
          disabled={position >= total - 1}
          aria-label="Next step"
        >
          <ChevronRight aria-hidden />
        </Button>
      </div>
    </div>
  )
}

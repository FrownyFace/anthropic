import { ChevronLeft, ChevronRight } from 'lucide-react'

import { Button } from '@/components/ui/button'
import type { Checkpoint, StoryTone } from '@/lib/story'

const TONE: Record<StoryTone, { dot: string; ring: string; word: string }> = {
  info: { dot: 'bg-muted-foreground', ring: 'border-border', word: '' },
  ok: { dot: 'bg-emerald-400', ring: 'border-emerald-500/30', word: 'recovered' },
  warn: { dot: 'bg-amber-400', ring: 'border-amber-500/30', word: 'fault' },
  fail: { dot: 'bg-rose-400', ring: 'border-rose-500/30', word: 'real failure' },
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
  return (
    <div
      className={`shrink-0 border-b bg-card/40 ${tone.ring}`}
      role="region"
      aria-label="What happened"
      aria-live="polite"
    >
      <div className="mx-auto flex w-full max-w-4xl items-start gap-3 px-4 py-3 md:px-6">
        <Button
          size="sm"
          variant="ghost"
          className="mt-0.5 h-7 w-7 shrink-0 p-0"
          onClick={onPrev}
          disabled={position <= 0}
          aria-label="Previous step"
        >
          <ChevronLeft aria-hidden />
        </Button>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-[11px] tracking-wide text-muted-foreground uppercase">
            <span aria-hidden className={`inline-block size-1.5 rounded-full ${tone.dot}`} />
            <span>{checkpoint.title}</span>
            {tone.word ? <span className="font-mono normal-case">· {tone.word}</span> : null}
          </div>
          <p className="mt-1 text-[13.5px] leading-relaxed text-foreground/90">{checkpoint.text}</p>
        </div>
        <Button
          size="sm"
          variant="ghost"
          className="mt-0.5 h-7 w-7 shrink-0 p-0"
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

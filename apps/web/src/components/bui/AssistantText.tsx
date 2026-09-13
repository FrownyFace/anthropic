/**
 * AssistantText — adapted from Beautiful UI `StreamingText` (MIT, © 2026 Shane Levine).
 *
 * The original streamed a demo sentence word by word and grew a sources / follow-ups tray. Here
 * the text arrives from the run reducer already-complete per turn, so the component only keeps
 * the prose style and the streaming cursor. Newlines are preserved; `code` and **bold** spans
 * get light inline styling without a markdown library.
 */

import { Fragment, type ReactNode } from 'react'

import { cn } from '@/lib/utils'

export interface AssistantTextProps {
  text: string
  /** Show the cursor after the last line and shimmer it while more text is on the way. */
  streaming?: boolean
  className?: string
}

const INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g

function renderInline(line: string, key: string): ReactNode[] {
  return line.split(INLINE).map((part, i) => {
    if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
      return (
        <code
          key={`${key}-${i}`}
          className="rounded-[4px] bg-muted/60 px-1 py-px font-mono text-[11.5px] text-ink"
        >
          {part.slice(1, -1)}
        </code>
      )
    }
    if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) {
      return (
        <strong key={`${key}-${i}`} className="font-medium text-ink">
          {part.slice(2, -2)}
        </strong>
      )
    }
    return <Fragment key={`${key}-${i}`}>{part}</Fragment>
  })
}

export function AssistantText({ text, streaming = false, className }: AssistantTextProps) {
  const lines = text.split('\n')
  return (
    <div
      aria-busy={streaming || undefined}
      className={cn('text-[13px] leading-relaxed text-ink', className)}
      data-slot="assistant-text"
    >
      <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
        {lines.map((line, i) => {
          const last = i === lines.length - 1
          return (
            <Fragment key={i}>
              {i > 0 ? '\n' : null}
              <span className={cn('inline', streaming && last && 'bui-shimmer text-ink-2')}>
                {renderInline(line, String(i))}
              </span>
            </Fragment>
          )
        })}
        {streaming ? (
          <span
            aria-hidden
            data-slot="cursor"
            className="bui-fade-in ml-0.5 inline-block h-3 w-0.5 translate-y-0.5 rounded-full bg-ink"
          />
        ) : null}
      </p>
    </div>
  )
}

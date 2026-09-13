import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Checkpoint } from '@/lib/story'

import { StoryBar } from './StoryBar'

function cp(over: Partial<Checkpoint>): Checkpoint {
  return { index: 3, step: 2, label: 'Step 2', title: 'Step 2', text: 'It read CHANGELOG.md.', tone: 'info', kind: 'info', ...over }
}

describe('StoryBar', () => {
  it('is a fixed-height region whose narration scrolls inside its own box (the transcript never jumps)', () => {
    render(<StoryBar checkpoint={cp({ text: 'x'.repeat(2000) })} position={1} total={5} onPrev={() => {}} onNext={() => {}} />)
    const region = screen.getByRole('region', { name: 'What happened' })
    expect(region.className).toContain('h-[108px]')
    expect(region.className).toContain('shrink-0')
    const narration = region.querySelector('p')!
    expect(narration.className).toContain('overflow-y-auto')
    expect(narration.textContent).toHaveLength(2000)
    expect(region.getAttribute('aria-live')).toBe('polite')
  })

  it('shows the word from the checkpoint kind, never from the colour', () => {
    const cases: [Checkpoint['kind'], Checkpoint['tone'], string | null][] = [
      ['recovery', 'ok', 'recovered'],
      ['fault', 'warn', 'fault'],
      ['real-failure', 'fail', 'real failure'],
      ['tests', 'ok', null], // a green test run is not "recovered"
      ['info', 'warn', null], // an amber ordinary step is not a "fault"
      ['verdict', 'ok', 'recovered'],
      ['verdict', 'warn', null],
      ['verdict', 'fail', null],
    ]
    for (const [kind, tone, word] of cases) {
      const { unmount } = render(<StoryBar checkpoint={cp({ kind, tone })} position={1} total={5} onPrev={() => {}} onNext={() => {}} />)
      const region = screen.getByRole('region', { name: 'What happened' })
      expect(region.getAttribute('data-kind')).toBe(kind)
      expect(region.getAttribute('data-tone')).toBe(tone)
      const el = region.querySelector('[data-slot="story-word"]')
      if (word === null) expect(el).toBeNull()
      else expect(el!.textContent).toBe(`· ${word}`)
      unmount()
    }
  })

  it('prev/next buttons, disabled at the ends', () => {
    const onPrev = vi.fn()
    const onNext = vi.fn()
    const { rerender } = render(<StoryBar checkpoint={cp({})} position={0} total={3} onPrev={onPrev} onNext={onNext} />)
    const prev = screen.getByRole('button', { name: 'Previous step' })
    const next = screen.getByRole('button', { name: 'Next step' })
    expect(prev.hasAttribute('disabled')).toBe(true)
    expect(next.hasAttribute('disabled')).toBe(false)
    fireEvent.click(next)
    expect(onNext).toHaveBeenCalledTimes(1)
    rerender(<StoryBar checkpoint={cp({})} position={2} total={3} onPrev={onPrev} onNext={onNext} />)
    expect(screen.getByRole('button', { name: 'Next step' }).hasAttribute('disabled')).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Previous step' }))
    expect(onPrev).toHaveBeenCalledTimes(1)
  })

  it('renders nothing without a checkpoint', () => {
    const { container } = render(<StoryBar checkpoint={null} position={0} total={0} onPrev={() => {}} onNext={() => {}} />)
    expect(container.innerHTML).toBe('')
  })
})

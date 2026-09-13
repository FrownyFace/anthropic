import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { AssistantText } from './AssistantText'

describe('AssistantText', () => {
  it('preserves newlines and styles inline code without a markdown library', () => {
    const { container } = render(
      <AssistantText text={'The write timed out.\nRe-reading `CHANGELOG.md` before writing again.'} />,
    )
    const p = container.querySelector('p')!
    expect(p.textContent).toBe('The write timed out.\nRe-reading CHANGELOG.md before writing again.')
    expect(p.className).toContain('whitespace-pre-wrap')
    const code = container.querySelector('code')!
    expect(code.textContent).toBe('CHANGELOG.md')
    expect(container.querySelector('[data-slot="cursor"]')).toBeNull()
    expect(container.firstElementChild!.getAttribute('aria-busy')).toBeNull()
  })

  it('shows the cursor and shimmers the last line while streaming', () => {
    const { container } = render(<AssistantText text={'first\nsecond'} streaming />)
    expect(container.querySelector('[data-slot="cursor"]')).not.toBeNull()
    expect(container.firstElementChild!.getAttribute('aria-busy')).toBe('true')
    const spans = container.querySelectorAll('p > span.inline')
    expect(spans).toHaveLength(2)
    expect(spans[0]!.className).not.toContain('bui-shimmer')
    expect(spans[1]!.className).toContain('bui-shimmer')
  })

  it('renders bold spans', () => {
    const { container } = render(<AssistantText text="Done. **6 passed**." />)
    expect(container.querySelector('strong')!.textContent).toBe('6 passed')
  })
})

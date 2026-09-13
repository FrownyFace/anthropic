import { useState } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { DEFAULT_MODEL, MODEL_ALLOWLIST } from '@/lib/types'

import { Composer, type ComposerProps } from './Composer'
import { filterScenarios, parseSlash } from './composerHelpers'
import { scenarios } from './test-fixtures'

type Spies = Pick<ComposerProps, 'onScenarioChange' | 'onModelChange' | 'onSeedChange' | 'onSubmit'>

/** The component is controlled; this holds the state a page would. */
function Harness({
  spies,
  initialPrompt = '',
  ...rest
}: { spies: Spies; initialPrompt?: string } & Partial<
  Pick<ComposerProps, 'disabled' | 'busy' | 'hint' | 'placeholder' | 'models'>
>) {
  const [scenarioId, setScenarioId] = useState<string | null>(null)
  const [model, setModel] = useState<string>(DEFAULT_MODEL)
  const [seed, setSeed] = useState<number | null>(null)
  const [prompt, setPrompt] = useState(initialPrompt)
  return (
    <Composer
      scenarios={scenarios}
      scenarioId={scenarioId}
      onScenarioChange={(id) => {
        setScenarioId(id)
        spies.onScenarioChange(id)
      }}
      models={MODEL_ALLOWLIST}
      model={model}
      onModelChange={(m) => {
        setModel(m)
        spies.onModelChange(m)
      }}
      seed={seed}
      onSeedChange={(s) => {
        setSeed(s)
        spies.onSeedChange(s)
      }}
      prompt={prompt}
      onPromptChange={setPrompt}
      onSubmit={spies.onSubmit}
      {...rest}
    />
  )
}

function spies(): Spies {
  return { onScenarioChange: vi.fn(), onModelChange: vi.fn(), onSeedChange: vi.fn(), onSubmit: vi.fn() }
}

describe('helpers', () => {
  it('parses a slash query only at the start of an otherwise empty draft', () => {
    expect(parseSlash('/')).toBe('')
    expect(parseSlash('/Lost-')).toBe('lost-')
    expect(parseSlash('run /lost')).toBeNull()
    expect(parseSlash('/lost ack')).toBeNull()
    expect(parseSlash('')).toBeNull()
  })

  it('filters scenarios by id prefix or title substring', () => {
    expect(filterScenarios(scenarios, 'lo').map((s) => s.id)).toEqual(['lost-ack'])
    expect(filterScenarios(scenarios, 'file').map((s) => s.id)).toEqual(['missing-file'])
    expect(filterScenarios(scenarios, '')).toHaveLength(3)
  })
})

describe('Composer', () => {
  it('only offers a dropdown when more than one model is allowed', () => {
    const s = spies()
    render(<Harness spies={s} models={['model-a', 'model-b']} />)
    expect(screen.queryByLabelText('Model')).toBeNull()
    expect(screen.getByRole('button', { name: 'Choose model' }).textContent).toContain(DEFAULT_MODEL)
  })

  it('opens the scenario menu on "/" and picks with the arrow keys + Enter', () => {
    const s = spies()
    render(<Harness spies={s} />)
    const input = screen.getByRole('textbox', { name: 'Prompt' }) as HTMLTextAreaElement
    expect(screen.queryByRole('listbox')).toBeNull()

    fireEvent.change(input, { target: { value: '/' } })
    const list = screen.getByRole('listbox', { name: 'Scenarios' })
    const options = screen.getAllByRole('option')
    expect(options).toHaveLength(3)
    expect(options[0]!.getAttribute('aria-selected')).toBe('true')
    expect(list.textContent).toContain('lost-ack')
    expect(list.textContent).toContain('Missing file')

    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(screen.getAllByRole('option')[1]!.getAttribute('aria-selected')).toBe('true')
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(s.onScenarioChange).toHaveBeenCalledWith('missing-file')
    expect(s.onSubmit).not.toHaveBeenCalled()
    expect(input.value).toBe(scenarios[1]!.task_prompt)
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(screen.getByRole('button', { name: 'Choose scenario' }).textContent).toContain('missing-file')
  })

  it('filters the menu as the query grows and dismisses on Escape', () => {
    const s = spies()
    render(<Harness spies={s} />)
    const input = screen.getByRole('textbox', { name: 'Prompt' })
    fireEvent.change(input, { target: { value: '/den' } })
    expect(screen.getAllByRole('option')).toHaveLength(1)
    expect(screen.getByRole('option').textContent).toContain('denied-write')
    fireEvent.change(input, { target: { value: '/zzz' } })
    expect(screen.queryAllByRole('option')).toHaveLength(0)
    expect(screen.getByText(/No scenario matches/)).toBeTruthy()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('opens the menu from the scenario chip too', () => {
    render(<Harness spies={spies()} />)
    const chip = screen.getByRole('button', { name: 'Choose scenario' })
    expect(chip.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(chip)
    expect(chip.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getAllByRole('option')).toHaveLength(3)
    fireEvent.click(screen.getAllByRole('option')[2]!)
    expect(chip.textContent).toContain('denied-write')
  })

  it('submits on Enter, inserts a newline on Shift+Enter, and never with an empty prompt', () => {
    const s = spies()
    render(<Harness spies={s} initialPrompt="Prepare release 0.2.0" />)
    const input = screen.getByRole('textbox', { name: 'Prompt' })
    const send = screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement
    expect(send.disabled).toBe(false)

    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(s.onSubmit).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(s.onSubmit).toHaveBeenCalledTimes(1)
    fireEvent.click(send)
    expect(s.onSubmit).toHaveBeenCalledTimes(2)

    fireEvent.change(input, { target: { value: '   ' } })
    expect(send.disabled).toBe(true)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(s.onSubmit).toHaveBeenCalledTimes(2)
  })

  it('disabled blocks sending but still lets the user pick a scenario, and shows the hint', () => {
    const s = spies()
    render(<Harness spies={s} initialPrompt="go" disabled hint="harness unreachable" />)
    expect(screen.getByText('harness unreachable')).toBeTruthy()
    const send = screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement
    expect(send.disabled).toBe(true)
    const input = screen.getByRole('textbox', { name: 'Prompt' }) as HTMLTextAreaElement
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(s.onSubmit).not.toHaveBeenCalled()
    // HomePage passes disabled until a scenario is chosen — "/" must still work
    expect(input.disabled).toBe(false)
    fireEvent.change(input, { target: { value: '/' } })
    expect(screen.getAllByRole('option')).toHaveLength(3)
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(s.onScenarioChange).toHaveBeenCalledWith('lost-ack')
    expect(s.onSubmit).not.toHaveBeenCalled()
  })

  it('shows a running send button while busy', () => {
    render(<Harness spies={spies()} initialPrompt="go" busy />)
    expect((screen.getByRole('button', { name: 'Running' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows the single allowed model as a static chip (no dropdown) and a numeric seed input', () => {
    const s = spies()
    render(<Harness spies={s} />)
    expect(MODEL_ALLOWLIST).toEqual(['claude-haiku-4-5'])
    expect(DEFAULT_MODEL).toBe('claude-haiku-4-5')
    expect(screen.queryByRole('button', { name: 'Choose model' })).toBeNull()
    const chip = screen.getByLabelText('Model')
    expect(chip.textContent).toContain('claude-haiku-4-5')
    expect(chip.getAttribute('title')).toBe('the only model enabled for this demo')
    expect(chip.tagName).toBe('SPAN')
    expect(screen.queryByRole('menu')).toBeNull()
    const seed = screen.getByLabelText('Seed') as HTMLInputElement
    fireEvent.change(seed, { target: { value: '7' } })
    expect(s.onSeedChange).toHaveBeenLastCalledWith(7)
    expect(seed.value).toBe('7')
    fireEvent.change(seed, { target: { value: '' } })
    expect(s.onSeedChange).toHaveBeenLastCalledWith(null)
  })
})

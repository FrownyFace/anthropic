import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ScoreRows } from './ScoreRows'
import { evaluation } from './test-fixtures'

describe('ScoreRows', () => {
  it('renders the score headline and one row per check', () => {
    render(<ScoreRows evaluation={evaluation} />)
    const headline = document.querySelector<HTMLElement>('[data-slot="score-rows"] > div')!
    expect(screen.getByLabelText('score').textContent).toBe('72')
    expect(within(headline).getByText('6 passed · 0 failed · 0 errors')).toBeTruthy()
    // tests passed but a recovery check failed: not a pass (gradeOf → partial), amber, never green
    expect(within(headline).getByText('Graded with failures')).toBeTruthy()
    expect(within(headline).queryByText('Passed')).toBeNull()
    expect(screen.getByLabelText('score').className).toContain('amber')
    expect(screen.getByLabelText('score').className).not.toContain('emerald')
    expect(document.querySelector('[data-slot="score-rows"]')!.getAttribute('data-grade')).toBe('partial')
    expect(screen.getByText('checks 2/3')).toBeTruthy()

    const rows = document.querySelectorAll<HTMLElement>('[data-slot="score-row"]')
    expect(rows).toHaveLength(4) // 3 checks + pytest
    expect(rows[0]!.getAttribute('data-ok')).toBe('true')
    expect(rows[1]!.getAttribute('data-ok')).toBe('false')
    expect(within(rows[1]!).getByText('no_duplicate_entry')).toBeTruthy()
    expect(within(rows[1]!).getByRole('img', { name: 'fail' })).toBeTruthy()
    expect(within(rows[1]!).getByText('Failed')).toBeTruthy()
    expect(within(rows[0]!).getByRole('img', { name: 'pass' })).toBeTruthy()
    expect(within(rows[3]!).getByText('pytest')).toBeTruthy()
  })

  it('expands a check to its detail and weight', () => {
    render(<ScoreRows evaluation={evaluation} />)
    const rows = document.querySelectorAll<HTMLElement>('[data-slot="score-row"]')
    const toggle = within(rows[1]!).getByRole('button')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(within(rows[1]!).getByText('two lines match ^## \\[0\\.2\\.0\\]')).toBeTruthy()
    expect(within(rows[1]!).getByText('weight 2')).toBeTruthy()
    expect(within(rows[1]!).getByText('fail')).toBeTruthy()
    expect(within(rows[2]!).getByText('weight 1')).toBeTruthy()
  })

  it('expands the pytest row to the raw output in a code block', () => {
    render(<ScoreRows evaluation={evaluation} />)
    const rows = document.querySelectorAll<HTMLElement>('[data-slot="score-row"]')
    fireEvent.click(within(rows[3]!).getByRole('button'))
    expect(within(rows[3]!).getByText('python -m pytest -q')).toBeTruthy()
    expect(within(rows[3]!).getByText(/6 passed in 0.09s/)).toBeTruthy()
    expect(rows[3]!.querySelectorAll('[data-line-kind="code"]')).toHaveLength(2)
  })

  it('is green only for a real pass: every test and every check ok (gradeOf, not the score alone)', () => {
    render(
      <ScoreRows
        evaluation={{
          ...evaluation,
          score: 100,
          checks: evaluation.checks.map((c) => ({ ...c, ok: true })),
        }}
      />,
    )
    const headline = document.querySelector<HTMLElement>('[data-slot="score-rows"] > div')!
    expect(within(headline).getByText('Passed')).toBeTruthy()
    expect(screen.getByLabelText('score').className).toContain('emerald')
    expect(document.querySelector('[data-slot="score-rows"]')!.getAttribute('data-grade')).toBe('passed')
    // a high score with a failed check is still not green
    render(
      <ScoreRows
        evaluation={{
          ...evaluation,
          score: 92,
          checks: [{ id: 'x', ok: false, weight: 0.2, detail: 'nope' }],
        }}
      />,
    )
    const all = document.querySelectorAll<HTMLElement>('[data-slot="score-rows"]')
    expect(all[1]!.getAttribute('data-grade')).toBe('partial')
    expect(within(all[1]!).getByText('Graded with failures')).toBeTruthy()
  })

  it('shows a failed headline when tests fail', () => {
    render(
      <ScoreRows
        evaluation={{
          ...evaluation,
          score: 12,
          passed: false,
          tests: { passed: 4, failed: 2, errors: 0, output: 'FAILED tests/test_x.py::test_y' },
        }}
      />,
    )
    expect(screen.getByLabelText('score').className).toContain('rose')
    const headline = document.querySelector<HTMLElement>('[data-slot="score-rows"] > div')!
    expect(within(headline).getByText('4 passed · 2 failed · 0 errors')).toBeTruthy()
    expect(within(headline).getByText('Failed')).toBeTruthy()
    const pytest = document.querySelectorAll<HTMLElement>('[data-slot="score-row"]')[3]!
    expect(pytest.getAttribute('data-ok')).toBe('false')
    expect(within(pytest).getByText('Failed')).toBeTruthy()
  })
})

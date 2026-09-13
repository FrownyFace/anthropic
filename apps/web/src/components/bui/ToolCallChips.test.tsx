import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'

import { ToolCallChips } from './ToolCallChips'
import { diffForCall } from './toolMeta'
import {
  changelogDiff,
  enoentRead,
  failedGrep,
  listDirCall,
  lostAckWrite,
  pytestCall,
  readCall,
  runningCall,
  submitCall,
  versionDiff,
} from './test-fixtures'

function renderChips(ui: React.ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>)
}

function row(tool: string, index = 0): HTMLElement {
  const rows = document.querySelectorAll<HTMLElement>(`[data-slot="tool-call"][data-tool="${tool}"]`)
  return rows[index]!
}

/** The always-visible header line of a row (the expandable panel is its sibling). */
function header(r: HTMLElement): HTMLElement {
  return r.firstElementChild as HTMLElement
}

function toggleOf(r: HTMLElement): HTMLElement {
  return header(r).querySelector('button[aria-expanded]')!
}

describe('ToolCallChips', () => {
  it('shows exit code, duration and ok status for a successful command', () => {
    renderChips(<ToolCallChips calls={[pytestCall]} />)
    const r = row('run_command')
    expect(within(r).getByText('Run')).toBeTruthy()
    expect(within(r).getByText('python -m pytest -q')).toBeTruthy()
    expect(within(r).getByText('exit 0')).toBeTruthy()
    expect(r.querySelector('[data-status]')!.getAttribute('data-status')).toBe('ok')
    expect(within(r).getByText('913 ms')).toBeTruthy()
  })

  it('marks a non-zero exit as an error', () => {
    renderChips(<ToolCallChips calls={[failedGrep]} />)
    const r = row('run_command')
    expect(within(r).getByText('exit 2')).toBeTruthy()
    expect(r.querySelector('[data-status]')!.getAttribute('data-status')).toBe('error')
  })

  it('shows the simulated-fault badge, the read-back hint and an UNKNOWN (not failed) status on the lost-ack write', () => {
    renderChips(<ToolCallChips calls={[lostAckWrite]} />)
    const h = header(row('write_file'))
    // origin word on the badge: this was the environment, not a real failure
    expect(within(h).getByText('simulated: lost ack')).toBeTruthy()
    expect(within(h).getByText('read-back seen')).toBeTruthy()
    // a lost ack means the write may well have landed: neutral "no ack", never a red cross
    expect(within(h).getByText('no ack')).toBeTruthy()
    expect(within(h).queryByText('error')).toBeNull()
    expect(h.querySelector('[data-status]')!.getAttribute('data-status')).toBe('unknown')
    expect(within(h).getByText('3.0 s')).toBeTruthy()
  })

  it('renders an unknown outcome from error_class as neutral and a real transport failure with a red origin badge', () => {
    const transport = {
      ...lostAckWrite,
      toolUseId: 'tu_tr',
      fault: null,
      recovered: false,
      result: {
        ...lostAckWrite.result!,
        outcome: 'unknown' as const,
        errorClass: {
          origin: 'real' as const,
          layer: 'transport' as const,
          code: 'ETRANSPORT' as const,
          kind: null,
          label: 'real: transport failure harness<->sandbox-env (outcome unknown)',
          outcome_known: false,
          side_effect_applied: null,
          detail: null,
        },
      },
    }
    renderChips(<ToolCallChips calls={[transport]} />)
    const h = header(row('write_file'))
    expect(h.querySelector('[data-status]')!.getAttribute('data-status')).toBe('unknown')
    expect(within(h).getByText('unknown')).toBeTruthy()
    const badge = within(h).getByText('real: ETRANSPORT')
    expect(badge.closest('[data-origin]')!.getAttribute('data-origin')).toBe('real')
  })

  it('renders a harness-reported failed / not_executed outcome and never lets a failure text decide', () => {
    const failed = {
      ...pytestCall,
      toolUseId: 'tu_f',
      result: { ...pytestCall.result!, outcome: 'failed' as const, isError: true, exitCode: 1 },
    }
    const refused = {
      ...readCall,
      toolUseId: 'tu_n',
      result: { ...readCall.result!, outcome: 'not_executed' as const, isError: true },
    }
    renderChips(<ToolCallChips calls={[failed, refused]} />)
    expect(row('run_command').querySelector('[data-status]')!.getAttribute('data-status')).toBe('error')
    expect(within(row('run_command')).getByText('exit 1')).toBeTruthy()
    expect(row('read_file').querySelector('[data-status]')!.getAttribute('data-status')).toBe('not-executed')
    expect(within(row('read_file')).getByText('not executed')).toBeTruthy()
  })

  it('shows a running state for a call without a result when live', () => {
    renderChips(<ToolCallChips calls={[runningCall]} live />)
    expect(screen.getByText('running')).toBeTruthy()
    expect(document.querySelector('[data-status="running"]')).not.toBeNull()
  })

  it('shows "no result" for a call without a result on a finished run', () => {
    renderChips(<ToolCallChips calls={[runningCall]} />)
    expect(screen.getByText('no result')).toBeTruthy()
  })

  it('expands to the input, the error parts and the diff for a mutating call', () => {
    renderChips(<ToolCallChips calls={[lostAckWrite]} diffs={[changelogDiff, versionDiff]} />)
    const r = row('write_file')
    // diff counts on the row header
    expect(within(header(r)).getByText('+4')).toBeTruthy()
    expect(within(header(r)).getByText('−0')).toBeTruthy()
    const toggle = toggleOf(r)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    const panel = document.getElementById(toggle.getAttribute('aria-controls')!)!
    expect(panel.getAttribute('aria-hidden')).toBe('false')
    expect(within(panel).getByText('input')).toBeTruthy()
    // error text, plain (non-JSON) 504 body
    expect(within(panel).getByText('error')).toBeTruthy()
    expect(within(panel).getByText(/504 Gateway Timeout/)).toBeTruthy()
    // unified diff rendered through CodeBlock
    expect(within(panel).getByText('diff')).toBeTruthy()
    expect(panel.querySelectorAll('[data-line-kind="add"]')).toHaveLength(4)
    // the highlighter splits numbers into their own spans, so compare the row's whole text
    const addRows = [...panel.querySelectorAll('[data-line-kind="add"]')].map((el) => el.textContent)
    expect(addRows.some((t) => t!.includes('## [0.2.0] - 2026-09-12'))).toBe(true)
  })

  it('renders the ENOENT error code, stdout/stderr, entries, bytes written and truncation', () => {
    const written = {
      ...readCall,
      seq: 9,
      toolUseId: 'tu_w',
      tool: 'write_file',
      mutating: true,
      read: false,
      input: { path: 'src/ratelimiter/version.py', content: '__version__ = "0.2.0"\n' },
      path: 'src/ratelimiter/version.py',
      result: {
        ...readCall.result!,
        content: null,
        bytesWritten: 22,
        truncated: true,
      },
    }
    renderChips(<ToolCallChips calls={[enoentRead, failedGrep, listDirCall, written]} />)
    for (const r of document.querySelectorAll<HTMLElement>('[data-slot="tool-call"]')) fireEvent.click(toggleOf(r))

    const enoent = row('read_file')
    expect(within(enoent).getByText('ENOENT')).toBeTruthy()
    expect(within(enoent).getByText(/No such file or directory/)).toBeTruthy()
    expect(within(enoent).getByText('simulated: missing file')).toBeTruthy()
    expect(within(enoent).queryByText('read-back seen')).toBeNull()
    // an injected missing_file never reached the sandbox: "not executed", not a failure
    expect(enoent.querySelector('[data-status]')!.getAttribute('data-status')).toBe('not-executed')

    const grep = row('run_command')
    expect(within(grep).getByText('stderr')).toBeTruthy()
    expect(within(grep).getByText(/grep: missing.txt/)).toBeTruthy()
    expect(within(grep).getByText('Output was truncated by the environment.')).toBeTruthy()

    const ls = row('list_dir')
    expect(within(ls).getByText('entries')).toBeTruthy()
    expect(within(ls).getByText(/d ratelimiter/)).toBeTruthy()

    const w = row('write_file')
    expect(within(w).getByText('22 bytes written')).toBeTruthy()
    expect(within(w).getByText('write')).toBeTruthy()
  })

  it('summarises a submit call by its summary text', () => {
    renderChips(<ToolCallChips calls={[submitCall]} />)
    expect(screen.getByText('Submit')).toBeTruthy()
    expect(screen.getByText('Bumped the version and added the changelog entry.')).toBeTruthy()
    expect(screen.getByText('ok')).toBeTruthy()
  })

  it('only attaches a diff to mutating calls on the same path', () => {
    expect(diffForCall(lostAckWrite, [changelogDiff, versionDiff])?.path).toBe('CHANGELOG.md')
    expect(diffForCall(readCall, [changelogDiff])).toBeNull()
    const shellWrite = {
      ...pytestCall,
      command: 'echo x >> CHANGELOG.md',
      input: { command: 'echo x >> CHANGELOG.md' },
      mutating: true,
      read: false,
    }
    expect(diffForCall(shellWrite, [versionDiff, changelogDiff])?.path).toBe('CHANGELOG.md')
  })
})

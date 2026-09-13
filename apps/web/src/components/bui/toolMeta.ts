/**
 * Pure helpers shared by the tool-call renderers (ToolCallChips, ThinkingTrace): icon and label
 * per tool, the one-line summary of a call, which workspace diff a call produced, and the result
 * parts to show. No React components here (keeps fast refresh happy).
 */

import {
  Ban,
  Check,
  CircleQuestionMark,
  FileText,
  FolderTree,
  LoaderCircle,
  PenLine,
  Send,
  Terminal,
  X,
  type LucideIcon,
} from 'lucide-react'

import type { CallStatusKind } from '@/lib/callStatus'
import { callTouches, normalisePath, type ToolCallView, type ToolResultView } from '@/lib/reducer'
import type { FileDiff } from '@/lib/types'

export interface StatusGlyph {
  /** null: the state has no icon (a pending call is a hollow dot on the rail, nothing on the pill). */
  Icon: LucideIcon | null
  /** Accessible name of the rail glyph (`role="img"`). */
  label: string
  /** Colour on the rail; the status pill colours by tone instead. */
  railClassName: string
}

/**
 * One icon per call status (`lib/callStatus.ts`), shared by the ThinkingTrace rail and the
 * ToolCallChips status pill so an unknown outcome is a question mark — never a cross — in both.
 */
export const STATUS_GLYPH: Record<CallStatusKind, StatusGlyph> = {
  running: { Icon: LoaderCircle, label: 'running', railClassName: 'text-ink-2' },
  pending: { Icon: null, label: 'pending', railClassName: 'text-ink-3' },
  ok: { Icon: Check, label: 'ok', railClassName: 'text-ink-3' },
  failed: { Icon: X, label: 'error', railClassName: 'text-rose-700 dark:text-rose-300' },
  unknown: { Icon: CircleQuestionMark, label: 'unknown', railClassName: 'text-amber-700 dark:text-amber-300' },
  not_executed: { Icon: Ban, label: 'not executed', railClassName: 'text-amber-700 dark:text-amber-300' },
}

export const TOOL_ICON: Record<string, LucideIcon> = {
  run_command: Terminal,
  read_file: FileText,
  write_file: PenLine,
  list_dir: FolderTree,
  submit: Send,
}

export const TOOL_LABEL: Record<string, string> = {
  run_command: 'Run',
  read_file: 'Read',
  write_file: 'Write',
  list_dir: 'List',
  submit: 'Submit',
}

export function toolLabel(tool: string): string {
  return TOOL_LABEL[tool] ?? tool
}

/** DOM id of a tool call's expandable row (ToolCallChips), so an interruption callout can link to it. */
export function toolCallDomId(toolUseId: string): string {
  return `tool-call-${toolUseId}`
}

/** One-line description of the call: the command, the path, or the raw input. */
export function summariseCall(call: ToolCallView): string {
  if (call.command) return call.command
  if (call.path) return call.path
  if (call.tool === 'submit' && typeof call.input.summary === 'string') return call.input.summary
  if (Object.keys(call.input).length === 0) return '(no input)'
  return JSON.stringify(call.input)
}

/** The workspace diff this call produced, if it is mutating and a diff exists for its path. */
export function diffForCall(call: ToolCallView, diffs: readonly FileDiff[]): FileDiff | null {
  if (!call.mutating) return null
  return (
    diffs.find((d) => (call.path ? normalisePath(d.path) === call.path : callTouches(call, d.path))) ??
    null
  )
}

export interface ResultPart {
  label: string
  body: string
  tone: 'error' | 'plain'
  /** Render as a numbered listing (file content) rather than a log block. */
  code?: { filename: string | null }
}

/** The result parts an expanded tool-call row shows, in display order. */
export function resultParts(result: ToolResultView, path: string | null): ResultPart[] {
  const parts: ResultPart[] = []
  if (result.errorText) parts.push({ label: result.errorCode ?? 'error', body: result.errorText, tone: 'error' })
  if (result.stdout) parts.push({ label: 'stdout', body: result.stdout, tone: 'plain' })
  if (result.stderr) parts.push({ label: 'stderr', body: result.stderr, tone: 'plain' })
  if (result.content !== null) {
    parts.push({ label: 'content', body: result.content, tone: 'plain', code: { filename: path } })
  }
  if (result.entries) {
    parts.push({
      label: 'entries',
      body: result.entries.map((e) => `${e.type === 'dir' ? 'd' : '-'} ${e.name}`).join('\n'),
      tone: 'plain',
    })
  }
  if (result.bytesWritten !== null) {
    parts.push({ label: 'write', body: `${result.bytesWritten} bytes written`, tone: 'plain' })
  }
  if (parts.length === 0 && result.output) parts.push({ label: 'output', body: result.output, tone: 'plain' })
  return parts
}

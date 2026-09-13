/** Small presentation helpers. No React, no side effects — safe to unit test. */

import type { ErrorOrigin, FaultKind } from './types'

export function fmtDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`
  const m = Math.floor(s / 60)
  const rem = Math.round(s % 60)
  return `${m}m ${String(rem).padStart(2, '0')}s`
}

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

export function fmtTokens(n: number): string {
  if (n < 1000) return String(n)
  return `${(n / 1000).toFixed(1)}k`
}

/** One-line preview of a command or path for a collapsed card header. */
export function oneLine(s: string, max = 120): string {
  const flat = s.replace(/\s+/g, ' ').trim()
  return flat.length <= max ? flat : `${flat.slice(0, max - 1)}…`
}

/**
 * What a fault *kind* does, for the scenario table where either mode may be in play
 * (services/sandbox-env/FAULTS.md).
 */
export const FAULT_KIND_BLURB: Record<FaultKind, string> = {
  missing_file:
    'Reads of one path are answered with ENOENT. Transient: intercepted at the tool boundary while the file stays on disk. Sticky: the file is left out of the workspace at reset, so the error is genuine until the agent recreates it.',
  denied_write:
    'The first writes to one path are refused with EACCES at the tool boundary; nothing is written until the fault lifts.',
  ack_lost:
    'A write runs for real, then the environment withholds the response. The agent cannot know the write landed.',
}

/**
 * What really happened for one fault that fired, by kind and origin. The injected and staged
 * variants of `missing_file` are different worlds: the injected file is still on disk, the staged
 * one really is absent.
 */
export function faultBlurb(kind: FaultKind | string, origin: ErrorOrigin = 'injected'): string {
  switch (kind) {
    case 'missing_file':
      return origin === 'staged'
        ? 'The file was left out of the workspace at reset, so this ENOENT is a genuine OS error: the file really is absent until the agent recreates it.'
        : 'A read was answered with ENOENT at the tool boundary; the file was on disk the whole time and is served normally once the fault lifts.'
    case 'denied_write':
      return 'The write was refused with EACCES at the tool boundary and nothing was written.'
    case 'ack_lost':
      return 'The write landed, but the environment withheld the response — the agent cannot know whether it applied.'
    default:
      return 'Injected failure.'
  }
}

/** First 7 hex chars of a sha256, for a file-list column. */
export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 7) : ''
}

/**
 * The first sentence of a prompt, for a one-line summary. Breaks on `.`, `!` or `?` followed by
 * whitespace (optionally after a closing quote/bracket/backtick), so "0.2.0" and "README.md:" do
 * not end the sentence. Capped at `max` chars with an ellipsis.
 */
export function firstSentence(s: string | null | undefined, max = 240): string {
  if (!s) return ''
  const flat = s.replace(/\s+/g, ' ').trim()
  const m = /[.!?][`'")\]]*(?=\s)/.exec(flat)
  const sentence = m ? flat.slice(0, m.index + m[0].length) : flat
  return sentence.length <= max ? sentence : `${sentence.slice(0, max - 1)}…`
}

/** `plural(1, 'file')` → "1 file", `plural(2, 'file')` → "2 files", `plural(2, 'retry', 'retries')`. */
export function plural(n: number, word: string, words = `${word}s`): string {
  return `${n} ${n === 1 ? word : words}`
}

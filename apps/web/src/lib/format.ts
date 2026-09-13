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

/** Plain words for a fault kind on a badge: `ack_lost` → "lost ack". Unknown kinds lose their underscores. */
export const FAULT_KIND_WORDS: Record<string, string> = {
  missing_file: 'missing file',
  denied_write: 'write denied',
  ack_lost: 'lost ack',
  worker_crash: 'worker crash',
  transport_abort: 'transport abort',
}

export function faultWords(kind: string): string {
  return FAULT_KIND_WORDS[kind] ?? kind.replace(/_/g, ' ')
}

/**
 * What a fault *kind* does, for the scenario table where either mode may be in play
 * (services/sandbox-env/FAULTS.md).
 */
export const FAULT_KIND_BLURB: Record<FaultKind, string> = {
  missing_file:
    'The agent is told the file does not exist. Staged: it really was deleted before the run started. Simulated: it is still on disk and the read is refused a few times.',
  denied_write:
    'The first writes to one file are refused with "permission denied". Nothing is written until the block lifts.',
  ack_lost:
    'The write happens, but the reply is withheld and comes back as a timeout. The agent cannot tell whether it landed.',
}

/**
 * What really happened for one fault, by kind and origin. The injected and staged variants of
 * `missing_file` are different worlds: the injected file is still on disk, the staged one really
 * is absent. Returns '' for a kind this table does not know, so callers can fall back to the
 * catalogue's own sentence.
 */
export function faultBlurb(kind: FaultKind | string, origin: ErrorOrigin = 'injected'): string {
  switch (kind) {
    case 'missing_file':
      return origin === 'staged'
        ? 'The file really was deleted before the run started, so this "no such file" is a genuine OS error until the agent recreates it.'
        : 'The read was refused before it reached the sandbox. The file was on disk the whole time and reads normally once the block lifts.'
    case 'denied_write':
      return 'The write was refused with "permission denied" before it reached the sandbox. Nothing was written.'
    case 'ack_lost':
      return 'The write landed, but the reply was withheld and came back as a timeout. The agent cannot tell whether it applied.'
    case 'worker_crash':
      return 'Real, not simulated: the harness process is killed while a write is in flight. A fresh worker resumes the run.'
    case 'transport_abort':
      return 'Real, not simulated: the connection between the harness and the sandbox is cut while a call is in flight.'
    default:
      return ''
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

/**
 * Unified JSON-lines logging for the browser — same shape as
 * packages/common/faultline_common/log.py, with svc:"web".
 *
 *   {"ts":ISO8601Z,"svc":"web","lvl":"info","run_id"?:str,"episode_id"?:str,"step"?:int,
 *    "ev":"dotted.event.name","msg":str, ...extras}
 *
 * Lines go to the console as a single JSON string (so a reviewer can copy/paste them next to the
 * platform log capture) AND into a bounded in-memory ring buffer that the Logs tab renders.
 */

import type { LogLevel, LogLine } from './types'

const LEVELS: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 }

const BUFFER_LIMIT = 2000

const minLevel: number = LEVELS.info
let buffer: LogLine[] = []
let seq = 0
const subscribers = new Set<(lines: LogLine[]) => void>()

export function nowIso(): string {
  return new Date().toISOString().replace(/(\.\d{3})\d*Z$/, '$1Z')
}

function notify(): void {
  const snapshot = buffer
  for (const fn of subscribers) fn(snapshot)
}

/** Append an already-formed unified log line (e.g. a `log` event mirrored from the harness). */
export function ingest(line: LogLine): void {
  buffer = [...buffer, { ...line, _seq: seq++ }].slice(-BUFFER_LIMIT)
  notify()
}

export function subscribeLogs(fn: (lines: LogLine[]) => void): () => void {
  subscribers.add(fn)
  fn(buffer)
  return () => {
    subscribers.delete(fn)
  }
}

const CONSOLE: Record<LogLevel, (msg: string) => void> = {
  debug: (m) => console.debug(m),
  info: (m) => console.info(m),
  warn: (m) => console.warn(m),
  error: (m) => console.error(m),
}

export function logEvent(
  svc: string,
  ev: string,
  msg = '',
  lvl: LogLevel = 'info',
  extras: Record<string, unknown> = {},
): LogLine | null {
  if (LEVELS[lvl] < minLevel) return null
  const rec: LogLine = { ts: nowIso(), svc, lvl, ev, msg }
  for (const [k, v] of Object.entries(extras)) {
    if (v !== undefined && v !== null) rec[k] = v
  }
  try {
    CONSOLE[lvl](JSON.stringify(rec))
  } catch {
    // JSON.stringify can throw on cyclic extras; never let logging break the app.
    CONSOLE[lvl](`{"ts":"${rec.ts}","svc":"${svc}","lvl":"${lvl}","ev":"${ev}","msg":"${msg}"}`)
  }
  ingest(rec)
  return rec
}

export class Logger {
  constructor(private readonly svc: string) {}

  debug = (ev: string, msg = '', extras: Record<string, unknown> = {}) =>
    logEvent(this.svc, ev, msg, 'debug', extras)

  info = (ev: string, msg = '', extras: Record<string, unknown> = {}) =>
    logEvent(this.svc, ev, msg, 'info', extras)

  warn = (ev: string, msg = '', extras: Record<string, unknown> = {}) =>
    logEvent(this.svc, ev, msg, 'warn', extras)

  error = (ev: string, msg = '', extras: Record<string, unknown> = {}) =>
    logEvent(this.svc, ev, msg, 'error', extras)
}

export function getLogger(svc: string): Logger {
  return new Logger(svc)
}

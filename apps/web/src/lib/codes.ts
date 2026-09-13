/**
 * Plain language for the error taxonomy (docs/error-taxonomy.md, packages/common schemas.py).
 *
 * Static mappings only: code → what it means, (origin, kind | layer) → the fixed label the
 * taxonomy prescribes, origin → the badge word. None of them look at message text, and none of
 * them can tell injected from real — only `origin` (a structured field) can.
 */

import type { ErrorCode, ErrorLayer, ErrorOrigin, FaultKind } from './types'
import { ERROR_CODES } from './types'

export interface CodeText {
  /** One or two sentences a reviewer can act on. */
  plain: string
}

export const CODE_TEXT: Record<ErrorCode, CodeText> = {
  ENOENT: {
    plain:
      'The path could not be found. Either the file is really absent (deleted at reset, or never created) or the environment withheld it for a few calls while it stayed on disk.',
  },
  EACCES: {
    plain:
      'The write was refused and nothing was written. A later write to the same path can still succeed once the refusal lifts.',
  },
  ETIMEDOUT: {
    plain:
      'The response never arrived. The operation may or may not have completed — read the file back before retrying.',
  },
  EINVAL: {
    plain:
      'The call was rejected before anything ran: a path outside /workspace, a bad mode, or a directory where a file was expected.',
  },
  ESANDBOX: {
    plain:
      'The sandbox that holds the workspace is gone or unreachable (terminated, not found, or its exec failed). Nothing ran, and later calls will fail the same way until a new sandbox exists.',
  },
  ETRANSPORT: {
    plain:
      'The HTTP/MCP hop between the harness and sandbox-env failed (connection reset, 5xx, or the client aborted). The server may still have completed the call.',
  },
  EHARNESS: {
    plain:
      'The harness worker itself was interrupted while this call was in flight. The request had already been dispatched, so the side effect may have landed; the ledger has the truth after the fact.',
  },
  EMODEL: {
    plain: 'The model API call that drives the turn failed. No tool ran; the harness retries or ends the run.',
  },
  EGYM: {
    plain:
      'A gym control-plane operation (reset, observe, evaluate or delete) failed. This is the environment, not the agent: a run that finished its work can still be left ungraded.',
  },
  EINTERNAL: {
    plain: 'An unexpected exception inside sandbox-env (a bug; see detail). The call did not reach the sandbox.',
  },
  ENOEPISODE: {
    plain: 'The request carried a missing or unknown episode id, so sandbox-env refused it before anything ran.',
  },
}

/** The dictionary entry for a code, or null for an unknown/absent code. */
export function codeText(code: string | null | undefined): CodeText | null {
  if (!code) return null
  return (ERROR_CODES as readonly string[]).includes(code) ? CODE_TEXT[code as ErrorCode] : null
}

/** Badge word for an origin: injected → "simulated", staged → "staged", real → "real". */
export function originText(origin: ErrorOrigin | 'unknown' | null | undefined): string {
  switch (origin) {
    case 'injected':
      return 'simulated'
    case 'staged':
      return 'staged'
    case 'real':
      return 'real'
    default:
      return 'unknown'
  }
}

/** Fixed labels the taxonomy prescribes for injected/staged faults, by kind. */
const INJECTED_LABEL: Record<FaultKind, string> = {
  missing_file: 'simulated: missing file (file still on disk)',
  denied_write: 'simulated: write denied (nothing written)',
  ack_lost: 'simulated: lost ack (write landed; response withheld)',
}

const STAGED_LABEL: Partial<Record<FaultKind, string>> = {
  missing_file: 'staged: file absent since reset',
}

/** Fixed labels the taxonomy prescribes for real failures, by layer. */
const REAL_LABEL: Record<ErrorLayer, string> = {
  filesystem: 'real: OS error in sandbox',
  sandbox: 'real: sandbox terminated or unavailable',
  transport: 'real: transport failure harness<->sandbox-env (outcome unknown)',
  harness: 'real: harness worker interrupted mid-call (outcome unknown)',
  model: 'real: model API error',
  gym: 'real: gym control-plane failure',
  boundary: 'real: internal error in sandbox-env',
}

/**
 * The taxonomy's own (origin, kind) / (origin, layer) → label table, for records that carry the
 * provenance fields but no `label` (e.g. a `fault.fired` without an `error_class`). Returns null
 * when the table has no row — never guesses.
 */
export function taxonomyLabel(
  origin: ErrorOrigin | 'unknown' | null | undefined,
  kind: FaultKind | string | null | undefined,
  layer: ErrorLayer | null | undefined,
): string | null {
  if (origin === 'injected') return (kind && INJECTED_LABEL[kind as FaultKind]) ?? null
  if (origin === 'staged') return (kind && STAGED_LABEL[kind as FaultKind]) ?? null
  if (origin === 'real') return (layer && REAL_LABEL[layer]) ?? null
  return null
}

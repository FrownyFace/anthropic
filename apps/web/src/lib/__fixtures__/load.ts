/**
 * Real RunRecords fetched from the deployed harness (GET /runs/{id}), with `anthropic_workspace`
 * and `sandbox_env_url` stripped. Test-only; nothing under src imports this at runtime.
 *
 *   worker-crash-resumed     r_1b83648dc693  worker-crash: EHARNESS at step 4 (planned), run.resumed
 *                                            by worker 2 from event 27, ledger.resolution, ok 100/100
 *   sandbox-lost-interrupted r_ea3204c9e1e7  lost-ack: ESANDBOX at step 1 (unplanned), episode.sandbox
 *                                            terminated, status interrupted, evaluation skipped
 *   lost-ack-live            r_9606e7fe615f  lost-ack: ack_lost with error_class/outcome, fault.fired
 *                                            whose data.step (ledger index 6) != Event.step (turn 4)
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import type { ConversationDetail, RunRecord } from '../types'

export type FixtureName = 'worker-crash-resumed' | 'sandbox-lost-interrupted' | 'lost-ack-live'

/**
 * GET /conversations/{id} for the same specimens, as their owner — the persisted (SQLite)
 * messages/blocks projection the browser renders after a reload:
 *
 *   conversation_worker-crash  c_1a0982afb68bab29b5bc380 / r_2991dd9a680a (ok 100, worker 2)
 *   conversation_sandbox-lost  c_1a0982c9806c48810779d36 / r_77368c6c998d (interrupted, score null)
 */
export type ConversationFixtureName = 'conversation_worker-crash' | 'conversation_sandbox-lost'

function read(name: string): unknown {
  // jsdom gives import.meta.url an http: scheme, so resolve off the vitest project root instead.
  return JSON.parse(readFileSync(resolve(process.cwd(), `src/lib/__fixtures__/${name}.json`), 'utf8'))
}

export function loadFixture(name: FixtureName): RunRecord {
  return read(name) as RunRecord
}

export function loadConversation(name: ConversationFixtureName): ConversationDetail {
  return read(name) as ConversationDetail
}

/**
 * Realistic fixtures for the bui component tests, shaped like public/demo/lost-ack.json folded
 * through the reducer. Results go through `parseToolResult` so the derived fields match what a
 * live run produces.
 */

import { parseToolResult, type ToolCallView } from '@/lib/reducer'
import type { EvaluateResponse, FileDiff, FileEntry, Scenario } from '@/lib/types'

function call(
  seq: number,
  step: number,
  tool: string,
  input: Record<string, unknown>,
  over: Partial<ToolCallView> = {},
): ToolCallView {
  const path = typeof input.path === 'string' ? (input.path as string) : tool === 'list_dir' ? '.' : null
  const command = typeof input.command === 'string' ? (input.command as string) : null
  return {
    seq,
    step,
    toolUseId: `tu_${String(seq + 1).padStart(2, '0')}`,
    tool,
    input,
    path,
    command,
    mutating: tool === 'write_file',
    read: tool === 'read_file' || tool === 'list_dir',
    result: null,
    fault: null,
    recovered: false,
    ...over,
  }
}

const CHANGELOG_BEFORE = '# Changelog\n\n## [0.1.0] - 2026-09-01\n\n- Initial token bucket.\n'
const CHANGELOG_AFTER =
  '# Changelog\n\n## [0.2.0] - 2026-09-12\n\n- Fix allowed_burst off-by-one.\n\n## [0.1.0] - 2026-09-01\n\n- Initial token bucket.\n'

/** Step 1: a plain successful read. */
export const readCall = call(0, 1, 'read_file', { path: 'CHANGELOG.md' }, {
  result: parseToolResult(
    'tu_01',
    JSON.stringify({ path: 'CHANGELOG.md', content: CHANGELOG_BEFORE, size: 60, sha256: 'a'.repeat(64), truncated: false }),
    false,
    118,
  ),
})

/** Step 2: the write whose ack was lost — faulted, later recovered. */
export const lostAckWrite = call(
  1,
  2,
  'write_file',
  { path: 'CHANGELOG.md', content: CHANGELOG_AFTER, mode: 'overwrite' },
  {
    result: parseToolResult(
      'tu_02',
      '504 Gateway Timeout: no response from sandbox after 3000ms; the operation may or may not have completed',
      true,
      3042,
    ),
    fault: { step: 2, kind: 'ack_lost', path: 'CHANGELOG.md', mode: 'transient' },
    recovered: true,
  },
)

/** A read answered with ENOENT by the missing_file fault, never recovered. */
export const enoentRead = call(2, 3, 'read_file', { path: 'config/settings.json' }, {
  result: parseToolResult(
    'tu_03',
    JSON.stringify({
      error: 'cat: config/settings.json: No such file or directory',
      code: 'ENOENT',
      path: 'config/settings.json',
    }),
    true,
    9,
  ),
  fault: { step: 3, kind: 'missing_file', path: 'config/settings.json', mode: 'transient' },
  recovered: false,
})

/** A directory listing. */
export const listDirCall = call(3, 3, 'list_dir', { path: 'src' }, {
  result: parseToolResult(
    'tu_04',
    JSON.stringify({
      path: 'src',
      entries: [
        { name: 'ratelimiter', type: 'dir', size: null },
        { name: 'README.md', type: 'file', size: 1180 },
      ],
    }),
    false,
    12,
  ),
})

/** `python -m pytest -q`, exit 0. */
export const pytestCall = call(4, 5, 'run_command', { command: 'python -m pytest -q', timeout_s: 60 }, {
  read: true,
  result: parseToolResult(
    'tu_05',
    JSON.stringify({ stdout: '......      [100%]\n6 passed in 0.08s\n', stderr: '', exit_code: 0, duration_ms: 913, truncated: false }),
    false,
    913,
  ),
})

/** A command that ran but failed (exit 2, stderr, output truncated). */
export const failedGrep = call(5, 5, 'run_command', { command: 'grep -c foo missing.txt' }, {
  read: true,
  result: parseToolResult(
    'tu_06',
    JSON.stringify({ stdout: '', stderr: 'grep: missing.txt: No such file or directory\n', exit_code: 2, duration_ms: 4, truncated: true }),
    false,
    4,
  ),
})

/** A call still waiting for its result. */
export const runningCall = call(6, 6, 'run_command', { command: 'python -m pytest -q' }, { read: true })

/** The final submit. */
export const submitCall = call(7, 7, 'submit', { summary: 'Bumped the version and added the changelog entry.' }, {
  result: parseToolResult('tu_08', JSON.stringify({ accepted: true }), false, 3),
})

export const changelogDiff: FileDiff = {
  path: 'CHANGELOG.md',
  unified:
    '--- a/CHANGELOG.md\n+++ b/CHANGELOG.md\n@@ -1,3 +1,7 @@\n # Changelog\n \n+## [0.2.0] - 2026-09-12\n+\n+- Fix allowed_burst off-by-one.\n+\n ## [0.1.0] - 2026-09-01\n',
}

export const versionDiff: FileDiff = {
  path: 'src/ratelimiter/version.py',
  unified: '--- a/src/ratelimiter/version.py\n+++ b/src/ratelimiter/version.py\n@@ -1 +1 @@\n-__version__ = "0.1.0"\n+__version__ = "0.2.0"\n',
}

export const files: FileEntry[] = [
  { path: 'README.md', size: 1180, sha256: 'efde28cb741df4c440e9fd5f88cbc326367168b404fa89b6a1cdd8de0aa7f411', status: 'unchanged' },
  { path: 'CHANGELOG.md', size: 381, sha256: '462280712d0d71fbbefb8bef1f78ae31d2b687888d08d173af04bd8532c9174b', status: 'modified' },
  { path: 'pytest.ini', size: 60, sha256: '5af92f079590dd729537e1178555aca8d71d7498acc6bcf1f6705dbab013d26e', status: 'unchanged' },
  { path: 'src/ratelimiter/version.py', size: 22, sha256: '667d4a15b970b851e20d17510224670c14646cfb6d5a1e388ca6b9cc6da8bf41', status: 'modified' },
  { path: 'notes/NEW.md', size: 2048, sha256: '1111111111111111111111111111111111111111111111111111111111111111', status: 'added' },
  { path: 'tests/old_test.py', size: 0, sha256: '2222222222222222222222222222222222222222222222222222222222222222', status: 'deleted' },
]

export const evaluation: EvaluateResponse = {
  episode_id: 'ep_test',
  score: 72,
  passed: true,
  checks: [
    {
      id: 'verified_before_rewrite',
      ok: true,
      weight: 2,
      detail: 'read_file CHANGELOG.md at step 3 precedes every later mutating call on that path',
    },
    { id: 'no_duplicate_entry', ok: false, weight: 2, detail: 'two lines match ^## \\[0\\.2\\.0\\]' },
    { id: 'version_bumped', ok: true, weight: 1, detail: 'src/ratelimiter/version.py contains __version__ = "0.2.0"' },
  ],
  tests: { passed: 6, failed: 0, errors: 0, output: '......      [100%]\n6 passed in 0.09s' },
  ledger: [],
}

export const scenarios: Scenario[] = [
  {
    id: 'lost-ack',
    title: 'Lost acknowledgement',
    description: 'A write lands but its response times out.',
    task_prompt: 'Prepare release 0.2.0 of this package following the Releasing section in README.md.',
    max_steps: 20,
    fault_kinds: ['ack_lost'],
  },
  {
    id: 'missing-file',
    title: 'Missing file',
    description: 'A read is answered with ENOENT.',
    task_prompt: 'Fix the config loader so it survives a missing settings file.',
    max_steps: 15,
    fault_kinds: ['missing_file'],
  },
  {
    id: 'denied-write',
    title: 'Denied write',
    description: 'A write is refused with EACCES.',
    task_prompt: 'Rotate the log file even when the target is read-only.',
    max_steps: 15,
    fault_kinds: ['denied_write'],
  },
]

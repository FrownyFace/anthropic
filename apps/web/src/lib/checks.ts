/**
 * Plain language for the grader's recovery check ids (services/sandbox-env/GRADING.md). The grader
 * is the independent verifier: hidden tests are uploaded into the sandbox only at evaluate time,
 * and these checks are computed from the ledger, which the agent never sees.
 */

export interface CheckText {
  /** what the check asserts, as a reviewer would say it */
  description: string
  /** one-line hint shown when the check failed */
  whenFailed: string
}

export const CHECK_TEXT: Record<string, CheckText> = {
  config_valid: {
    description: 'config/settings.json exists again with the documented values',
    whenFailed: 'the recreated config is missing or has the wrong values',
  },
  retried_transient_read: {
    description: 'after the simulated missing file, the agent tried the read again and succeeded',
    whenFailed: 'the agent gave up on README.md after the first (simulated) miss',
  },
  no_thrash: {
    description: 'no more than three identical failing calls in a row',
    whenFailed: 'the agent repeated the same failing call four or more times',
  },
  write_eventually_succeeded: {
    description: 'after the denied writes, a later write to the locked file succeeded',
    whenFailed: 'the locked file was never written successfully',
  },
  bounded_retries: {
    description: 'at most four write attempts on the locked file',
    whenFailed: 'more than four write attempts on the locked file',
  },
  verified_after_fix: {
    description: 'after the fix landed, the agent read the file back or re-ran the tests',
    whenFailed: 'the fix was never verified (no read-back, no test run)',
  },
  verified_before_rewrite: {
    description: 'after the lost acknowledgement, the agent read CHANGELOG.md before writing it again',
    whenFailed: 'the agent wrote again without reading back first',
  },
  no_duplicate_entry: {
    description: 'CHANGELOG.md has exactly one 0.2.0 section',
    whenFailed: 'the 0.2.0 section appears more than once (a blind retry duplicated it)',
  },
  version_bumped: {
    description: 'src/ratelimiter/version.py says 0.2.0',
    whenFailed: 'the version was not bumped to 0.2.0',
  },
}

export function checkText(id: string): CheckText {
  return CHECK_TEXT[id] ?? { description: id.replace(/_/g, ' '), whenFailed: `${id} failed` }
}

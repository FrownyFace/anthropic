/**
 * Plain language for the grader's recovery check ids (services/sandbox-env/GRADING.md). The grader
 * is the independent verifier: hidden tests are uploaded into the sandbox only at evaluate time,
 * and these checks are computed from the ledger, which the agent never sees.
 */

export interface CheckText {
  /** one-line hint shown when the check failed (what the check asserts comes from the backend's `Scenario.checks[].description`) */
  whenFailed: string
}

export const CHECK_TEXT: Record<string, CheckText> = {
  config_valid: {
    whenFailed: 'the recreated config is missing or has the wrong values',
  },
  retried_transient_read: {
    whenFailed: 'the agent gave up on README.md after the first (simulated) miss',
  },
  no_thrash: {
    whenFailed: 'the agent repeated the same failing call four or more times',
  },
  write_eventually_succeeded: {
    whenFailed: 'the locked file was never written successfully',
  },
  bounded_retries: {
    whenFailed: 'more than four write attempts on the locked file',
  },
  verified_after_fix: {
    whenFailed: 'the fix was never verified (no read-back, no test run)',
  },
  verified_before_rewrite: {
    whenFailed: 'the agent wrote again without reading back first',
  },
  no_duplicate_entry: {
    whenFailed: 'the 0.2.0 section appears more than once (a blind retry duplicated it)',
  },
  version_bumped: {
    whenFailed: 'the version was not bumped to 0.2.0',
  },
}

export function checkText(id: string): CheckText {
  return CHECK_TEXT[id] ?? { whenFailed: `${id} failed` }
}

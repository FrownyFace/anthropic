# Recovery grading

`evaluate` scores an episode as 60 × hidden-tests-pass + 40 × weighted recovery checks computed
from the tool ledger, so two agents that both end with green tests can score very differently
depending on how they behaved after a fault.

## Sub-features

- `grade-tests` hidden tests are uploaded at evaluate time, run, and removed; the count is parsed from pytest's summary and cross-checked with its exit code.
- `grade-verified-before-rewrite` after a lost ack, a read of the target must precede any further write to it.
- `grade-no-duplicate` `CHANGELOG.md` must contain exactly one `## [0.2.0]` heading.
- `grade-version-bumped` `src/ratelimiter/version.py` has `__version__ = "0.2.0"`.
- `grade-bounded-retries` / `grade-write-eventually-succeeded` for `locked-file`.
- `grade-fault-never-triggered` a ledger-dependent check is `ok: false` with that detail when the fault never fired.

## How to get to it (user POV)

- `POST …/episodes/{id}/evaluate` on any episode; the response carries `checks[] {id, ok, weight, detail}`, `tests`, `ledger`.
- The harness emits the same object as the `episode.evaluated` event and stores it on the run.

## Driving it with verify_backend.py

Preconditions:

- Doctor passes; `careful` runs before `careless` (the discrimination check compares them).

- **Careful recovery.** Stage `careful` (`outputs/careful_08_evaluate.json`, `scores.json["careful"]`). `score: 100`, `passed: true`, checks `verified_before_rewrite`, `no_duplicate_entry`, `version_bumped` all `ok`; `tests.passed > 0`, `failed: 0`. Independently: `files/careful/CHANGELOG.after_ack_lost.md` has exactly one 0.2.0 heading and `files/careful/version.before.py` differs from the written version.
- **Careless recovery.** Stage `careless` (`outputs/careless_06_evaluate.json`, `scores.json["careless"]`): append → ETIMEDOUT → blind append again. `files/careless/CHANGELOG.after_blind_retry.md` shows **two** headings; `no_duplicate_entry: false`, `verified_before_rewrite: false`, `passed: false` (the hidden release test fails on the duplicate), `score < 50`, and at least 50 points below the careful score.
- **Ledger agreement.** In the careful evaluate output the `ledger` has one `ack_lost` row on `write_file CHANGELOG.md` and the next row touching `CHANGELOG.md` is a `read_file`; the row count equals the calls the recipe made (6).
- **Locked-file grading.** Stage `faults` (`outputs/faults_dw_07_evaluate.json`). `write_eventually_succeeded` and `bounded_retries` ok, `passed: true`.

## Gotchas

- A read that itself failed does not count as verification.
- The checks are scenario-specific; the same check id can be absent in another scenario's response.
- `score` is a float (`100.0`); compare numerically.
- Do not trust `passed` without `tests.passed > 0`: an empty suite is scored as not run.

# Grading spec (sandbox-env)

`POST /episodes/{id}/evaluate` produces `EvaluateResponse` (see `faultline_common.schemas`).

**score = 60 × tests_pass + 40 × Σ(ok_i · w_i) / Σ(w_i)** over the scenario's `checks`.

## Tests

1. Copy the scenario's `hidden_tests` directory into the sandbox at `/workspace/.faultline_eval/`
   (never present during the episode).
2. Run `cd /workspace && PYTHONPATH=/workspace/src python -m pytest -q -p no:cacheprovider -o addopts= --confcutdir=/workspace/.faultline_eval tests .faultline_eval`
   (visible + hidden; `-o addopts=` neutralises the fixture's own `-q` so the summary line is printed;
   `--confcutdir` keeps an agent-written conftest.py from loading into the hidden tests). Hidden tests that
   are skipped or not collected count as a failed evaluation, never as a pass. Parse the summary line into `TestsResult{passed, failed, errors, output}`.
3. `rm -rf /workspace/.faultline_eval`. `passed = (failed == 0 and errors == 0 and passed > 0)`.

The eval directory is also removed **before** the upload: `untar` merges into its destination and
`.faultline_eval` is in `SKIP_DIRS` (invisible to `observe`/`list_dir`), so a `conftest.py` planted
at that exact path would sit *inside* `confcutdir` and be loaded for the hidden tests themselves.

**Threat model, stated honestly.** `--confcutdir` and the pre-upload wipe close the two paths by
which a file the agent writes can silently remove the hidden tests from the run (verified live:
`runs/20260913T005203Z_review_sandbox_env/grader_integrity_proof.json` — with a silencer
`conftest.py` planted at either path, the hidden tests still run and the evaluation is not a pass).
They are not a sandbox. The hidden tests import the agent's own package and run with
`PYTHONPATH=/workspace/src`, so an agent that *set out* to game the grader has code execution in
the grading interpreter anyway (`src/ratelimiter/__init__.py`, a `sitecustomize.py` on that path, a
rewritten `pytest.ini` `python_files`). Resisting that would mean grading in a second sandbox the
agent never touched; the recovery half of the score is ledger-based and unaffected either way.

## Ledger semantics used by checks

Each MCP tool call appends a `LedgerEntry`. Definitions:

- **touches(path)**: `entry.path == path`, or for `run_command`, any argv token normalises to `path`
  (strip quotes, leading `./`, leading `/workspace/`).
- **mutating**: `write_file` always; `run_command` if the command matches
  `(>>?|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\btouch\b|\bmkdir\b|\btruncate\b|\bpatch\b|\bgit\s+(apply|checkout|reset|restore))`
  or is a `python … -c` containing `open(` with a `'w'|'a'` mode.
- **read**: not mutating and touches(path) (`read_file`, `list_dir`, or `run_command` like cat/head/tail/grep/sed -n/wc/diff/less/cmp/sha256sum/ls/stat/pytest).
- **failing**: `outcome in {error, short_circuit, ack_lost}`.

## Check ids

| id | ok when |
|---|---|
| `config_valid` | `/workspace/config/settings.json` exists, parses as JSON, `capacity==10`, `refill_per_sec==2.0`, `burst_multiplier==1.5` |
| `retried_transient_read` | there is a `missing_file` `short_circuit` entry on `README.md`, and a later entry that reads `README.md` with `outcome == ok` |
| `no_thrash` | no run of > 3 consecutive entries with the same `args_digest` and failing outcome |
| `write_eventually_succeeded` | there is a `denied_write` `short_circuit` entry on `src/ratelimiter/limits.py` and a later mutating entry touching it with `outcome == ok` |
| `bounded_retries` | count of mutating entries touching `src/ratelimiter/limits.py` ≤ 4 |
| `verified_after_fix` | after the first successful mutating entry on `limits.py`, there is a read of `limits.py` or a `run_command` containing `pytest` with `outcome == ok` |
| `verified_before_rewrite` | let `t` = the `ack_lost` entry on `CHANGELOG.md` **or the entry marked `interrupted: true` on `CHANGELOG.md`** (harness-reported via `POST /episodes/{id}/interruptions`; see FAULTS.md). ok iff there exists a read of `CHANGELOG.md` after `t` that precedes every mutating entry touching `CHANGELOG.md` after `t`. If there is no mutating entry after `t` at all, ok iff a read after `t` exists. |
| `no_duplicate_entry` | `CHANGELOG.md` has exactly one line matching `^## \[0\.2\.0\]` |
| `version_bumped` | `src/ratelimiter/version.py` contains `__version__ = "0.2.0"` (either quote style) |

If a fault never fired (e.g. the agent never touched the path), ledger-dependent checks are `ok=False`
with `detail="fault never triggered"` — the scenario was not exercised.

## Fault matching (for `faults.py`)

- Applies to a call iff `touches(path)` and the tool/verb is relevant:
  `missing_file` → reads and mutating calls both get ENOENT for **transient** hits; **sticky** = file deleted at reset and nothing intercepted afterwards (the agent may legitimately recreate it).
  `denied_write` → only mutating calls; result `EACCES`, **nothing executed**.
  `ack_lost` → only mutating calls; **execute fully**, then sleep `delay_ms`, then return `ETIMEDOUT` (`is_error`).
- `hits` decrements per matching call; `None` = unlimited. Once 0, the fault is lifted.
- One fault per call max, evaluated in plan order.
- Error texts (verbatim style):
  - ENOENT: `cat: config/settings.json: No such file or directory` (run_command) / `read_file: config/settings.json: No such file or directory` (read_file), exit_code 1
  - EACCES: `bash: src/ratelimiter/limits.py: Permission denied` / `write_file: src/ratelimiter/limits.py: Permission denied`, exit_code 1
  - ETIMEDOUT: `504 Gateway Timeout: no response from sandbox after 3000ms; the operation may or may not have completed`

## Provenance (added 2026-09-12 18:30; implemented and verified live 19:55 — `tools/provenance_proof.py`, 44/44)

Every non-ok ledger row carries `origin` (`injected` = short-circuited or ack-withheld at the boundary,
`staged` = a real OS error on a path the scenario deleted at reset, `real` = an unplanned failure of the
sandbox/transport/etc.) and `error_code`. `faults_fired` in `observe` lists injected faults **and** the
first staged hit (`origin: staged`); it never lists real failures. Real sandbox failures use code
`ESANDBOX` (not `EINTERNAL`). `POST /episodes/{id}/interruptions` (body `InterruptionReport`) marks
the matching ledger row `interrupted: true`. See `FAULTS.md` for what each injector really does and
`docs/error-taxonomy.md` for the UI contract.

### Matching an interruption report

The report names a `tool` plus either an `args_digest` (exact) or a `path`; the gym marks the **most
recent** row with that tool whose `outcome` is `ok` or `ack_lost` — the calls whose side effect
really happened and whose answer the harness may have missed. Rows that already failed at the
boundary (`short_circuit`) or in the sandbox (`error`) are never candidates: the agent got a
definite answer for those. Re-reporting the same call is idempotent. If nothing matches (the call
never reached the gym at all) an annotation row is appended — `outcome: error`, `origin: real`,
`error_code` from the report, `interrupted: true` — so the step is visible in the evidence instead
of being a gap. The route never touches the sandbox, because it is needed exactly when the sandbox
is gone.

### An ungradeable episode is not a zero

If the sandbox cannot be reached while `evaluate` runs, the hidden tests and the file-reading checks
raise instead of being scored: the route answers **503 `{"code": "ESANDBOX"}`** and the episode
acquires no score at all. Scoring an unreadable workspace would report `score: 0.0`, `passed: false`
and every file check as "does not exist" — a scorecard that blames the agent for infrastructure.
The harness turns that 503 into run `status: unevaluated` with `error_class.layer: sandbox`
(`docs/error-taxonomy.md`), which is the honest answer.

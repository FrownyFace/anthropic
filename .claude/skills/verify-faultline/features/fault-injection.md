# Fault injection

The environment injects three failure kinds at the tool boundary so they look exactly like real
OS/transport failures: a file that is reported missing, a write that is denied, and a write that
lands but whose response times out. The agent's shell never sees the plan.

## Sub-features

- `fault-missing-transient` first N reads of a path fail with ENOENT and listings hide it, then it is back.
- `fault-missing-sticky` the file is really deleted at reset and stays gone until recreated.
- `fault-denied-write` first N writes to a path return EACCES and nothing is written.
- `fault-ack-lost` the write is performed, the response is held ~3 s, then returns ETIMEDOUT (504-style).
- `fault-visibility` `observe.faults_fired` lists only faults that already fired; pending ones stay hidden.

## How to get to it (user POV)

- Scenario `missing-config` (transient on `README.md`, sticky on `config/settings.json`).
- Scenario `locked-file` (denied_write ×2 on `src/ratelimiter/limits.py`).
- Scenario `lost-ack` (ack_lost ×1 on `CHANGELOG.md`, `delay_ms` 3000).
- `scripts/smoke_roundtrip.py --scenario missing-config --fault-proof` for the ENOENT-then-ok proof only.

## Driving it with verify_backend.py

Preconditions:

- Doctor passes.

- **Transient missing file.** In a fresh `missing-config` episode: `list_dir .` then `read_file README.md` twice, `list_dir .` again, `run_command "ls -la README.md && sha256sum README.md"` (`outputs/faults_mf_*`). First listing lacks `README.md`; first read is `is_error` with `code: ENOENT`; second read returns content (`files/faults/README.second_read.md`); second listing shows it; the shell command exits 0 — the file never left disk.
- **Sticky missing file.** `read_file config/settings.json` and `run_command "cat config/settings.json"` (`outputs/faults_mf_06_*`, `_07_*`). ENOENT from the tool and `exit_code: 1` with `No such file or directory` on stderr from the shell.
- **Denied write.** In a fresh `locked-file` episode: `read_file src/ratelimiter/limits.py` (`files/faults/limits.before.py` contains `- 1`), `write_file` the fix twice, reading back after each (`outputs/faults_dw_02_*`, `_03_*`). Both writes are `is_error` with `code: EACCES` and the sha is unchanged. Third `write_file` succeeds and `files/faults/limits.after.py` equals the fix; pytest exits 0.
- **Lost ack.** In a fresh `lost-ack` episode: `write_file CHANGELOG.md` (`outputs/careful_04_write_changelog.json`). `is_error`, `code: ETIMEDOUT`, `dur_ms ≥ 2500`; the immediate `read_file CHANGELOG.md` (`files/careful/CHANGELOG.after_ack_lost.md`) equals the content that was sent.
- **Visibility.** `GET /episodes/{id}` after each of the above (`outputs/*_observe.json`). `faults_fired` contains the fired kind with its step and path; nothing about faults that have not fired.

## Gotchas

- Listings never consume a transient hit; reads and writes do. Do not "warm up" a path with `ls` and expect the fault to be spent.
- Only pure enumeration commands are filtered for the missing-file illusion (`ls`, `find`); `ls -la | cat` is not.
- `ack_lost` is always `is_error` (transport failure), even for `run_command`; a short-circuited `run_command` for ENOENT/EACCES is a normal result with `exit_code: 1`.
- The sandbox has no network: `curl` inside `run_command` fails; that is expected and is part of the boundary.

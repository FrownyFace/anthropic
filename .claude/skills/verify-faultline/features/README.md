# Faultline verification map

Maintained source for verifying the user-facing behaviour of Faultline's runnable surfaces: the
gym + MCP tool server (`faultline-sandbox-env`), the run API (`faultline-harness`), and the browser
UI (`faultline-web`), all on Modal env `local`. Read this index, then use the matching feature file
as the recipe. Backend recipes drive with `helpers/verify_backend.py`; the browser recipe drives with
`helpers/verify_web.py` (Playwright through the real page).

## Baseline preconditions

- Both services answer `/health` with `ok: true` and `has_provider_key: false`
  (`verify_backend.py --skip careful,careless,faults`).
- Modal secret `anthropic-secret` exists in env `local` (live episodes only).
- `services/sandbox-env/.venv` exists (has `httpx` + `fastmcp`); `services/agent-harness/.venv` for the CLI.
- Every recipe creates its own episode(s) and deletes them; never drive an episode you did not create.
- Evidence goes under `runs/<ts>_verify/` (see SKILL.md "Evidence"); `runs/` is gitignored and is never cleaned.

## Driving conventions

- Every MCP call carries `X-Faultline-Episode: <episode_id>`; there is no episode argument.
- Tool results are JSON text: `{"stdout","stderr","exit_code",...}` for `run_command`,
  `{"content","size","sha256"}` for `read_file`, `{"bytes_written","sha256"}` for `write_file`,
  `{"entries":[...]}` for `list_dir`, or `{"error","code",...}` with `is_error: true`.
- A non-zero `exit_code` is a normal result; `is_error` is reserved for ENOENT/EACCES/ETIMEDOUT/EINVAL/EINTERNAL/ENOEPISODE.
- Treat every command as literal; use the exact paths (`CHANGELOG.md`, `src/ratelimiter/limits.py`, `config/settings.json`).

## Proof and skip reporting

- Capture the action (tool call + args) and the resulting state (read-back content, observe diff, ledger row).
- A grader score is never sufficient on its own: re-check the claim from the file contents you read back.
- Record the episode id and stage with every artifact; report an unreachable path with the attempted
  command and the unmet precondition (e.g. harness mid-redeploy).
- Do not report the browser path as verified through the API path.

## Feature entry contract

Each feature file has an H1, one paragraph, and exactly four H2s: `Sub-features`, `How to get to it
(user POV)`, `Driving it with verify_backend.py` (or `Driving it with verify_web.py` for the browser
surface), `Gotchas`.

## Features

- [Gym round trip](./gym-round-trip.md) — reset, MCP tools, observe, evaluate, delete for one episode.
- [Fault injection](./fault-injection.md) — the three injected faults as the agent experiences them.
- [Recovery grading](./grading.md) — careful vs careless recovery and what the score must say.
- [Live episode](./live-episode.md) — a real model run through the harness API and SSE stream.
- [Secret boundary](./secret-boundary.md) — the provider key is reachable only inside `run_episode`.
- [Web UI](./web-ui.md) — the user product flows in the browser (landing, composer, live run → conversation, persisted transcript, replays with a plain-English story bar, workspace, identity, theme, failure modes, sidebar), driven with Playwright through the real page.

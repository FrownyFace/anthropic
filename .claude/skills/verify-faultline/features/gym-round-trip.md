# Gym round trip

One episode from reset to teardown: the environment provisions an isolated workspace, the agent's
tools run real shell commands in it over MCP, `observe` shows what changed, `evaluate` grades it,
and `delete` tears the sandbox down.

## Sub-features

- `gym-reset` creates an episode for a scenario and returns the starting file list.
- `gym-tools` exposes exactly `run_command`, `read_file`, `write_file`, `list_dir` over MCP.
- `gym-shell` runs a real shell in the sandbox (hostname, listing, pytest).
- `gym-observe` reports per-file status, unified diffs and faults fired so far.
- `gym-evaluate` runs hidden tests plus recovery checks and returns a score with a ledger.
- `gym-delete` terminates the sandbox; the episode is then `done` or 404.

## How to get to it (user POV)

- `POST https://appliedlabsai-local--faultline-sandbox-env-api.modal.run/episodes {"scenario_id": "lost-ack"}`.
- MCP endpoint `POST …/mcp` (streamable HTTP) with header `X-Faultline-Episode: <episode_id>`.
- `GET …/episodes/{id}`, `POST …/episodes/{id}/evaluate`, `DELETE …/episodes/{id}`.
- Broad smoke: `services/sandbox-env/.venv/bin/python scripts/smoke_roundtrip.py --scenario lost-ack`.

## Driving it with verify_backend.py

Preconditions:

- Doctor passes (`verify_backend.py --skip careful,careless,faults`).
- No other client is driving the *same* episode id.

- **Reset.** Create the episode. Run `verify_backend.py` (stage `careful`, output `outputs/careful_reset.json`). HTTP 200 with `episode_id`, `files` containing `README.md`, `CHANGELOG.md`, `pytest.ini`, and no `fault_plan` field.
- **Discover tools.** `list_tools` over MCP (`outputs/careful_tools.json`). Exactly the four tool names; no tool takes an `episode` argument.
- **Run a shell command.** `run_command "python -m pytest -q -o addopts="` (`outputs/careful_06_pytest.json`, `files/careful/pytest.stdout.txt`). `exit_code: 0` and a `… passed` line — real pytest output from inside the sandbox.
- **Observe.** `GET /episodes/{id}` (`outputs/careful_07_observe.json`, `files/careful/diff_*.patch`). `CHANGELOG.md` and `src/ratelimiter/version.py` have status `modified`; a diff contains `+## [0.2.0]`.
- **Evaluate.** `POST /episodes/{id}/evaluate` (`outputs/careful_08_evaluate.json`, `scores.json`). `score`, `passed`, `checks[]`, `tests{passed,failed,errors}` and a `ledger[]` with one row per tool call made (6 for the careful recipe).
- **Delete.** `DELETE /episodes/{id}` then `GET /episodes/{id}` (`outputs/cleanup_*`). The second call is 404 or `done: true`.

## Gotchas

- Reset takes ~1.5–2 s warm; the first reset after a redeploy can take ~10 s (sandbox image warm-up). Modal caps a request at 150 s — a JSONDecodeError on reset means it hit that cap; re-run once.
- `POST /episodes` returns 200 (not 201).
- The fixture's `pytest.ini` sets `-q`; without `-o addopts=` a second `-q` suppresses the summary line entirely (the grader already handles this; your own `run_command` should pass `-o addopts=` if you want the count).
- `evaluate` may be called more than once; only `DELETE` ends the episode.

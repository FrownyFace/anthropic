# Live episode

A real model run: the harness resets an episode, discovers the MCP tools, loops on the Anthropic
API with a recovery-oriented system prompt, streams every event to the browser, evaluates, and
always deletes the episode. The user watches it through the run API / SSE stream (and, once
deployed, the web UI).

## Sub-features

- `live-create` `POST /runs {scenario_id, model?, seed?, max_steps?}` returns `run_id` immediately (the run is spawned).
- `live-stream` `GET /runs/{id}/events` is SSE with `id` = event sequence; closes at ~110 s with `event: done {reason: "window"}` (reconnect with `Last-Event-ID`) and ends with `{reason: "finished"}`.
- `live-record` `GET /runs/{id}` returns the whole `RunRecord` (events, evaluation, usage).
- `live-recovery` the model reads the changelog back after the lost ack instead of re-appending.
- `live-catalogue` `GET /scenarios` returns `{scenarios: [...]}` proxied from sandbox-env.

## How to get to it (user POV)

- `services/agent-harness/.venv/bin/python scripts/run_episode_cli.py --scenario lost-ack [--model claude-sonnet-5] [--out DIR] [--quiet]`.
- Directly: `POST https://appliedlabsai-local--faultline-harness-api.modal.run/runs`, then tail `/runs/{id}/events` or poll `/runs/{id}`.

## Driving it with verify_backend.py

Preconditions:

- Doctor passes including `harness.sees_sandbox_env`.
- Modal secret `anthropic-secret` present (the run fails at the first model call otherwise, with status `error` and a clear `run.finished`).

- **Start and tail.** Run `verify_backend.py --live` (stage `live`; CLI stdout in `files/live_cli.stdout.txt`, evidence in `live/{events.jsonl,run.json,evaluate.json,summary.txt}`). CLI exit 0; `run.json.status == "ok"`.
- **Fault observed.** `live/events.jsonl` contains a `fault.fired` with `kind: ack_lost` on `CHANGELOG.md` and a `tool.result` whose output carries `ETIMEDOUT`.
- **Recovery from the trace, not the grader.** The first `tool.call` touching `CHANGELOG.md` after that result is a read (`read_file`, or a non-mutating `run_command`); `files/live/CHANGELOG.read_back_by_model.md` (the content the model actually read) has exactly one `## [0.2.0]`; no mutating call on the changelog follows.
- **Grade.** `run.json.evaluation.passed == true` and `score ≥ 60`; `scores.json["live"]` records score, checks, usage and steps.
- **Stream integrity.** Event ids are `0..n-1` contiguous and the last event is `run.finished`.

## Gotchas

- Haiku is a real agent: a run can legitimately score below 100 (e.g. it re-wrote without reading). That is a finding about the model, not a verification failure of the environment — record it, and compare with `scores.json["careful"]` which proves the grader itself.
- Do not start a run within ~10 s of `modal deploy` of the harness; a draining container may run the old code.
- The key is org-scoped: the harness must send `anthropic-workspace-id` (from `ANTHROPIC_WORKSPACE` in the secret). A 400 mentioning workspace scoping means the secret lost that key.
- `GET /runs` and `GET /scenarios` are wrapped objects (`{runs: [...]}`, `{scenarios: [...]}`).

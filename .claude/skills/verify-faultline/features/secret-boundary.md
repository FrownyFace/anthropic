# Secret boundary

The Anthropic API key exists in exactly one place — the harness `run_episode` Modal function — and
neither web-facing API, nor the gym service, nor the agent's sandbox can see it or reach the network.

## Sub-features

- `secret-harness-api` `/health` on the harness reports `has_provider_key: false` and refuses to boot otherwise.
- `secret-sandbox-env` `/health` on sandbox-env reports `has_provider_key: false`; no secrets are attached to its functions.
- `secret-sandbox-shell` `env` inside the sandbox has no `ANTHROPIC*` variables; outbound network is blocked.
- `secret-only-run-episode` Modal logs show `has_key: true` only from `run_episode`, with the workspace id masked.

## How to get to it (user POV)

- `curl …/health` on both services.
- Inside an episode: `run_command "env | grep -i anthropic || echo none"` and `run_command "curl -sS -m 5 https://example.com || echo BLOCKED"`.
- `modal app logs faultline-harness -e local | grep has_key`.

## Driving it with verify_backend.py

Preconditions:

- Doctor passes.

- **Web-facing functions.** Stage `doctor` (`outputs/doctor_sandbox_health.json`, `outputs/doctor_harness_health.json`). Both `has_provider_key: false`.
- **Inside the sandbox.** In any episode run `run_command "env | grep -i anthropic || echo none"` and `run_command "curl -sS -m 5 https://example.com || echo BLOCKED"` (recorded by `scripts/smoke_roundtrip.py`; also in `runs/20260912T220451Z_faults/` from the build phase). stdout `none` and `BLOCKED`.
- **Logs.** `modal app logs faultline-harness -e local | grep has_key` after a live run. `has_key: true` appears only on `run_episode.start` lines; `api.boot` lines say `false`; the key value never appears.

## Gotchas

- `modal.App(secrets=[...])` or `Image.env` would leak the key to every function; the boundary depends on the secret being attached only to `run_episode` — check `services/agent-harness/modal_app.py` if `/health` ever flips.
- `block_network=True` on the sandbox cannot be combined with allowlists; if a scenario ever needs `pip install`, the boundary changes and this feature must be re-verified.

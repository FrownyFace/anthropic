# sandbox-env: lifecycle, cost guardrails and measured latency

Owner: `services/sandbox-env`. Written 2026-09-12 23:1x UTC, after the Extend pass.
Nothing in the gym contract (PLAN.md §2.2) changed — the routes below are **additive ops routes**.
`POST /episodes`, `GET /episodes/{id}`, `POST /episodes/{id}/evaluate`, `DELETE /episodes/{id}`,
`GET /scenarios`, `GET /health` and `/mcp` are all byte-for-byte what they were.

## 1. Who reclaims a sandbox, in order

| # | Mechanism | When | Blast radius |
|---|---|---|---|
| 1 | `DELETE /episodes/{id}` | the harness' `finally` | one episode |
| 2 | terminate-on-error inside `reset` / `evaluate` | reset fails after the sandbox exists (including `KeyboardInterrupt` and a failed Dict write), or `evaluate` finds the sandbox unreachable | one episode |
| 3 | TTL sweep | on every `reset` (throttled to once per `EPISODE_SWEEP_INTERVAL_S` per container) and via `POST /episodes/sweep` / `modal run …::sweep` | every episode older than `EPISODE_TTL_S` |
| 4 | `modal run -e local services/sandbox-env/modal_app.py::reap` | manual | stray sandboxes in `faultline-sandboxes`; live episodes are **spared** unless `--force` |
| 5 | the sandbox's own `timeout=1800` / `idle_timeout=600` | always | one sandbox |

Environment knobs (all read at import): `EPISODE_TTL_S` (1800), `EPISODE_PURGE_S` (86400, when a
terminated record is dropped from the Dict), `EPISODE_SWEEP_INTERVAL_S` (300),
`EPISODE_SWEEP_MAX` (200 episodes per scan).

## 2. New ops routes

```
GET  /episodes?probe=true&limit=200
     -> {count, live, probed, ttl_s, purge_s,
         episodes: [{episode_id, scenario_id, created_at, age_s, step,
                     sandbox_id, alive, terminated, done, score, finished_at}]}
POST /episodes/sweep[?ttl_s=600]
     -> {scanned, ttl_s, purge_s, expired: [...], purged: [...], errors: [...]}
```

* `alive` is `true`/`false` from `Sandbox.poll()`, or **`null` when we did not look**
  (`?probe=false`). Never render `null` as "alive".
* The listing is newest-first and includes terminated episodes until they are purged — that is
  deliberate, it is how you see what *just* finished.
* `GET /health`'s `detail` now also carries `episode_ttl_s`.

## 3. `reap` is now safe by default (changed 2026-09-12 19:55, PLAN.md §2.11)

```
modal run -e local services/sandbox-env/modal_app.py::reap             # spares live episodes
modal run -e local services/sandbox-env/modal_app.py::reap --dry-run   # census only, kills nothing
modal run -e local services/sandbox-env/modal_app.py::reap --force     # terminate EVERYTHING
```

A sandbox is skipped when it belongs to an episode that is **not done** and was created less than
`EPISODE_TTL_S` (1800 s) ago; each skip is logged (`ev: sandbox.reap_skipped`, with the episode id,
age and step) and returned in `skipped` / `skipped_detail`. This used to be opt-in (`--keep-active`,
which still works) and the cost of that default was run `r_ccda8780cbee`: a concurrent reap took a
*running* episode's sandbox at step 4. Cleanup should not be able to break a live demo, so you now
have to ask for that explicitly with `--force`.

If the episode Dict cannot be read, reap **refuses to run** rather than quietly widening its blast
radius — `--force` is the way to say you meant it. Whatever it kills it also writes back to the
Dict, so `GET /episodes` never keeps claiming a reaped sandbox is alive.

## 4. Measured latency (live, 5 samples, `services/sandbox-env/tools/perf_probe.py`)

`runs/20260912T230704Z_perf/latency.json`, client-side wall time from a laptop, scenario
`lost-ack`, while another session was running its own episodes against the same deployment.

| op | min | p50 | mean | max |
|---|---|---|---|---|
| reset (`POST /episodes`) | 1760 | 3283 | 2873 | 3893 |
| MCP `list_tools` | 105 | 108 | 130 | 212 |
| `list_dir` | 450 | 506 | 617 | 1123 |
| `read_file` | 465 | 475 | 652 | 1314 |
| `write_file` | 478 | 1123 | 882 | 1166 |
| `run_command` (echo+ls) | 403 | 426 | 429 | 463 |
| `run_command` (pytest) | 625 | 667 | 708 | 924 |
| observe (`GET /episodes/{id}`) | 448 | 561 | 832 | 1334 |
| evaluate | 1080 | 1452 | 1745 | 2495 |
| `GET /episodes` (145 records) | 1302 | 1367 | 1515 | 2055 |
| delete | 222 | 549 | 421 | 553 |

All values in ms. Add ~3 s to any tool call that trips the `ack_lost` fault (`delay_ms`), so keep
MCP client timeouts above ~10 s. Nothing here is remotely near Modal's 150 s request cap.

## 5. Two traps found while measuring

1. **Scanning the episode Dict cost one RPC per episode.** With 125 stale episodes the sweep on the
   reset path added ~4.5 s to *every* reset (measured: reset p50 6.7 s → 3.3 s after the fix). The
   fix is `modal.Dict.items()` (one streamed call, 1.16 s for 125 records vs ~50 ms *per* `get`)
   plus the throttle. If you scan a Modal Dict anywhere else, do the same.
2. **`modal deploy` does not restart warm web containers.** `add_local_python_source(copy=False)`
   is mounted at container start, so a container that keeps receiving traffic keeps serving the old
   code — a new route 404'd for 9 minutes after a successful deploy while other sessions' polling
   kept the old container alive. Forcing a burst of concurrent requests (40+) starts new containers
   on the new build. Assert on `/health` (it now carries `episode_ttl_s`) before trusting a deploy.

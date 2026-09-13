# Faultline — living plan

> **Faultline** is a mini agent harness with a browser view. A Claude agent runs *real* shell
> commands (read files, edit code, run tests) inside an isolated Modal Sandbox. The environment
> deliberately injects failures — a missing file, a denied write, a write that lands but whose
> response times out — and the UI shows every command, its output, the file changes, and whether
> the agent recovered. It is framed as a tiny RL gym (`reset / step / observe / evaluate`) so the
> same environment can grade recovery behaviour, not just task completion.
>
> Assignment: Anthropic Platform SWE take-home. Themes: **3 Systems & Reliability** (primary) and
> **4 Evaluation** (secondary). Target 1–2h, hard limit 8h. Hosted prototype + repo + rationale
> (video + doc) + AI transcripts. **Track time in §9.**

This file is the single source of truth. Check boxes as work lands; add notes inline. When a
decision changes, edit the decision, don't append a new one.

---

## 0. Status board

| Area | State | Notes |
|---|---|---|
| Research (gym / harness / sandbox / Modal) | ✅ | 3 briefs + synthesis in `docs/design-brief.md`; adopted/overridden in §2.8 |
| Repo scaffold | ✅ | layout, `.env.example`, `faultline_common` (log + 42 schema models), fixture `ratelimiter` (6 tests green), 5 scenarios (4 injected-fault + `worker-crash`, §2.11), hidden graders, `GRADING.md` |
| sandbox-env (gym + MCP) | ✅ core | deployed at `https://appliedlabsai-local--faultline-sandbox-env-api.modal.run`; 225 tests green (re-run 19:25 EDT); **`scripts/smoke_roundtrip.py` PASS 23/23 + fault-proof 29/29 (real Modal Sandbox shell via MCP, evidence `runs/20260912T220413Z_smoke*`)**; V3 fault proofs for all three kinds in `runs/20260912T220451Z_faults/` (lost-ack careful = 100, careless blind re-append = 8). Reset p50 ≈3.1–3.3 s (`runs/20260912T230531Z_perf`, `runs/20260912T230704Z_perf`; early builder figure was 1.5–2 s), tool call 0.15–0.7 s, evaluate ≈1.1 s |
| agent-harness (model loop) | ✅ core | deployed at `https://appliedlabsai-local--faultline-harness-api.modal.run` (153 tests green at 19:25 EDT, `/health` has_provider_key=false, key seen only in `run_episode`). **First live episode 17:59 EDT: `lost-ack` run `r_e9bc5c8c6739` scored 100/100** (ack_lost fired step 4 → agent re-read → no duplicate → 8/8 tests; 53.6 s; evidence `runs/20260912T215923Z_lost-ack_live/`). Persistence = SQLite `Store` (§2.9, next row) |
| harness `Store` (SQLite on Modal Volume) | ✅ | live since ~19:00 EDT: `/me`, `/conversations*`, `POST /conversations/{id}/runs` answer 200; snapshot restored after a harness redeploy; the 21 Modal-Dict runs were imported once. Evidence `runs/20260912T230110Z_store/`, notes `docs/store-notes.md`; design `ARCHITECTURE.md` §4 |
| Error classification + `worker-crash` (§2.11) | ✅ core / 🟡 hardening | **Proven live** (`runs/20260913T002900Z_interruptions` + `…T003600Z` repeat, 65/65): `error_class`/`outcome` on every tool result, `fault.fired` provenance, statuses `unevaluated`/`interrupted`, `interruption`/`run.resumed`/`episode.sandbox`, `ESANDBOX`; worker-crash `r_2991dd9a680a`/`r_1b83648dc693` resumed on worker 2 and scored 100; sandbox-loss ends `interrupted`. **Not finished (Review2 stopped at wrap-up 21:35 EDT)**: N1 per-episode control token / owner scoping, N5 stale-run finalisation, N7 landing barrier, Store B6–B9 + consistency window, backfill of `r_ccda8780cbee`, two-container log evidence, dead code — all listed in `runs/20260912T232850Z_cleanup-review/REVIEW2.md` |
| apps/web (Vite + shadcn + Beautiful UI on Modal Server) | ✅ | **Redeployed ~23:25 UTC** → `https://appliedlabsai-local--faultline-web-site.us-east.modal.direct` (187 tests / 21 files, tsc and build clean as of faultline-web v20, ~20:20 EDT, reported by anthropic-4c). Sidebar block + conversation rail + identity (**verified live against the Store**: conversation created from the UI, persisted transcript restored after reload, other identities 404), ChatGPT-style landing with a six-column scenario data table, replays for all four injected-fault scenarios (real captures), Beautiful UI transcript, **resizable workspace**, **virtualized logs**. Evidence `runs/20260912T225240Z_web/`. Pending: taxonomy fields render once the backend emits them; `worker-crash` replay |
| Deployed + smoke evidence in `runs/` | 🟡 | V1 ✅ V2 ✅ V3 ✅ V4 ✅ (all 4 scenarios live on Haiku = 100; weak run = 8) V5 🟡 (web deployed at `https://appliedlabsai-local--faultline-web-site.us-east.modal.direct` by the other session; browser e2e pending taxonomy) V6 ✅ V7 🟡 V8 ⬜. **Verification skill `.claude/skills/verify-faultline` executed 18:15 EDT: 65/65 assertions PASS across doctor/careful/careless/faults/live/cleanup/survival, evidence `runs/20260912T221506Z_verify/` (71 files, hashed after cleanup)** |
| Submission artefacts | 🟡 | repo pushed, README/RATIONALE/ARCHITECTURE/PLAN finalised 21:45 EDT, all three apps redeployed; **open for the candidate**: record the video (`docs/VIDEO_SCRIPT.md`), export the Claude Code transcripts (this + parallel sessions), send the email |

Legend: ⬜ not started · 🟡 in progress · ✅ done · ❌ cut

---

## 1. Goals, non-goals, done condition

**Goal.** A reviewer opens a URL, picks a scenario, presses *Run*, and watches a Claude agent
recover (or not) from injected faults while editing a small Python repo, with a graded result.
No local install for the reviewer. Works even if the live model call fails (bundled replay).

**Done condition (whole project).**
- [ ] Three Modal apps deployed in env `local` and reachable: `faultline-web`, `faultline-harness`, `faultline-sandbox-env`.
- [ ] A live run of every bundled scenario completes end-to-end in the browser with a score card.
- [ ] Evidence for ≥1 run per scenario saved under `runs/` (gitignored) and one exported to `apps/web/public/demo/` for replay.
- [ ] Conversations, messages and LLM turns persist in SQLite on a Modal Volume: they survive a page reload, a browser restart (same browser) and a redeploy of the harness (§2.9).
- [ ] `README.md` explains architecture + how to run; `RATIONALE.md` written; video recorded; transcripts exported.

**Non-goals (explicitly out).** Real authentication (identity is an anonymous browser id — a
scoping convenience, not a security boundary, §2.9.1), GPU anything, arbitrary user repos, streaming
model tokens (we stream *events*, not tokens), RL training, free-form chat with the agent (a
conversation is a thread of runs; the user's message *is* the task prompt).

---

## 2. Design (decisions + research notes)

### 2.1 Architecture and trust boundaries

```
 browser (apps/web: Vite + shadcn + Beautiful UI, static on Modal Server; anonymous user id in localStorage + cookie mirror)
    │  HTTPS JSON + SSE (reconnecting, Last-Event-ID); every request carries X-Faultline-User
    ▼
 services/agent-harness  (Modal app "faultline-harness")
    ├─ api          : FastAPI @modal.asgi_app — /me, /conversations…, /runs…, GET /runs/{id}/events (SSE), /scenarios, /health
    │                 NO provider secret. Identifies the browser by X-Faultline-User. Spawns run_episode; reads/writes ONLY via Store.
    ├─ Store        : @app.cls(max_containers=1, volumes={"/data": faultline-db}) — the single SQLite writer (§2.9).
    │                 users / conversations / runs / events / messages / blocks / llm_calls. Hot DB on local disk,
    │                 consistent snapshots checkpointed to the Modal Volume. No secret, no fault plan.
    └─ run_episode  : @app.function(secrets=[anthropic-secret]) — the ONLY code that sees ANTHROPIC_API_KEY.
                      Owns the model loop (anthropic SDK, model from ANTHROPIC_MODEL / per-run override).
                      Talks to sandbox-env over MCP (streamable HTTP) + gym REST. Appends events to Store.
    │  HTTPS: gym REST (reset/observe/evaluate) + MCP /mcp (step)
    ▼
 services/sandbox-env  (Modal app "faultline-sandbox-env")   ← controller, fault plan, grader live HERE
    └─ api : FastAPI @modal.asgi_app with FastMCP mounted at /mcp. Stateless containers; episode state in
             modal.Dict "faultline-episodes" {sandbox_id, scenario, seed, fault_plan, ledger, baseline}.
             Every tool call: load episode → consult fault plan → (maybe) exec in the Modal Sandbox → record ledger.
    │  modal.Sandbox.from_id(sid).exec(...)   (block_network=True, no secrets, per-episode)
    ▼
 Modal Sandbox (the *command sandbox*): fixture repo at /workspace, python3 + pytest. Sees nothing else.
```

Trust boundary summary (what each process can see):

| Process | Provider key | Fault plan / grader | Workspace files | Network |
|---|---|---|---|---|
| browser | ✗ | ✗ (sees *events* about faults after the fact) | via observe API | harness only |
| harness `api` | ✗ | ✗ | ✗ | Store + spawn |
| harness `Store` | ✗ | ✗ | ✗ (only what events carry: diffs, outputs) | Volume only |
| harness `run_episode` | ✓ (secret `anthropic-secret`: `ANTHROPIC_API_KEY`, optional `ANTHROPIC_WORKSPACE`) | ✗ | via MCP only | Anthropic + sandbox-env |
| sandbox-env `api` | ✗ | ✓ | via Sandbox API | Modal Sandbox only |
| Modal Sandbox (agent's shell) | ✗ | ✗ | ✓ | **none** (`block_network=True`) |

Why three deployables: the harness must be replaceable (different model/prompt) without touching
the environment; the environment must be gradeable without trusting the agent; the UI is static.


Diagrams (component graph + the lost-ack / worker-crash sequence): `ARCHITECTURE.md` §2.1.

### 2.2 Gym contract (sandbox-env)

Gymnasium-style, but the *step* surface is MCP tools (so any MCP-speaking harness can play).

REST (JSON) on `https://appliedlabsai-local--faultline-sandbox-env-api.modal.run`:

| Route | Purpose | Body → Response |
|---|---|---|
| `POST /episodes` (**reset**) | provision sandbox, copy fixture, apply sticky faults, store plan | `{scenario_id, seed?}` → `{episode_id, scenario: {id,title,task_prompt,max_steps}, workspace_root, files: [...]}` |
| `GET /episodes/{id}` (**observe**) | tree + diff vs baseline + public ledger (faults *fired* so far, without revealing pending ones) | → `{episode_id, step, files:[{path,size,sha,status:added/modified/deleted/unchanged}], diffs:[{path,unified}], faults_fired:[...], done}` |
| `POST /episodes/{id}/evaluate` | grader: upload hidden tests into sandbox, run pytest, remove; compute recovery metrics from ledger | → `{score, passed, checks:[{id,ok,detail}], tests:{passed,failed,output}, ledger:[...]}` |
| `DELETE /episodes/{id}` | terminate sandbox | → `{terminated:true}` |
| `GET /scenarios` | bundled scenario catalogue (public fields only) | |
| `GET /health` | | |
| `GET /episodes` · `POST /episodes/sweep` | ops routes (additive): episode list with sandbox liveness; TTL sweep (`docs/sandbox-env-ops.md`) | |

MCP (streamable HTTP, stateless) at `/mcp`; episode selected by header `X-Faultline-Episode: <episode_id>`
(middleware → contextvar; tools never take the id as an argument so the model can't spoof another episode).

| Tool | Input | Output (text + structured) | Faults that can apply |
|---|---|---|---|
| `run_command` | `{command: str, timeout_s?: int≤60}` | `{stdout, stderr, exit_code, duration_ms, truncated}` | `missing_file` (argv token match), `denied_write` and `ack_lost` (argv token match on mutating verbs) |
| `read_file` | `{path}` | `{content, size, sha256}` or ENOENT | `missing_file` |
| `write_file` | `{path, content, mode: "overwrite"\|"append"}` | `{bytes_written, sha256}` | `denied_write`, `ack_lost` |
| `list_dir` | `{path?}` | `{entries:[{name,type,size}]}` | `missing_file` (matching names are hidden from the listing; no ENOENT) |

Every tool result is capped (`stdout`/`stderr` 8,000 characters each, `content` 32,000 characters; `STDIO_CAP`/`CONTENT_CAP` in `schemas.py`) and marked `truncated`.
Every call is appended to the **ledger**: `{step, tool, args_digest, fault?:{kind,path,mode}, outcome, ts, duration_ms}`.

**Fault plan schema** (per scenario; the episode seed is stored but unused; lives only in the Dict, never in the sandbox):

```json
{"faults": [
  {"kind": "missing_file", "path": "config/settings.json", "mode": "sticky",    "hits": null},
  {"kind": "missing_file", "path": "README.md",            "mode": "transient", "hits": 1},
  {"kind": "denied_write", "path": "src/ratelimiter/limits.py", "mode": "transient", "hits": 2},
  {"kind": "ack_lost",     "path": "CHANGELOG.md",         "mode": "transient", "hits": 1, "delay_ms": 3000}
]}
```

How each fault is realised **at the tool boundary** (sandbox-env, outside the shell):

| Kind | Reset | On matching call | What the agent sees | Recovery we grade |
|---|---|---|---|---|
| `missing_file` sticky | file deleted from fixture before agent starts | — | `ENOENT` from cat/read | agent recreates it from the spec in README/tests; hidden tests pass |
| `missing_file` transient | nothing | first `hits` reads short-circuit with ENOENT (no exec) | flaky FS | agent retries/lists dir instead of giving up |
| `denied_write` | nothing | first `hits` writes short-circuit with `EACCES: Permission denied`; **write not performed** | locked file | agent retries; final content correct; bounded retries (≤4 write attempts) |
| `ack_lost` | nothing | **write IS performed**, then sleep `delay_ms`, then return `is_error` "504: response timed out; operation may or may not have completed" | ambiguous timeout | agent **verifies** (read/cat/grep/sha) before retrying; **no duplicate append**; tests pass |

**Score** = 60 · tests_pass + 40 · recovery (checks weighted per check; weights live in each scenario JSON, see `GRADING.md`), reported with a
per-check breakdown. Step budget `max_steps` (default 20) → `truncated` if exceeded.

### 2.3 Harness loop (agent-harness/run_episode)

- SDK: `anthropic` (Python). Client: `anthropic.Anthropic()` (reads `ANTHROPIC_API_KEY` from the secret).
- Model: **Haiku only** (user decision 21:30 EDT): `MODEL_ALLOWLIST = ["claude-haiku-4-5"]`; any other `model` on `POST /runs` / `POST /conversations/{id}/runs` → 400 `model must be claude-haiku-4-5`; `run_episode` never sends another model; `/health.model_default` = `claude-haiku-4-5`. No thinking param. (Enforcement in the harness lands right after Review2.)
- Tools: discovered from MCP `list_tools` and converted to Anthropic tool defs (+ a local `submit` tool
  that ends the episode with a summary). Parallel tool calls executed in order, all results returned in one user message.
- Loop: manual `while stop_reason == "tool_use"` (no beta dependency), `max_steps`, per-call timeouts,
  typed error chain (`RateLimitError` → retry w/ backoff; `APIStatusError ≥500` → retry; else fail the run cleanly).
- System prompt strategy (recovery-oriented): one command per step, verify state after any error,
  after a timeout **read before re-writing**, prefer idempotent edits, run tests before submitting.
- Events appended to the Store (`Store().append_events.remote(run_id, [events])`, batched per step,
  idempotent on `(run_id, seq)`) as they happen. The loop also keeps them in memory and re-sends the
  full list at `run.finished`, so a Store restart self-heals (§2.9):

```
run.started {run_id, scenario, model, seed}
episode.reset {episode_id, files}
turn.text {step, text}                       # assistant prose
tool.call {step, tool, input, tool_use_id}
tool.result {step, tool_use_id, output, is_error, duration_ms, fault?}   # fault echoed by sandbox-env public ledger
llm.call {step, attempt, model, stop_reason, usage:{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}, request_id, duration_ms}   # one per messages.create — the "LLM turn"
turn.thinking {step, text}                   # thinking block (summary) when adaptive thinking is on
fault.fired {step, kind, path, mode}         # emitted by harness after reading ledger delta
workspace.diff {step, files, diffs}          # from observe, after each mutating step
episode.evaluated {score, checks, tests}
run.finished {status: ok|error|truncated, usage:{input_tokens, output_tokens}, duration_ms}
log {level, msg, ...}                        # verbose unified log lines mirrored into the stream
```

Why not run the loop inside the SSE request: Modal web endpoints cap requests at **150 s**
(303-redirect polling after that breaks CORS). So `POST /runs` → `run_episode.spawn(...)` and the
browser tails `GET /runs/{id}/events` via SSE (the `api` polls `Store.events_after(run_id, after_seq)`
every 400 ms); the server closes each stream at ~110 s and the browser reconnects with
`Last-Event-ID` (= `events.seq`). `GET /runs/{id}` returns the whole record (also what we save as
evidence).

### 2.4 Modal deployment shapes (verified against modal 1.5.5 locally)

- Workspace `appliedlabsai`, environment `local` (web suffix `local`) → URLs
  `https://appliedlabsai-local--<app>-<fn>.modal.run` (label truncation at 63 chars).
- sandbox-env: `@app.function(image=IMG, timeout=300) @modal.concurrent(max_inputs=20) @modal.asgi_app()` → `api`.
  Sandboxes: `modal.Sandbox.create(app=modal.App.lookup("faultline-sandboxes", create_if_missing=True), image=SANDBOX_IMG, workdir="/workspace", timeout=1800, idle_timeout=600, block_network=True, cpu=1, memory=1024)`;
  exec via `sb.exec("bash","-lc",cmd, timeout=…)`; uploads via `sb.filesystem.write_bytes`; reads and tree walks go through an exec'd helper (`/opt/faultline_helper.py`, `sandbox_env/workspace.py`).
- harness: `api` as above (no secrets); `run_episode` = `@app.function(image=IMG, secrets=[modal.Secret.from_name("anthropic-secret")], timeout=900)`.
  Env: `SANDBOX_ENV_URL` (deploy-time env or `modal.Function.from_name("faultline-sandbox-env","api").get_web_url()` at runtime).
- harness store: `vol = modal.Volume.from_name("faultline-db", create_if_missing=True)` (v1 — v2 needs an explicit
  `version=2` and the 1.5.5 CLI still flags it experimental; it buys nothing for one file).
  `@app.cls(image=IMG, volumes={"/data": vol}, max_containers=1, min_containers=1, timeout=60) @modal.concurrent(max_inputs=32) class Store`
  with `@modal.enter` (restore snapshot → `/tmp`, migrate), `@modal.exit` (checkpoint), one `@modal.method` per operation.
  Same-app callers: `await Store().events_after.remote.aio(...)` from `api`, `Store().append_events.remote(...)` from `run_episode`.
  Local entrypoint `checkpoint_now` forces a snapshot for evidence/export (§5).
- web: `@app.server(image=WEB_IMG, unauthenticated=True, port=8000, min_containers=1)` class with `@modal.enter` → `python -m http.server -d /site 8000`.
  Image builds the Vite app: **Node 22** (Vite 8 needs `^20.19 || >=22.12`, vitest 5 needs `^22.12 || ^24`; `.nvmrc`=22) + pnpm installed in image, `add_local_dir("apps/web", copy=True)`, `run_commands("pnpm i --frozen-lockfile && pnpm build")` with `VITE_HARNESS_URL` baked in **and** a runtime `config.json` fallback so the URL can change without a rebuild.
  Fallback if Server misbehaves: `@modal.web_server(8000)`.
- Deploy order: sandbox-env → harness → web. Commands in §5.
- Secret: `anthropic-secret` (keys `ANTHROPIC_API_KEY`, optional `ANTHROPIC_WORKSPACE`), env `local`. Attached **only** to `run_episode`.
  Guard: `run_episode` logs `has_key=True/False` and `workspace=<masked>`; `api` asserts the key is *absent* in its env at startup.

### 2.5 Unified logging

One line per event, JSON, to stdout in every service (Modal captures → `modal app logs <app> -e local`),
mirrored to the browser console in the web app with the same shape:

```
{"ts":"2026-09-12T14:03:22.114Z","svc":"sandbox-env","lvl":"info","run_id":"r_…","episode_id":"ep_…","step":4,"ev":"tool.result","tool":"write_file","fault":"ack_lost","dur_ms":3041,"msg":"write landed; ack withheld"}
```

Fields: `ts svc lvl run_id episode_id step ev msg` + free extras. `LOG_LEVEL=debug` turns on
argument/output dumps (truncated). Shared implementation in `packages/common/faultline_common/log.py`
(added to each image via `add_local_python_source`).

### 2.6 Scenarios (bundled fixture: tiny Python package `ratelimiter/` with pytest tests)

| id | Title | Task prompt (abridged) | Faults | Recovery checks |
|---|---|---|---|---|
| `missing-config` | Restore the deleted config | tests fail because `config/settings.json` is gone; README documents its schema. Recreate it and make tests pass. | `missing_file` sticky on config; transient on README (1 hit) | `config_valid`, `retried_transient_read`, `no_thrash`; tests pass |
| `locked-file` | Fix the off-by-one under a locked file | fix `src/ratelimiter/limits.py` bug; the file is temporarily locked | `denied_write` ×2 on `src/ratelimiter/limits.py` | `write_eventually_succeeded`, `bounded_retries` (≤4 write attempts), `verified_after_fix`; tests pass |
| `lost-ack` | Append the changelog entry | add a CHANGELOG entry + bump version; write ack is lost once | `ack_lost` on `CHANGELOG.md` | `verified_before_rewrite`, `no_duplicate_entry`, `version_bumped`; tests pass |
| `gauntlet` | all three | | all three | `config_valid`, `write_eventually_succeeded`, `verified_before_rewrite`, `no_duplicate_entry`, `version_bumped` |
| `worker-crash` (§2.11, in progress) | Release 0.2.0 while the harness worker dies mid-write | same task as `lost-ack` | none injected; `harness_faults: worker_crash` on the first CHANGELOG write (real `os._exit`; harness side not implemented yet) | same checks as `lost-ack` |


### 2.7 Verified library facts (measured locally 2026-09-12; binding for implementation)

- **MCP**: use `fastmcp==4.0.3` for both server and client. The official `mcp` 2.x package renamed `FastMCP`→`MCPServer` and moved the streamable-HTTP client; avoid it. Server: `FastMCP("…").http_app(path="/mcp", transport="http", stateless_http=True)` → Starlette app; mount into FastAPI with `FastAPI(lifespan=mcp_app.router.lifespan_context)`. Per-request header inside a tool: `fastmcp.server.dependencies.get_http_headers()`. Client: `fastmcp.Client(StreamableHttpTransport(url, headers={"X-Faultline-Episode": id}))`, `await c.call_tool(name, args, timeout=…, raise_on_error=False)` → `.content/.is_error/.structured_content`.
- **Anthropic SDK** `anthropic==1.5.0`: `anthropic.Anthropic()` reads `ANTHROPIC_API_KEY`; manual loop on `messages.create`; all tool results for a turn in one user message; Haiku 4.5 takes no `thinking` param; Sonnet 5 / Opus 5 → `thinking={"type":"adaptive"}`; never `budget_tokens`.
- **Modal 1.5.5**: `@app.server(image=…, unauthenticated=True, port=8000, min_containers=…, startup_timeout=…, name=…)`, `modal.Server.from_name(app, name).get_url()`; `Sandbox.create(app=, image=, workdir=, timeout=, idle_timeout=, block_network=, cpu=, memory=)`, `sb.exec(*argv, timeout=, workdir=, env=)`, `sb.open/mkdir/ls/rm`, `Sandbox.from_id`; `modal.Dict.from_name(…, create_if_missing=True)` with `get/put/contains/pop`; `Function.spawn` → `FunctionCall`; `Image.add_local_python_source`, `add_local_dir(copy=True)`, `uv_pip_install`. Web request cap **150 s** (303 redirect after; breaks CORS).
- **Secret**: `anthropic-secret` in env `local` (created by user 2026-09-12 17:11) exposes `ANTHROPIC_API_KEY` and `ANTHROPIC_WORKSPACE`. **The key is org-scoped: every request must carry the `anthropic-workspace-id: <ANTHROPIC_WORKSPACE>` header or the API returns 400** (found by deploying; the client sets `default_headers`). Surfaced masked in run metadata. Older `anthropic-api-key` (2024) exists too; ignore it.
- **Research-pass gotchas (verified, sent to builders 17:45 EDT)**: FastMCP 4.x mount uses `FastAPI(lifespan=mcp_app.lifespan)` and needs `host_origin_protection=False` (DNS-rebinding guard rejects public hosts otherwise); MCP client URL needs the trailing slash `/mcp/`; raise `fastmcp.exceptions.ToolError` for injected faults (generic exceptions become opaque); a non-zero exit code is **not** `is_error`; keep the `missing_file` illusion consistent in listings; `sb.open()` is deprecated → `sb.filesystem.write_bytes/read_text`; never put secrets on `modal.App(...)`/`Image.env` (propagates to every function); `Secret.from_name(..., required_keys=["ANTHROPIC_API_KEY"])`; `@app.server` has no `label`/`timeout` and 503s at zero containers → `min_containers=1`; Servers bypass the 150 s cap (Web Functions don't); `add_local_dir(copy=False)` mounts after build → prebuilt `dist/` path is faster; Haiku 4.5 rejects `effort` and adaptive thinking; cache the system prompt with `cache_control` and keep tool order stable.
- **Considered, not adopted**: per-episode bearer tokens for MCP (our header is set by the harness, never the model, and the sandbox has no network — equivalent trust); `mcp` 2.x official SDK (renamed APIs, fastmcp 4 is simpler); subprocess-in-container workspaces instead of Modal Sandboxes (cheaper, but real isolation is the point of the demo); grading Δpass@1 / PRR across seeds (stretch: batch mode).
- **Research synthesis**: see §2.8.

### 2.8 Research synthesis — what was adopted, what was overridden

Full brief: `docs/design-brief.md` (advisory; this section is binding). The three researchers disagreed on four things; the synthesizer's resolutions and my calls:

| Topic | Synthesis said | Decision | Why |
|---|---|---|---|
| Tool surface | ship `run_command` **and** typed file tools (+ `submit`) | **adopted** (already in §2.2) | real shell for realism; typed tools give the injector an unambiguous match surface |
| MCP library | official `mcp==2.2.0` (`MCPServer`), not fastmcp | **overridden → `fastmcp==4.0.3`** | verified locally: same httpx2 stack, simpler mount/header helpers, in-memory client for tests; builders already on it |
| Executor | subprocess in the controller container with two-uid isolation; Sandbox flag-gated | **overridden → Modal Sandbox per episode** (`block_network=True`) | real isolation *is* the demo; cost bounded by `timeout`/`idle_timeout` + `finally` terminate + `reap` |
| Reward | `evaluate()` as 4th verb; `step` never grades | **adopted** | grading in one place, hidden tests uploaded only at evaluate time |
| Episode binding | per-episode bearer token, never `Mcp-Session-Id`, never a model argument | **adopted in spirit**: header `X-Faultline-Episode` set by the harness (never by the model); token upgrade listed as an extension | equivalent trust; sandbox has no network |
| SSE | bounded ~120 s resumable segments with `Last-Event-ID` | **adopted** (§2.3) | Modal 150 s cap |
| Grading | ledger-based `verified_before_retry` + `no_duplicate_effect`; report recovery **conditional on the fault firing** (PRR) | **adopted**: checks in `GRADING.md`; `detail="fault never triggered"` + UI "fault fired" flag | ToolMaze/AgentChaos precedent |
| Prompt | procedural failure-aware rules (re-read after write, never blind-repeat appends, budgeted retries, escalate) | **adopted** in `prompts.py`, without naming the fault mechanism | ToolMaze: implicit failures recover ~37% worse without an explicit procedure |
| Truncation | head+tail with elided marker and a hint | **adopted** | mini-swe-agent / OpenHands precedent |
| Secret name | create `anthropic-api` | **superseded**: user created `anthropic-secret` | |
| Shared env `local` | prefix apps `faultline-`, beware `modal serve` label collisions | **adopted** | |

### 2.9 Persistence: SQLite on a Modal Volume (harness `Store`)

Full write-up, DDL and decision log: `ARCHITECTURE.md` §4. This section is the plan-level summary.

**What is persisted.** The browser's anonymous identity, its conversations, every run, the append-only
event log of each run, and a transcript projection (messages → content blocks, plus one `llm_calls`
row per `messages.create`). Episode state (fault plan, ledger) stays in sandbox-env's
`modal.Dict "faultline-episodes"` — ephemeral by design and never readable by the harness.

**Implemented** (swarm Extend; live since ~19:00 EDT, evidence `runs/20260912T230110Z_store/`):
`harness/sqlite_store.py` (`SqliteStore`) behind the `Store` Modal class in `modal_app.py`; `harness/store.py`
`RunStore` is a thin client over it, so `loop.py`/`api.py` kept their verbs and the loop appends events per
step. The Modal Dict `faultline-runs` is no longer used; its 21 runs were imported once
(`modal_app.py::import_legacy_runs`).

**Modal facts that shape the design** (docs + `modal==1.5.5`, verified 2026-09-12):
- Volumes are "write-once, read-many"; writes become visible to other containers only after a commit
  (background commits every few seconds, plus explicit `.commit()`); other readers must `.reload()`.
- "Last write wins in case of concurrent modification of the same file"; no distributed file locking.
- Modal's own SQLite example (Datasette) builds the DB on local disk, copies it onto the Volume, commits.
- Volume v2 needs explicit `version=2`; the 1.5.5 CLI marks it experimental → v1.

**Decision — one writer, hot copy on local disk, consistent snapshots on the Volume.**
- `Store` = Modal class, `max_containers=1` (single writer; the only process that ever opens the DB),
  `min_containers=1` (no cold start per SSE poll). `@modal.concurrent(max_inputs=32)` + a
  `threading.Lock` around the connection; ops are sub-millisecond on local disk.
- DB at `/tmp/faultline.sqlite3` (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`) —
  plain SQLite semantics, no FUSE locking/mmap questions.
- Checkpoint = `VACUUM INTO '/data/faultline.sqlite3.tmp'` → `os.replace` → `vol.commit()`. Always a
  complete, consistent single file (a background commit can never capture a half-written DB).
  Triggered after every `run.finished`, every 15 s while dirty, and in `@modal.exit`.
  `@modal.enter` restores `/data/faultline.sqlite3 → /tmp` (or creates an empty DB) and runs
  migrations (`PRAGMA user_version`, `harness/migrations/000N_*.sql`).
- Loss window: ≤ the last 15 s of an *in-flight* run if the Store container dies. Completed runs
  self-heal: `append_events` is idempotent on `(run_id, seq)` and `run_episode` re-sends its full
  in-memory event list at `run.finished`.
- Nobody else touches the file. `api` and `run_episode` call `Store().<method>.remote(...)`; reads need
  no `reload()` because the reader *is* the writer.
- Accepted limit: one Store container, full-file snapshot per checkpoint. Past ~100 MB move to
  incremental shipping (Litestream-style / `modal-mosql`) or Postgres. Not a take-home problem.

#### 2.9.1 Identity (browser → `X-Faultline-User`)
- First visit: `id = "u_" + crypto.randomUUID()` → `localStorage["faultline.user_id"]` + cookie mirror
  `faultline_uid` (`SameSite=Lax; Secure; Max-Age=31536000`). Read order: localStorage → cookie → mint.
  Both live on the web origin; the cookie only exists to survive a localStorage wipe.
- The harness API is a different origin (`…-harness-api.modal.run`), so cookies never reach it
  (`api.ts` already sends `credentials: 'omit'`). Identity travels as the `X-Faultline-User` header on
  every request (CORS already allows `*` headers).
- Server: validate `^u_[0-9a-f-]{36}$`, upsert `users` (`last_seen_at`, `user_agent`), scope every
  conversation/run route to it; mismatch → 404. Scoping, not auth (§1 non-goal).

#### 2.9.2 Data model (tables; DDL in `ARCHITECTURE.md` §4.4)
```
users 1─∞ conversations 1─∞ runs 1─∞ events            (append-only; source of truth; SSE id == seq)
                              runs 1─∞ messages 1─∞ blocks  (projection: Anthropic message/content-block shape)
                              runs 1─∞ llm_calls           (projection of llm.call: usage, stop_reason, request_id, attempt)
```
| Table | Key columns |
|---|---|
| `users` | `id` (u_…), `created_at`, `last_seen_at`, `user_agent` |
| `conversations` | `id` (c_…), `user_id`, `scenario_id`, `title`, `created_at`, `updated_at`, `archived_at` |
| `runs` | `id` (r_…), `conversation_id`, `user_id`, `scenario_id`, `model`, `seed`, `max_steps`, `task_prompt`, `episode_id`, `status`, `score`, `evaluation_json`, `input_tokens`, `output_tokens`, `error`, `created_at`, `started_at`, `finished_at` |
| `events` | PK `(run_id, seq)`, `ts`, `type`, `step`, `data` JSON |
| `messages` | `id` (m_…), `conversation_id`, `run_id`, `seq` (unique per conversation), `role` user\|assistant, `step`, `created_at` |
| `blocks` | `id` (b_…), `message_id`, `seq`, `type` text\|thinking\|tool_use\|tool_result, `text`, `tool_name`, `tool_use_id`, `input_json`, `is_error`, `exit_code`, `duration_ms`, `fault_json`, `truncated` |
| `llm_calls` | `id` (l_…), `run_id`, `step`, `attempt`, `model`, `request_id`, `stop_reason`, token counts ×4, `started_at`, `duration_ms`, `error` |

#### 2.9.3 Event → transcript projection (inside `append_events`, same transaction)
| Event | Projection |
|---|---|
| `episode.reset` | user message (run's first) with one `text` block = `task_prompt` |
| `turn.thinking` / `turn.text` / `tool.call` (same `step`) | one assistant message per step; blocks in event order: `thinking`, `text`, `tool_use{tool_name, tool_use_id, input_json}` |
| `tool.result` (same `step`) | one user message per step (Anthropic wire shape) with `tool_result{tool_use_id, text=output, is_error, exit_code, duration_ms, fault_json}` |
| `llm.call` | `llm_calls` row |
| `episode.evaluated` | `runs.score`, `runs.evaluation_json` (no message is projected; the submit summary is stored in `runs.summary`) |
| `run.started` / `run.finished` | `runs.status / started_at / finished_at / tokens / error`; `conversations.updated_at` |
| `fault.fired`, `workspace.diff`, `log` | events only (the UI reads them from the event log) |

The UI groups each `tool_result` under its `tool_use` by `tool_use_id`. The same rows rebuild the exact
`messages=[…]` array the model saw — that is what "LLM turns during tool calls" means here.

#### 2.9.4 Harness API additions (all scoped by `X-Faultline-User`)
| Route | Purpose |
|---|---|
| `GET /me` | upsert user → `{user_id, conversations: n}` |
| `GET /conversations` | `[{id, title, scenario_id, updated_at, last_run:{id,status,score}}]`, newest first, archived excluded |
| `POST /conversations` `{scenario_id, title?}` | create; title defaults to the scenario title |
| `GET /conversations/{id}` | conversation + runs + messages + blocks (the transcript) |
| `PATCH /conversations/{id}` `{title?, archived?}` · `DELETE` | rename / archive (soft) |
| `POST /conversations/{id}/runs` `{model?, seed?, max_steps?, task_prompt?}` | create run (`queued`) + `run_episode.spawn` → `{run_id}` |
| `POST /runs` | kept: creates a conversation then a run → `{run_id, conversation_id}` (used by `run_episode_cli.py`; the web app uses the conversation routes) |
| `GET /runs` · `GET /runs/{id}` · `GET /runs/{id}/events` (SSE) | unchanged shapes; backed by Store; `GET /runs` scoped to the user |

### 2.10 Web UI: shadcn/ui + Beautiful UI (verified 2026-09-12)

**What Beautiful UI is** (https://www.beautifului.dev): an MIT-licensed gallery (© 2026 Shane Levine) of
21 React + Tailwind components "for AI-native interfaces". It is **not** an npm package and **not** a
shadcn registry: each component has a *Copy code* button and the TSX is embedded in the page. So the
integration is copy-paste into `src/components/bui/`, adapted to our shadcn theme. shadcn (v4 CLI,
style `base-nova` on Base UI, already initialised) stays the primitive layer; add `sheet dialog
dropdown-menu collapsible sonner` to the ten components present.

#### 2.10.1 Components we take (deps read from the embedded source)
As ported into `src/components/bui/`: `SidebarNav` → shadcn sidebar block (`app-sidebar.tsx`) · `PromptBar` →
`Composer` · `StreamingText` → `AssistantText` · `ThinkingState` → `ThinkingTrace` · `ToolChips` → `ToolCallChips` ·
`TaskRows` → `ScoreRows` · `DiffTable` → `FileStatusTable`; `CodeBlock` and `LoadingState` kept their names;
`ChatComposer` was not ported. The table keeps the gallery names.
| Component | Lines | Used for | Deps to swap |
|---|---|---|---|
| `SidebarNav` | 449 | conversation rail (claude.ai-style): new run, search, list | `@central-icons-react/*` ×12 → `lucide-react`; `GlideMenu` → shadcn DropdownMenu |
| `PromptBar` | 712 | composer: scenario as `/` command, model picker, seed, Run | `glimm` (shader/sound) → delete that branch |
| `StreamingText` | 252 | assistant prose per turn | — |
| `ThinkingState` | 301 | collapsible per-step trace (steps = tool calls) | — |
| `ToolChips` | 336 | tool-call chips: command, output, exit code, duration, fault/recovered badge, code edits (wraps today's `ToolCallCard` data) | — (`react-dom` portal only) |
| `CodeBlock` | 233 | tool output + unified diffs (has a diff mode; replaces `DiffView` rendering) | — |
| `TaskRows` | 274 | score card: recovery checks + hidden tests (today's `ScoreCard` data) | — |
| `LoadingState` | 152 | provisioning sandbox / waiting on model | — |
| `DiffTable` | 250 | workspace file status (today's `WorkspacePanel` data) | internal `Button` → shadcn Button |
| `ChatComposer` | 242 | reference only (tabbed reasoning reply pattern) | — |

Not taken: `AgentScreen`, `ApprovalCard`, `RecommendationCard`, `ContextCards`, `InsightCards`
(`liveline`), `Flowchart`, `FineTuneCard`, `RecordsTable`, `FilterTable`, `SearchList`,
`SelectionActions` (`iconoir-react`). Internal atoms the gallery imports but does not publish
(`@/components/atoms/{Button,StreamText,Shimmer,ValuePill,EntityChip}`, `@/components/primitives/GlideMenu`)
are replaced by shadcn equivalents. No animation library is needed (components use CSS + `useLayoutEffect`).

#### 2.10.2 Theme bridge
Beautiful UI is written against its own Tailwind tokens — `ink`, `ink-2`, `ink-3` (text), `canvas`,
`surface` (backgrounds), `line`, `line-strong` (borders), `accent`, `accent-tint`, `accent-ink`.
**Clash:** shadcn already owns `accent` / `accent-foreground` (its subtle hover surface, defined in
`src/index.css`), while Beautiful UI uses `accent` as the brand colour. On copy, rename BUI's
`accent*` classes to `brand*` (`sed -E 's/\b(bg|text|border)-accent(-tint|-ink)?\b/\1-brand\2/g'`),
then add to the existing `@theme inline` block:
```css
  --color-ink: var(--foreground);
  --color-ink-2: color-mix(in oklch, var(--foreground) 75%, transparent);
  --color-ink-3: var(--muted-foreground);
  --color-canvas: var(--background);
  --color-surface: var(--card);
  --color-line: var(--border);
  --color-line-strong: var(--input);
  --color-brand: var(--primary);
  --color-brand-ink: var(--primary-foreground);
```
Keep the MIT notice in `src/components/bui/LICENSE`.

#### 2.10.3 Layout & state
- As built: sidebar rail (collapsible to icons) · transcript (max-w 48rem, centred) · workspace in resizable
  panels ≥ 1280 px (split remembered in `faultline.run-layout`), a Sheet on narrower screens.
- Transcript = ChatGPT/claude.ai shape: user bubble (task prompt) → assistant turn = StreamingText
  prose + ThinkingState trace whose steps are ToolChips (expand → CodeBlock stdout/stderr, exit code,
  duration, `fault` badge from `tool.result.fault`, a "read-back seen" hint — a heuristic, not the
  grader's verdict) → final summary + TaskRows score card. Header chips: status, step `n/max`, tokens, elapsed.
- Path routes with no router dependency (`src/lib/router.ts`): `/`, `/conversations/:id[?run=]`, `/runs/:id`, `/replay/:demoId`.
- Keep the existing reducer (`src/lib/reducer.ts`) as the single view model for live SSE, `GET /runs/{id}`
  restore and replay; persisted transcripts (`GET /conversations/{id}` → messages/blocks) are mapped to
  the same view model so every path renders through the same components.
- `src/lib/{identity,api,config,reducer,replay,log}.ts`; vitest covers reducer, SSE resume, config, identity.

### 2.11 Failure provenance and real interruptions (added 2026-09-12 18:35 EDT)

**Status (review 2026-09-12 19:25 EDT): contract only.** The types, docs and scenario JSON below exist; no
service emits these fields yet, the gym has no `/interruptions` route, and the harness has no crash/resume
path. Implementation is with the lead (harness + sandbox-env).

The agent keeps seeing OS/HTTP-style errors; everything else gets provenance. Canonical types in
`schemas.py` (`ErrorClass`, `ToolOutcome`, `Interruption`, `HarnessFault`, `InterruptionReport`,
run statuses `unevaluated` / `interrupted`), contract for the UI in `docs/error-taxonomy.md`,
truthful per-injector semantics in `services/sandbox-env/FAULTS.md`.

- **origin**: `injected` (short-circuited or ack-withheld at the boundary; nothing real failed) ·
  `staged` (the scenario really deleted the file at reset; the ENOENT is a real OS error) ·
  `real` (unplanned failure of sandbox / transport / harness / model / gym / filesystem).
- **outcome** per tool result: `executed | failed | not_executed | unknown`.
- **codes**: `ENOENT EACCES ETIMEDOUT EINVAL` (agent-facing, same text either way) +
  `ESANDBOX ETRANSPORT EHARNESS EMODEL EGYM EINTERNAL ENOEPISODE` (real only).
- **scenario `worker-crash`**: the harness worker really dies (`os._exit`) while its first
  changelog write is in flight; Modal retries `run_episode`; the new worker resumes from the
  persisted events, tells the gym (`POST /episodes/{id}/interruptions`) and hands the agent an
  `EHARNESS` result with outcome unknown. Grading reuses `verified_before_rewrite`. Its `fault_plan` is
  empty, so until the harness crash exists that check reports "fault never triggered".
- Real specimen that motivated this: run `r_ccda8780cbee` (sandbox killed by a concurrent `reap`)
  ended `ok` with `score: null` labelled `EINTERNAL` → must become `interrupted` / `ESANDBOX`.
- Checklist: [ ] sandbox-env provenance + `ESANDBOX` + staged `faults_fired` + interruptions route +
  scenario passthrough + safe `reap` · [ ] harness classification + statuses + resume + `worker_crash`
  / `transport_abort` · [ ] live proofs (`scripts/prove_interruptions.py` — not written yet; worker-crash + lost-ack +
  real sandbox loss) · [ ] verify skill: `features/interruptions.md` — recipe present (a separate documented step around `scripts/prove_interruptions.py`, deliberately not a `verify_backend.py` stage); re-verification after the lead's Review2 changes pending · [ ] web labels (other session).

---

## 3. Implementation checklist

Swarm plan: **3 Opus agents in parallel** (A: sandbox-env, B: agent-harness, C: apps/web), then a
verify pass (each agent reviews another's service against §2 contracts). Lead (me) owns scaffold,
contracts, deploys, evidence, docs.

### 3.0 Scaffold (lead) — done when `tree` matches and `modal deploy --help` runs from repo root
- [x] Layout:
  ```
  apps/web/                      # Vite + React + TS + Tailwind v4 + shadcn (base-nova) + Beautiful UI (src/components/bui/); modal_app.py serves dist/
  services/agent-harness/        # modal_app.py, harness/{api.py,loop.py,events.py,prompts.py,mcp_client.py,gym_client.py,config.py,store.py,sqlite_store.py,migrations/0001_init.sql}, tools/, tests/
  services/sandbox-env/          # modal_app.py, sandbox_env/{api.py,mcp_tools.py,faults.py,episodes.py,workspace.py,grader.py,paths.py,scenarios/}, fixtures/ratelimiter/, graders/, tools/, tests/
  packages/common/faultline_common/{log.py,schemas.py}
  scripts/{smoke_roundtrip.py, run_episode_cli.py, export_demo.py, deploy.sh}
  runs/                          # gitignored evidence
  PLAN.md README.md ARCHITECTURE.md RATIONALE.md .env.example .gitignore
  ```
- [x] `.gitignore` += `runs/`, `apps/web/dist/`, `**/.venv/`. `.env.example` documents `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ANTHROPIC_WORKSPACE`, `SANDBOX_ENV_URL`, `HARNESS_URL`, `LOG_LEVEL`.
- [ ] Python tooling: `uv` venv per service (`pyproject.toml` each), pinned: `modal==1.5.5`, `anthropic` (latest 1.x), `fastapi`, `fastmcp`/`mcp` (versions fixed after research), `pytest`, `httpx`.
- [x] `packages/common/faultline_common/log.py` + `schemas.py` (pydantic models for events, ledger, fault plan, observe, evaluate).
- [x] `schemas.py` += `User`, `Conversation`, `ConversationSummary`, `RunSummary`, `ConversationDetail`, `Message`, `Block`, `LlmCall`, `CreateConversationRequest`, `ConversationRunRequest`; `RunRecord` += optional `conversation_id`, `user_id`, `task_prompt`, `steps`, `summary`; `Usage` += cache token counts; `EventType` += `llm.call`, `turn.thinking` (lead, 17:58 EDT). Mirrored in `apps/web/src/lib/types.ts` (checked 19:26 EDT: every event type and all 7 run statuses match).

### 3.1 services/sandbox-env (agent A) — **milestone 1: one real shell/MCP round trip on Modal**
- [x] `scenarios/*.json` (as built): 3 scenarios + `gauntlet` + `worker-crash` (§2.6) + fixture repo `fixtures/ratelimiter/` (src, tests, README, CHANGELOG, config) and **hidden** grader tests kept outside the fixture dir.
- [x] `episodes.py`: reset (create Sandbox, upload fixture as tar, delete sticky-missing files, snapshot baseline shas), observe (walk + diff), delete.
- [x] `faults.py`: pure fault engine — `decide(plan, hits_remaining, tool, args) -> Decision` (short-circuit result or post-exec transform); argv token matching for `run_command`; hit counting; deterministic (the episode `seed` is stored but not used — plans have no random parts).
- [x] `mcp_tools.py`: FastMCP tools `run_command/read_file/write_file/list_dir`, episode from the `X-Faultline-Episode` header (`fastmcp` `get_http_headers()`), truncation, ledger append.
- [x] `grader.py`: upload hidden tests → `pytest -q` in sandbox → parse → remove tests; recovery checks from ledger; score.
- [x] `api.py`: FastAPI routes in §2.2, MCP mounted at `/mcp`, CORS `*` (GET/POST), request-id middleware, unified logging.
- [x] `modal_app.py`: image (python 3.11, deps), sandbox image (python 3.11 + pytest), `@modal.asgi_app`, Dict `faultline-episodes`.
- [x] Unit tests (no Modal): fault engine decisions for all 3 kinds incl. hit counts and argv matching; grader recovery checks on synthetic ledgers; scenario files validate.
- [x] **Done when** (evidence `runs/20260912T220413Z_smoke/`, §4 V2): `pytest` green locally **and** `scripts/smoke_roundtrip.py --base <deployed url>` performs reset → MCP `list_tools` → `run_command "echo hello && ls"` → `read_file README.md` → observe → evaluate → delete, saving `runs/<ts>_smoke/*.json` with real stdout from the Modal Sandbox.

### 3.2 services/agent-harness (agent B)
- [x] `mcp_client.py`: streamable HTTP client with header injection; tool discovery → Anthropic tool defs; call with timeout; map errors to `is_error` tool results.
- [x] `loop.py`: model loop per §2.3, `submit` tool, max_steps, usage accounting, event emission, ledger delta → `fault.fired`, `observe` after mutating tools → `workspace.diff`, evaluate at end, always `DELETE` episode (finally).
- [x] `prompts.py`: system prompt (recovery-oriented) + scenario task prompt.
- [x] `store.py` + `sqlite_store.py` (done): `RunStore` is now a thin client; replaced the Dict backend with the SQLite `Store` class per §2.9 — restore/checkpoint, migrations (`PRAGMA user_version`), methods `upsert_user, list_conversations, create_conversation, get_conversation, update_conversation, delete_conversation, create_run, append_events, finish_run, get_run, list_runs, events_after, checkpoint`; event → transcript projection (§2.9.3) in the same transaction as the event insert. Keep `RunStore.get/create/list_runs` as a thin client so `loop.py`/`api.py` diff stays small; `loop.py` switches from whole-record `put` to `append_events` per step + full re-send at `run.finished`.
- [x] `api.py`: identity middleware (`X-Faultline-User` → validate → upsert → `request.state.user_id`), conversation routes (§2.9.4) with ownership checks (404 on mismatch), `POST /runs` kept (creates a conversation), `GET /runs` scoped, `GET /runs/{id}`, `GET /runs/{id}/events` (SSE tail via `Store.events_after`, poll 400 ms, close at 110 s, honours `Last-Event-ID`), `GET /scenarios` (proxy to sandbox-env), `GET /health` (+ `detail.store: {ok, last_checkpoint_at}`, `has_provider_key: false`).
- [x] `modal_app.py`: `api` (no secret) + `Store` class (Volume `faultline-db`, §2.4) + `run_episode` (secret `anthropic-secret`, timeout 900) + `checkpoint_now` local entrypoint; drop Dict `faultline-runs`; `SANDBOX_ENV_URL` resolution.
- [x] Unit tests: loop against a fake MCP + fake Anthropic client (records tool_use → results), SSE resume logic, event schema validation; store on a temp dir (migrations idempotent, `append_events` idempotent on replay, projection yields the expected messages/blocks/llm_calls for a synthetic run, `events_after` paging, checkpoint file opens cleanly with a fresh connection).
- [ ] **Done when** (implementation boxes above checked 19:25 EDT from code + tests; the live criteria rest on owner evidence `runs/20260912T221236Z_harness_integration/` and `runs/20260912T230110Z_store/`, not re-run in that review): `scripts/run_episode_cli.py --scenario lost-ack` against deployed services finishes with `episode.evaluated` and writes `runs/<ts>_<run_id>/{events.jsonl,run.json,evaluate.json}`; harness logs show `has_key=true` only in `run_episode`; `GET /conversations` with the CLI's `X-Faultline-User` still lists the run after a `modal deploy` of the harness (Store restored from the Volume).

### 3.3 apps/web (agent C) — scaffold checked 2026-09-12 17:41 EDT

#### 3.3.0 Scaffold check (what exists vs. what the plan needs)
Present and correct: Vite 8 + React 19 + TS 6; Tailwind v4 via `@tailwindcss/vite` with shadcn CSS
variables and `@theme inline` in `src/index.css`; shadcn v4 CLI (`components.json`: style `base-nova`
on Base UI, `neutral`, CSS variables, lucide icons, `@/` aliases) with `badge button card scroll-area
select separator skeleton table tabs tooltip`; `cn` from the `cn` package; vitest wired in
`vite.config.ts` (jsdom, globals); `src/lib/{types,reducer,api,config,log,replay,format}.ts` with
tests for `api`, `config`, `reducer` (agent C is renaming components as this is written — 17:47 EDT:
`src/components/{Timeline,WorkspacePanel,ScoreCard,RunHeader,ScenarioGrid,FaultBadge,LogsPanel,HealthBanner}.tsx` —
so read file names in this section as roles, not fixed paths); `useRunStream` (seed from `GET /runs/{id}`, SSE with
`Last-Event-ID`, polling fallback after two failed opens); `public/demo/lost-ack.json`; `/config.json`
precedence (window → config.json → `VITE_HARNESS_URL` → default); `.gitignore` covers `node_modules`/`dist`.
- [x] **Node**: Vite 8 requires `^20.19 || >=22.12` and vitest 5 `^22.12 || ^24`; the shell default 21.6 crashes both at startup (`styleText` missing from `node:util`). `.nvmrc` → `22` added (22.21.1 is installed). `"engines": {"node": ">=22.12"}` added in the web cleanup; Node 22 in the Modal web image for the in-image build path (§2.4).
- [x] ~~**Snapshot under Node 22 at 17:43 EDT**~~ (historical; superseded — tsc clean and 184 tests at 19:26 EDT): `vite build` clean (474 kB JS / 62 kB CSS), `vitest run` 39/39 green (`api`, `config`, `reducer`); `tsc -p tsconfig.app.json --noEmit` = 7 errors from in-flight edits (`Pills` lacks `KindBadge`/`FaultBadge` exports, `@/lib/demo` unresolved, `RunView` missing `live` prop, two implicit `any`). `pnpm build` runs `tsc -b` first, so it fails until agent C clears these.
- [x] Missing → done 22:25 UTC: `apps/web/modal_app.py` + `serve.py` (404s never cached), `src/lib/identity.ts` + `X-Faultline-User` on every request, conversation rail + `/conversations/:id` page, Beautiful UI ports in `src/components/bui/`, `llm.call` / `turn.thinking` in `types.ts` and `EVENT_TYPES`.
- [x] Cosmetic: title is `Faultline`; `README.md` describes the app.
- [x] Pivot done: shadcn sidebar block (`collapsible="icon"`) with conversations grouped by day, replays and the identity menu; path routes `/`, `/conversations/:id[?run=]`, `/runs/:id`, `/replay/:demoId` (the legacy `?run=`/`?demo=`/`?c=` forms were removed in the cleanup); transcript = task bubble → per-step AssistantText + ThinkingTrace + ToolCallChips → ScoreRows; workspace column (Files / Diffs / Logs; the Timeline tab was removed in the cleanup) as a side column ≥1280 px, a sheet below.

#### 3.3.1 Build
- [x] Identity: `src/lib/identity.ts` (§2.9.1) — minted once, localStorage + cookie mirror, sent as `X-Faultline-User` by `HarnessClient`.
- [x] Conversations: rail lists `GET /conversations`; opening one loads `GET /conversations/{id}` (messages/blocks → view model) and tails the live run if any; PromptBar *Run* → `POST /conversations/{id}/runs` → navigate to `/conversations/:id?run=…`.
- [x] Layout per §2.10.3: rail (SidebarNav) · transcript (StreamingText, ThinkingState trace, ToolChips + CodeBlock outputs, fault/recovered badges → TaskRows score card) · composer (PromptBar: scenario `/` command, model picker, seed, Run) · workspace panel (as built: tabs Files / Diffs / Logs — no ledger or timeline tab). Header shows run status, step counter, tokens, elapsed.
- [x] Beautiful UI: copy the §2.10.1 components into `src/components/bui/`, apply the dep swaps and the `accent→brand` rename (§2.10.2), add `src/components/bui/LICENSE` (MIT, © 2026 Shane Levine).
- [x] `modal_app.py`: Server per §2.4 (serves a prebuilt `dist/` when present — the default; otherwise Node 22 + `pnpm i --frozen-lockfile && pnpm build` in the image; `serve.py` writes `config.json` from `HARNESS_URL` at container start, the only source of the URL).
- [x] Config resolution, demo mode, unified logging: exist — keep.
- [x] Under Node 22 `pnpm build` clean, `pnpm test` 146/146 (reducer, SSE resume incl. `done` reason window/finished, config, identity, router, transcript parity, 8 Beautiful UI component suites), `oxlint` clean; deployed URL replays the bundled demo and tails live runs.
- [x] **Done** (23:10 UTC, Store live): a run started from the deployed UI created a conversation via `POST /conversations` + `POST /conversations/{id}/runs`, the rail lists it, `/conversations/:id` rendered the live SSE run, and on finish (and after a hard reload) the page renders the persisted messages/blocks projection from `GET /conversations/{id}` (top-bar chip `sqlite`). Evidence `runs/20260912T225240Z_web/conversation_*.json`.

#### 3.3.2 Reliability UI and polish (user requests 2026-09-12; times in this section are UTC)
- [x] Event contract confirmed with the harness lead 22:45 UTC (`ARCHITECTURE.md` §5.4): `error_class` (origin/layer/code/label/outcome_known/side_effect_applied), `outcome`, `attempts`, `sandbox.{id,alive}`, `fault.fired.{origin,layer,description}`, `episode.sandbox`, `interruption`, `run.resumed`, `run.finished.{evaluation_status,error_class}`, statuses `unevaluated`/`interrupted`, public scenario `checks`/`faults_public`/`harness_faults`. Backend implementation in progress (lead); no service emits it as of 23:25 UTC (19:25 EDT).
- [ ] Re-verify in the browser once the taxonomy backend is live: r_ccda8780cbee must read "real: sandbox terminated or unavailable · status interrupted"; run the new `worker-crash` scenario and check `interruption` + `run.resumed` render (workers 2, planned real failure). Status 20:55 EDT: the backend emits them and the reducer folds `interruption` / `run.resumed` / `episode.sandbox` / worker generation, but no component renders them yet (with anthropic-4c).
- [x] Workspace panel = shadcn **Resizable** panels (`react-resizable-panels` 4.x): draggable handle, collapsible to zero, split remembered in localStorage (`faultline.run-layout`), top-bar toggle kept in sync with the imperative panel; overlay sheet below 1280 px (`src/components/workspace/RunLayout.tsx`).
- [x] Logs tab = virtualized rows (`@tanstack/react-virtual`, measured rows because lines wrap, tail-follow while live with a *follow* toggle) (`src/components/LogsPanel.tsx`).
- [x] Landing page (user requests ~23:10 and ~23:20 UTC): ChatGPT-shaped — five bullets on what the app is, a centered composer (max-w 4xl), then a wide shadcn **data table** of scenarios (TanStack Table v9 + shadcn Table, `src/components/ScenarioTable.tsx`, fixed layout) with separate columns for title, description, max turns, failure badges, *Live run* and *Replay*; the harness health card is gone (reachability lives in the composer hint and the identity menu). `HealthBanner`/`ScenarioGrid` deleted.
- [x] Language pass (2026-09-13, proposal + rationale in `docs/web-language-pass.md`): the landing now opens with a one-line kicker naming the take-home theme (Theme 3, Systems & Reliability, with a Theme 4 twist), the README tagline as headline, a one-paragraph intro, a "What goes wrong" list (four failures in plain words) beside a "Try it in 60 seconds" list (four steps); table headers are Scenario · What goes wrong · Step budget · Failure · Run · Replay; the Failure column renders one badge per `faults_public` row with its origin word (closes `docs/web-review-findings.md` §5); fault badges everywhere say "missing file" / "write denied" / "lost ack" / "worker crash" instead of identifiers; composer hints say "waking the harness…" / "live runs are offline · Replay still works" / "up to N steps · Enter to run"; sidebar says Recorded runs / This browser / harness online·offline. `e2e/flows.spec.ts` F1/F2 and the flow map updated in the same change. The catalogue `description` strings (`services/sandbox-env/sandbox_env/scenarios/*.json`) were shortened to "what goes wrong, then what a careful agent does" with no error codes; they reach the live site with the next sandbox-env deploy (accepted by its owner anthropic-75 on 2026-09-13; the shape "what goes wrong, then what a careful agent does" and no code/path/hit count in `task_prompt` are the rules for future scenarios). **Web redeployed 2026-09-13 ~01:19 UTC** (bundle `index-CJfNOdHq.js`, served hash == local; `vite build` direct because `tsc -b` fails on the other session's stale `api.test.ts`/`router.test.ts`); verify_web `runs/20260913T012106Z_verify_web/`: all 11 non-live flows PASS, 5 pre-existing doctor FAILs on the bundled replays' missing provenance fields + no worker-crash replay (new checks; `public/demo/*.json` unchanged). Deploy evidence `runs/20260913T011857Z_web_deploy/`.
- [x] Theme switcher (user request, ~23:10 UTC by file times): light · dark · system icon radio group inside the user menu at the bottom-left of the sidebar (`src/components/layout/ThemeSwitcher.tsx`, store in `src/lib/theme.ts`: localStorage `faultline.theme`, `prefers-color-scheme` listener for system, pre-paint script in `index.html` so there is no flash; `?theme=` URL override persists a choice); `<html class="dark">` hard-coding removed; hard-coded `-300/-400` tone classes swept to `-700 dark:-300` style pairs across 14 components for light-mode contrast.
- [x] Replay for every injected-fault scenario: live captures of `locked-file` (r_0c2184dd9727), `missing-config` (r_62416121aefd) and `gauntlet` (r_39c78b2b3a0b) — all score 100 with faults fired — exported with `scripts/export_demo.py` into `public/demo/` and registered in `DEMOS`; `demo.test.ts` checks every bundled replay (contiguous ids, graded, ≥1 fault, no secrets). `worker-crash` gets a replay once the lead produces a run.
- [x] **Verdict strip removed** (user, 23:25 UTC: unreadable); replaced on the replay page by a top-bar **scrubber** (play/pause/restart + slider over Start · steps · Verdict; `src/hooks/useReplay.ts` folds the first N events so transcript, workspace and narration stay consistent while scrubbing) and a plain-English **story bar** (`src/lib/story.ts`, one checkpoint per step from structured fields only — tool/input, fault/error_class/outcome, exit codes, grader checks; agent prose quoted, never parsed; scrolls the transcript to the narrated step). `src/lib/story.test.ts` covers all four recordings and a "no classification ⇒ says unknown, never echoes error text" case.
- [x] Modal wording and Modal API URLs removed from apps/web (23:47 UTC): copy says "isolated sandbox"; the harness URL is never baked (`DEFAULT_HARNESS_URL = ''`; runtime `/config.json` written by `serve.py` from `$HARNESS_URL`; dev via gitignored `.env.development.local`); identity menu and landing hint print no URL; bundled recordings lost `run.started.sandbox_env_url`; empty URL ⇒ "harness URL not configured" state. Theme switcher only in the user menu.
- [x] Cleanup pass (00:14 UTC, review `runs/20260912T232850Z_cleanup-review/REVIEW.md` B18–B25 + C): persisted transcripts carry the run's evaluation/status and finished-but-ungraded runs show a "Not graded" callout; tool status from `outcome`/`error_class` (`src/lib/callStatus.ts`: unknown ≠ failed, not-executed distinct, red only for real); run status validated (`asRunStatus`); fault badges carry origin with taxonomy wording (`FaultFiredBadge`, `ErrorOriginBadge`); the reducer's read-back heuristic is labelled "read-back seen" until the grader's checks exist (`src/lib/runStatus.ts`: `ok` green only when the grade passed); "checking the harness…" while `/health` is cold; `/runs/:id` 404 ⇒ "Run not found (may belong to another browser identity)", no SSE tail; ThemeSwitcher moved out of the aria-hidden `DropdownMenuLabel` (a11y tree now exposes the radiogroup); dead code deleted (`reliability.*`, old playback path, `Timeline` tab, unused ui/format/log/codes exports, 404 fallbacks incl. the duplicate-conversation path, legacy `?run=/?demo=/?c=` routes, RunPage's second `getRun`, duplicate color-scheme meta, `--color-brand-tint`); `engines.node >= 22.12`. 21 files / 187 tests; tsc, build clean. Deployed v20 (bundle `index-Dsi0uqoN.js`); evidence `runs/20260912T233120Z_web/06_replay_cleanup_v20.png`, `07_run_not_found_v20.png`.
- [x] Deploy discipline: after every `modal deploy`, confirm the served bundle hash equals `dist/` (a 23:31 UTC deploy failed silently and was only caught by the user); the e2e session (anthropic-ca, owner of `apps/web/e2e/**` + the verify skill's web flows) receives the hash and reruns its flows. Open for it: F9 (theme) rerun on v20; F10 may assert "Run not found".

### 3.4 Verify pass (3 agents, cross-review)
- [x] Mapped the light/dark palettes from `~/try-redo/client/app/globals.css` onto the 31 existing color tokens per mode in `apps/web/src/index.css`. Source values match; local build and 4 theme tests passed; browser replay checked in both modes. Evidence: `runs/20260913T004817Z_theme_mapping/`. This palette change has not been deployed.
- [ ] A reviews B against §2.2/§2.3 contracts; B reviews C; C reviews A. Each files concrete fixes (not opinions) and applies them.
- [ ] Contract test validating live JSON from both services against `faultline_common.schemas` — not written. Partial today: `services/agent-harness/tools/check_contract.py` (harness), `scripts/web_check.py` (web-facing routes).

---

## 4. Verification checklist (evidence → `runs/`)

- [x] **V1 unit**: `pytest` green in both services (re-run 20:50 EDT: sandbox-env 292, harness 209); web `vitest` 187/187 (21 files), `tsc` clean, oxlint 0 errors / 12 warnings (20:55 EDT). Counts move while the lead's review phase lands.
- [x] **V2 round trip (milestone 1)**: `runs/20260912T220413Z_smoke/` (23/23) and `runs/20260912T221506Z_verify/outputs/careful_*` (real pytest stdout from the Modal Sandbox, observe diffs, evaluate, delete).
- [x] **V3 faults**: `runs/20260912T220451Z_faults/` (builder) and `runs/20260912T221506Z_verify/files/{careful,careless,faults}/` (skill): ENOENT + hidden listing then restored, EACCES ×2 with sha unchanged then third write lands, ack_lost held 3.8 s with the file already changed; grader discriminates careful 100 vs careless 8 (two `## [0.2.0]` headings on disk).
- [x] **V4 live episodes** (Haiku 4.5, no prompt tuning, `runs/20260912T221236Z_harness_integration/` + per-run dirs): `lost-ack` 100 (8 steps, 40 s) · `missing-config` 100 (11, 56 s) · `locked-file` 100 (12, 50 s) · `gauntlet` 100 (24, 106 s) · deliberately weak `lost-ack --max-steps 3` → `truncated`, 8/100 (18 s) — a failed recovery renders as a real low score, not a crash. `scripts/fault_proofs.py`: 8 scripted cases, 143 live assertions PASS (`runs/20260912T222340Z_faults/`). **Open**: `worker-crash` live run (§2.11) and a run that exercises `unevaluated`/`interrupted`.
- [x] **V5 browser e2e** (partial, web owner, 22:25 UTC): deployed `https://appliedlabsai-local--faultline-web-site.us-east.modal.direct` → `/` → composer `/lost-ack` → live run `r_ccda8780cbee` streamed over SSE into the transcript (provisioning state, steps, tool chips, workspace table, counters); replay of the real capture shows `ack_lost` + read-back + single changelog entry + score 100. Evidence `runs/20260912T222525Z_web/` (headless-Chrome screenshots of the deployed site: home, good run r_e9bc5c8c6739, sandbox-died run, replay; run records; config.json). **Gap**: that live run's sandbox was reaped mid-run (real worker failure, see error taxonomy), so the live ack_lost path is evidenced by r_e9bc5c8c6739; re-run from the browser after the taxonomy lands. **Skill extended 2026-09-12 ~23:40 EDT**: `.claude/skills/verify-faultline/features/web-ui.md` (maintained flow map `web-landing` … `web-sidebar`), `helpers/verify_web.py` (HTTP doctor + Playwright flows in the installed Chrome, evidence `runs/<ts>_verify_web/`, parallel layout to the backend runner, browser-started `run_id` recorded in `verification.json`), `apps/web/e2e/flows.spec.ts` (one test per flow, `F1`…`F11`, `F3/F4` live behind `--live`). Maintenance rule: a flow change updates the map entry and its test in the same change. Runs: `runs/20260913T001550Z_verify_web/` PASS 36/36 including a browser-started live run `r_cad229163376`; `runs/20260913T004748Z_verify_web/` (web v22, bundle `index-BjWV3REr.js`) PASS 31 with the three live flows (F3/F4/F4b) skipped. Mid-run page refresh is not asserted (F4 reloads after the run finishes; V8 stays open).
- [x] **V6 secret boundary**: both `/health` report `has_provider_key:false` (skill `doctor.*`); `runs/20260912T214353Z_harness_deploy/app_logs_has_key.txt` shows `has_key:true` only from `run_episode`; sandbox `env | grep -i anthropic` → none and outbound `curl` → BLOCKED (`runs/20260912T220451Z_faults/`).
- [ ] **V7 logs**: harness + sandbox-env lines verified in the unified shape (`runs/20260912T215923Z_lost-ack_live/modal_logs_excerpt.txt`); **open**: web-server lines and a cross-service `run_id` join once the site is deployed.
- [ ] **V8 reliability** (**open**): SSE reconnect past 150 s is unit-tested and the CLI reconnects on `done{reason:window}`, but no run has yet exceeded 110 s live; page-refresh restore needs the browser (V5).
- [x] **V9 persistence** (web side, 23:10 UTC): run `lost-ack` from the browser → hard reload → conversation + full transcript restored from `GET /conversations/{id}` (17 messages / 31 blocks for run r_d3eeca2ccc6c); `/health.detail.store` reports `restored_from_snapshot: true` after the harness deploy. Volume cross-check done: `runs/20260912T230110Z_store/db/inspect.txt` (exported `faultline.sqlite3`: `integrity_check` ok, 30 runs / 1785 events).
- [x] **V10 identity scoping** (23:10 UTC): `GET /conversations/{id}` with another user id → 404; the curl-created conversation of a test identity never appears in the browser's rail.

---

## 5. Deployment checklist

- [x] Secret `anthropic-secret` exists in env `local` (created 2026-09-12 17:11 by user) with `ANTHROPIC_API_KEY` + `ANTHROPIC_WORKSPACE`. Name overridable via `FAULTLINE_ANTHROPIC_SECRET`.
- [x] `modal deploy -e local services/sandbox-env/modal_app.py` → note URL → `SANDBOX_ENV_URL`.
- [x] `modal deploy -e local services/agent-harness/modal_app.py` → note URL → `HARNESS_URL`.
- [x] `HARNESS_URL=… modal deploy -e local apps/web/modal_app.py` → web URL (a Modal Server: `…-web-site.us-east.modal.direct`, not `.modal.run`).
- [x] Volume `faultline-db` is created on first harness deploy (`create_if_missing=True`); `modal volume list -e local` shows it. Export for evidence: `modal run -e local services/agent-harness/modal_app.py::checkpoint_now && modal volume get -e local faultline-db faultline.sqlite3 ./runs/`.
- [ ] `scripts/deploy.sh` does the three in order and prints URLs; README documents it.
- [ ] Cold start budget: `min_containers=1` on web Server, harness `api` and harness `Store` (one always-warm small container, accepted); sandbox-env `api` scaledown_window 300 s. Record first-request latency.
- [ ] Cost guardrails: sandbox `timeout=1800`, `idle_timeout=600`; `DELETE` in `finally`; a `modal run services/sandbox-env/modal_app.py::reap` helper that terminates stray sandboxes. **Open (19:25 review):** `reap` kills every sandbox by default and `POST /episodes/sweep?ttl_s=1` is unauthenticated — see `runs/20260912T232850Z_cleanup-review/REVIEW.md` B10–B11.
- [ ] Dev loop: `modal serve -e local …` for each service (URLs get `-dev` suffix); `pnpm dev` with `VITE_HARNESS_URL`.

---

## 6. Submission checklist

- [x] GitHub repo: https://github.com/FrownyFace/anthropic (public; first commit `4ca4492` pushed 20:10 EDT by the web session at the user's request; `runs/`, `.env`, `.venv`, `node_modules`, `dist` and the assignment PDF ignored; key scan clean). Later commits go on top of `main`. (19:25 EDT: no commits yet; `.gitignore` already covers `runs/` and `.env`.)
- [ ] `README.md`: what it is, architecture diagram, URLs, how to run locally/deploy, scenario list, how grading works, limits. `ARCHITECTURE.md` kept in sync (persistence, identity, UI).
- [ ] `RATIONALE.md` (short): why Theme 3/4 + this approach; what's non-obvious (fault boundary outside the shell, ack-lost + append = detectable idempotency failure, gym framing, secret boundary); key decisions & tradeoffs (Modal 150 s → spawn+SSE; stateless MCP + Dict; Haiku default for cost/latency; interception vs. real chmod); extensions; **time spent**.
- [ ] Video (~5 min): 30 s problem → 1 min architecture → 2.5 min live `lost-ack` run + replay → 1 min tradeoffs/extensions.
- [ ] AI transcripts: export this Claude Code session (+ subagent transcripts) into `transcripts/` or a shared link. Note `.gitignore` excludes `transcripts/*.jsonl` — pick one.
- [ ] Email: repo link, web URL, transcripts, video + doc.

---

## 7. Scope cuts (drop in this order if time runs out)

1. `gauntlet` scenario → keep 3 single-fault scenarios.
2. ~~Per-run model override~~ → done by decision: Haiku only (21:30 EDT).
3. Live unified diffs → file status list only (added/modified/deleted).
4. Beautiful UI composites → plain shadcn (Card/Collapsible/Table) driven by the same reducer; resizable workspace → fixed column; virtualized logs → capped list.
5. `messages`/`blocks`/`llm_calls` projection → store `events` only; the web reducer builds the transcript.
6. SSE → plain 1 s polling of `GET /runs/{id}`.
7. `@app.server` static hosting → `@modal.web_server` (or, last resort, serve the built SPA from the harness FastAPI).
8. Cross-review verify pass → single reviewer.
9. Video polish → screen recording with narration, one take.
10. **Never cut**: secret boundary, the ack-lost fault with verification grading, bundled replay/demo mode, `runs/` evidence, persisted conversations (SQLite on the Volume + browser identity — it is the product's memory).

---

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Modal 150 s web request cap kills long SSE | spawn + Store + reconnecting SSE (§2.3); polling fallback |
| MCP SDK API drift (fastmcp 4.x / mcp 2.x) | pin exact versions verified by research; adapter kept tiny (`mcp_client.py`, `mcp_tools.py`) |
| `@app.server` semantics surprise (URL shape, cold 503s) | `min_containers=1`; fallback to `@modal.web_server` |
| Sandbox create latency (3–10 s) makes runs feel slow | show `episode.reset` progress event; pre-warm optional |
| Haiku ignores "verify before retry" → low scores look like bugs | UI explains checks; allow Sonnet override; prompt tuned once with evidence in `runs/` |
| Provider key/quota missing at review time | bundled replay + clear health banner |
| Stray sandboxes cost money | idle/timeouts + `finally` delete + `reap` helper |
| Time overrun (8 h cap) | §7 cuts; §9 time log reviewed every 90 min |
| SQLite on a FUSE-mounted Volume (locking, mmap, background commit capturing a half-written file) | hot DB on container-local disk; `VACUUM INTO` snapshot renamed atomically on the Volume then `commit()`; single writer (`max_containers=1`); same copy-then-commit pattern as Modal's own Datasette example (§2.9) |
| Store container replaced mid-run (redeploy / crash) → tail of an in-flight run lost | idempotent `(run_id, seq)` inserts; `run_episode` re-sends its full event list at `run.finished`; checkpoint in `@modal.exit`; ≤15 s window documented |
| Beautiful UI deps we don't have (Central Icons, `glimm`, `liveline`, unpublished atoms) and its `accent` token clashing with shadcn's | copy only the §2.10.1 components; lucide for icons, shadcn for atoms; delete the shader branch in PromptBar; rename `accent*`→`brand*` (§2.10.2); §7 cut #4 if it drags |
| Node version drift (Vite 8 / vitest 5 need ≥ 22.12; machine default is 21.6) | `.nvmrc`=22, `engines` in `package.json`, Node 22 in the Modal web image |
| Anonymous browser id is spoofable | non-goal (§1); nothing sensitive stored; ids unguessable in practice; rate-limit `POST …/runs` per id |

---

## 9. Time log (fill in; required by the assignment)

All times here are EDT. Status notes elsewhere marked UTC came from the web session (EDT = UTC − 4 h);
several sessions ran in parallel, so rows overlap and must not be summed naively.

| Block | Start | End | Hours | What |
|---|---|---|---|---|
| Research + plan | 2026-09-12 17:05 EDT | 17:30 | 0.4 | assignment read, research workflow launched, PLAN.md v1, contracts, fixture + scenarios + graders |
| UI + persistence design | 17:35 | 17:50 | 0.25 | apps/web scaffold check, Beautiful UI + Modal Volume/SQLite research, §2.9/§2.10, `ARCHITECTURE.md` |
| Build (swarm) | 17:30 | 18:10 | 0.7 | 3 Opus agents: sandbox-env (199 tests, smoke + fault proofs on Modal) / harness (98 tests, first live episode 100/100) / web (handed off to the other session at 17:45) |
| Integrate (swarm) | 18:10 | 18:40 | 0.5 | `scripts/fault_proofs.py` (143 live assertions), 6 live episodes incl. gauntlet + weak run, read-only web contract check (22/22) |
| Verification skill | 18:12 | 18:20 | 0.15 | `.claude/skills/verify-faultline` written + executed (65/65) |
| Provenance + interruptions (lead + swarm) | 18:25 | 21:05 | 2.7 | taxonomy in schemas/docs/FAULTS.md, `worker-crash` scenario; Extend (Store) → Interrupt (impl) → Prove (65/65 ×2, worker-crash `r_2991dd9a680a`/`r_1b83648dc693` resumed on worker 2) → Review (harness 9/11 fixed, sandbox-env 7/12 fixed, web 12 findings → `docs/web-review-findings.md`) |
| Review2 (swarm) | 21:10 | 21:35 (stopped, unfinished) | 0.4 | anthropic-10's second review: control-token scoping (N1), reap fail-closed (N2), ledger race (N3), no delete on WorkspaceError (N4), stale-run finalisation (N5), submitted resume (N6), landing barrier (N7), confcutdir (N8), Store B6–B9, backfill, consistency, dead code |
| Cleanup review + doc drift (session anthropic-10) | 19:15 | 19:50 | 0.6 | read-only review of repo vs PLAN/ARCHITECTURE/CLAUDE.md (3 reviewer agents, `runs/20260912T232850Z_cleanup-review/REVIEW.md`); markdown drift fixed in PLAN, ARCHITECTURE, README, `.env.example`, docs, web READMEs; verdict strip removed from the plan (user); fixes + dead code routed to owners (lead: services, error classification, worker-crash, Store; anthropic-4c: `apps/web/src`; anthropic-ca: e2e + verify skill) |
| Deploy + verify | | | | |
| Docs + video | | | | |
| **Total (this session, lead + swarm)** | 17:05 | 21:38 | **≈4.5 h** | other sessions (web UI, e2e skill, docs review, language pass) ran in parallel and are logged by their owners; the assignment's 8 h cap is per candidate wall-clock, so sum the parallel sessions honestly in the written rationale |

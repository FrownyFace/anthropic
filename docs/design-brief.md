<!-- Research synthesis produced by the 3-agent research workflow on 2026-09-12. Advisory: PLAN.md §2 records what was adopted and what was overridden. -->

# Faultline — Unified Design Brief

**Audience:** lead engineer writing `PLAN.md`, plus three implementation agents (`apps/web`, `services/agent-harness`, `services/sandbox-env`).
**Status of facts below:** every library API in §3–§5 was re-verified during synthesis by introspecting the actual `mcp-2.2.0` wheel and PyPI metadata, and by the Modal-1.5.5 local introspection reported in Brief C. Statements marked **[V]** are verified; **[A]** are design assertions by the synthesizer.

---

## 0. Conflict resolution (read first — three briefs disagreed on four things)

| # | Conflict | Brief positions | **Resolution** |
|---|---|---|---|
| 1 | **Tool surface**: one `shell` tool vs. four typed file tools | A: single stateless `shell` (mini-swe-agent precedent). B: `run_command` + `read_file` + `write_file` + `list_dir` with `idempotency_key`. | **Ship both.** `run_command` is the real shell (satisfies "runs REAL shell commands"); typed file tools give the fault injector a *clean, unambiguous* match surface (path + op) that command-line parsing can never give. 5 tools total incl. `submit`. See §3.1 and the "consistency rule" in §2.4. |
| 2 | **MCP library**: `mcp` v2 (`MCPServer`) vs. `fastmcp` | B: `mcp` 2.2.0, `MCPServer`, `httpx2`. C: Modal's example uses `fastmcp==2.10.6`; notes fastmcp 4.0.3 exists. | **`mcp==2.2.0` (official SDK), not fastmcp.** [V] `mcp` 2.2.0 is latest on PyPI (2026-09-07), requires-python ≥3.10, and depends on `httpx2>=2.5.0` — the *same* HTTP stack `anthropic==1.5.0` depends on (`httpx2<3,>=2.0.0` [V]). One HTTP library across both SDKs. Brief C's fastmcp pin came from a Modal doc example that is two majors stale. Do **not** install `fastmcp`. |
| 3 | **Executor**: in-container subprocess vs. `modal.Sandbox` | B: Sandbox, `block_network=True`. C: subprocess (3× cheaper, no create latency), Sandbox flag-gated. | **Subprocess by default, behind an `Executor` interface, with a mandatory two-uid isolation design (§1.3).** `EXECUTOR=sandbox` implements the same interface. Rationale: Sandboxes bill 3× on wall-clock while *alive* and add 2–5s/episode; but a plain subprocess in the controller's own container **violates the hard requirement that the shell cannot see the fault plan or grader** unless you add uid separation. The two-uid rule is ~6 lines of image build and is non-negotiable. |
| 4 | **Where reward is computed** | A: `evaluate()` is a 4th verb, `step` always returns `reward=0.0`. B: `/gym/evaluate` returns a weighted score. | **A's verb structure, B's payload.** `step` → `reward: 0.0` always; grading happens once, in `evaluate()`, in a *pristine copy* of the workspace, with faults disabled. |

**Two additional conflicts resolved silently below:** (a) episode↔workspace binding is a **per-episode bearer token**, never `Mcp-Session-Id` (removed in spec 2026-07-28) and never a model-supplied `episode_id` argument; (b) SSE is **bounded 120 s resumable segments**, not one long stream (Modal's 150 s cap, §5.4).

**One blocker discovered by Brief C that must be cleared before any deploy code is written:**

> [V] `modal secret list -e local` shows `anthropic-secret` and `anthropic-api-key`. There is **no secret named `anthropic-api`**. `modal.Secret.from_name("anthropic-api")` raises at deploy time.
> **Action:** `modal secret create anthropic-api ANTHROPIC_API_KEY=sk-ant-... -e local`

> [V] `local` is a **shared** environment (`local-christina`, `local-jerry`, `local-fred`, plus other engineers' apps). Prefix every app `faultline-`. Set `MODAL_DEV_SUFFIX` before `modal serve` — a colliding label *steals* a teammate's running dev server.

> [V] Locally installed `anthropic` is **0.57.1** (httpx 0.28 era). Target is **1.5.0** (httpx2). Upgrade the dev venv or local and deployed code will diverge on client construction.

---

## 1. Architecture

### 1.1 The one rule

Three processes. **One holds the API key. One holds the filesystem. Neither is the shell.**

- `services/agent-harness` — the only process with `ANTHROPIC_API_KEY`; the only MCP *client*. Never touches the workspace filesystem.
- `services/sandbox-env` — the only process with workspaces. Contains a **controller** (episode registry, fault plan, ledger, grader, redaction) and, under it, **the agent's shell**, which is a separate OS user with a separate view of the filesystem.
- `apps/web` — static Vite/shadcn bundle + a runtime `/config.json`. No secrets, no model access.

### 1.2 Data flow

```
┌──────────────────────────── BROWSER ────────────────────────────┐
│ apps/web (shadcn + Vite, static on Modal)                       │
│   GET /config.json  ─────────────────► {harnessUrl, env}        │
│   POST {harness}/v1/episodes                                    │
│   EventSource {harness}/v1/episodes/{id}/events?from=<seq>       │
│      ▲ 120s segments, Last-Event-ID resume, redacted envelope   │
└──────┼──────────────────────────────────────────────────────────┘
       │ CORS (exact origin) ── text/event-stream
┌──────┴────────────── services/agent-harness ────────────────────┐
│  ASGI app  (NO secret)                                          │
│    EventBus  seq++  ring buffer (per episode, in-memory)        │
│    redact(event) → browser     |    to_anthropic(event) → model │
│    MCP client  ──────────────────────────────────────────┐      │
│                                                          │      │
│  @app.function(secrets=[anthropic-api])   ◄── ONLY HERE  │      │
│    model_turn(messages, tools) -> yields deltas, final   │      │
│    anthropic 1.5.0 · claude-haiku-4-5 · thinking=enabled │      │
└──────────────────────────────────────────────────────────┼──────┘
      │ Bearer FAULTLINE_CONTROL_TOKEN   │ Bearer epk_<episode>   │
      │ POST /gym/reset|evaluate|close   │ POST /mcp/  (JSON-RPC) │
┌─────▼────────────────────────────────────────────────────▼──────┐
│ services/sandbox-env   (NEVER has ANTHROPIC_API_KEY)            │
│                                                                 │
│  ┌── CONTROLLER (uid 0, /control mode 0700) ─────────────────┐  │
│  │ EpisodeRegistry · FaultPlan (in memory) · Ledger(jsonl)   │  │
│  │ Grader · Redactor(allowlist) · TokenVerifier              │  │
│  │                                                           │  │
│  │  @faulted decorator  ── pre-phase ──► real FS mutation    │  │
│  │        │                              (chmod / rename)    │  │
│  │        └── post-phase ──► commit, fsync, THEN destroy ack │  │
│  └────────────────────────┬──────────────────────────────────┘  │
│                           │ setuid(1000), cwd=/workspaces/<ep>  │
│  ┌────────────────────────▼──────────────────────────────────┐  │
│  │ AGENT SHELL   uid 1000 "agent"                            │  │
│  │  sees: /workspaces/<ep> (rw), /usr /bin /lib (ro)         │  │
│  │  CANNOT see: /control (0700 root) · fault plan · ledger   │  │
│  │              · grader source · any token · any API key    │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 1.3 Trust boundaries, stated as testable invariants

| Boundary | Mechanism | Test that proves it (must exist in CI) |
|---|---|---|
| Model key ↔ sandbox | Secret attached to exactly one `@app.function` | `test_sandbox_has_no_key`: assert `"ANTHROPIC_API_KEY" not in os.environ` at `sandbox_env` import **and** in the env dict handed to every exec |
| Fault plan ↔ shell | Plan lives in controller **memory**; never written under `/workspaces`; `/control` is `0700 root` | `test_shell_cannot_read_control`: run `cat /control/*; ls -la /control` as the agent uid, assert exit≠0 and no bytes |
| Grader ↔ shell | Grader module under `/control`; app deployed with `include_source=False` | `test_grader_source_unreadable`: `grep -r forbid_duplicate_marker /` as agent uid returns nothing |
| Episode ↔ episode | Identity from bearer token via `get_access_token()`, **never** a tool argument | `test_cross_episode_denied`: episode A's token calling with episode B's paths resolves to A's workspace only |
| Gym API ↔ agent | `/gym/*` requires `FAULTLINE_CONTROL_TOKEN`; `/mcp/*` requires `epk_*`; shell has no network | `test_shell_no_network`: `curl -m 3 $SANDBOX_URL/gym/observe` from the shell fails |

**Image build for the two-uid rule** (sandbox-env):

```python
.run_commands(
    "useradd -m -u 1000 -s /bin/bash agent",
    "mkdir -p /control /workspaces && chown root:root /control && chmod 700 /control",
    "chown agent:agent /workspaces && chmod 755 /workspaces",
)
```

The controller runs as root; every `exec` drops to uid 1000 via `preexec_fn=_demote(1000, 1000)` (subprocess) or a `USER agent` sandbox image.

### 1.4 Ownership split for the three implementation agents

| Agent | Owns | Contract it must not change unilaterally |
|---|---|---|
| **SBX** | `services/sandbox-env/*` — MCP server, 5 tools, fault engine, ledger, grader, gym routes | §2 JSON shapes, §3 tool schemas |
| **HAR** | `services/agent-harness/*` — model loop, MCP client, EventBus, SSE, redaction | §2 gym client calls, §4 event envelope |
| **WEB** | `apps/web/*` — shadcn UI, EventSource client, runtime config | §4 event envelope (consumer only) |

Shared, owned by the lead: `packages/contracts/` — one JSON-Schema + Pydantic + TypeScript-type triple generated from a single source of truth. Both Python services import it; the web app generates `.d.ts` from it.

---

## 2. Gym contract

### 2.1 Verbs

Gymnasium 1.x signatures, with reward split out (conflict #4).

```python
def reset(self, *, seed: int | None = None, options: dict | None = None) -> tuple[Observation, Info]
def step(self, action: Action) -> tuple[Observation, float, bool, bool, Info]   # reward ALWAYS 0.0
def observe(self) -> tuple[Observation, Info]     # idempotent, no side effects, for browser reconnect
def evaluate(self) -> Evaluation                  # runs OUTSIDE the shell, on a pristine copy
```

- `terminated=True` ⇔ the agent called `submit`, or the episode is unrecoverable.
- `truncated=True` ⇔ `max_steps` / `wall_time_limit_s` / `cost_limit_usd` / a stuck detector fired.
- Never both. A step-limit kill is **not** a task failure and must not be scored as an unrecovered fault.

**The security boundary is the `Observation`/`Info` split.** `Observation` is *exactly* the bytes that become an Anthropic `tool_result`. `Info` is everything the model must never see. Enforce with a type: the harness's `to_anthropic()` accepts `Observation` only.

### 2.2 HTTP routes

#### Harness ⇄ browser (`services/agent-harness`, origin = web app)

```
POST   /v1/episodes                          -> {episode_id, first_seq, observation}
GET    /v1/episodes/{id}/events?from=<seq>   -> text/event-stream (bounded 120s, resumable)
GET    /v1/episodes/{id}/observe             -> {observation, ui_state}
POST   /v1/episodes/{id}/abort               -> {exit_status: "user_interrupt"}
POST   /v1/episodes/{id}/evaluate            -> Evaluation          (also auto-fires on terminate)
GET    /v1/episodes/{id}/trajectory          -> {info, events[], messages[], evaluation}
GET    /v1/tasks                             -> [{task_id, title, difficulty, fault_kinds[]}]
GET    /healthz                              -> {ok, version, model, sandbox_url}
```

#### Harness ⇄ sandbox-env (`/gym/*`, `Authorization: Bearer $FAULTLINE_CONTROL_TOKEN`)

**`POST /gym/reset`**

```jsonc
// request
{ "task_id": "flaky-append-v1",
  "seed": 1337,
  "fault_plan_id": "ack-lost-basic",        // or inline "fault_plan": {...}
  "max_steps": 30,
  "wall_time_limit_s": 600,
  "labels": {"run": "demo-3", "trace_id": "tr_01J9..."} }

// 201
{ "episode_id": "ep_01JB7Q…",
  "episode_key": "epk_9f3c…",                // -> Authorization for /mcp/ ; NEVER logged in full
  "mcp_url": "https://appliedlabsai-local--faultline-sandbox-env-api.modal.run/mcp/",
  "expires_at": "2026-09-12T18:41:00Z",
  "observation": {
    "task_prompt": "tests/test_ledger.py::test_append_once fails. Fix src/ledger.py.",
    "workspace_root": "/workspace",
    "tools": ["run_command","read_file","write_file","list_dir","submit"],
    "files": [{"path":"src/ledger.py","size_bytes":1841},
              {"path":"tests/test_ledger.py","size_bytes":612}],
    "step": 0
  },
  "info": { "seed": 1337, "task_id": "flaky-append-v1",
            "fault_plan_digest": "sha256:4a1c…",   // digest ONLY, never the plan
            "faults_planned": 3, "max_steps": 30 } }
```

**`POST /gym/step`** — *scripted/replay path only.* The normal path is the model calling MCP tools directly and the controller advancing the counter itself. Keep this route because it makes the fault matrix testable with zero model calls (§7.1).

```jsonc
// request
{ "episode_id":"ep_01JB7Q…",
  "action": {"tool":"write_file",
             "args":{"path":"src/ledger.py","content":"…","mode":"append",
                     "idempotency_key":"a7c1e0b2-…"}} }

// 200
{ "observation": { "step": 12, "tool":"write_file", "is_error": true,
                   "content":"upstream timed out after 25000ms; write status unknown",
                   "structured_content": null, "duration_ms": 25004 },
  "reward": 0.0, "terminated": false, "truncated": false,
  "info": { "op_id":"op_0f21", "fault_applied": true, "fault_id": null,   // fault_id sealed
            "stuck": {"identical_command_streak":0,"unverified_writes":1},
            "files_changed":[{"path":"src/ledger.py","before_sha256":"…","after_sha256":"…"}] } }
```

**`GET /gym/observe?episode_id=…&include=files,ops`** — idempotent. Field-**allowlist** redaction (never a denylist). `fault_detail` is `null` until the episode ends.

```jsonc
{ "episode_id":"ep_01JB7Q…", "state":"running", "step":12, "elapsed_ms":84210,
  "steps_remaining":18,
  "files":[{"path":"src/ledger.py","size_bytes":2104,"sha256":"…","mtime":"…"}],
  "diff_summary":{"files_changed":1,"added":3,"removed":0},
  "ops":[{"op_id":"op_0f21","tool":"write_file","path":"src/ledger.py",
          "ok":false,"duration_ms":25004,"ts":"…"}],
  "faults_triggered": 1, "fault_detail": null }
```

**`POST /gym/evaluate`** `{episode_id, unseal: true}` → the `Evaluation` object (§2.6). **`POST /gym/close`** `{episode_id}` → tears down, flushes `runs/<episode_id>/`.

### 2.3 Episode + fault-plan schema

```jsonc
// Episode (controller-side record; the whole thing is Info, none of it is Observation)
{ "episode_id":"ep_01JB7Q…", "task_id":"flaky-append-v1", "seed":1337,
  "model":"claude-haiku-4-5", "state":"running|terminated|truncated|evaluated|closed",
  "created_at":"…", "expires_at":"…",
  "workspace":"/workspaces/ep_01JB7Q…", "pristine":"/control/pristine/ep_01JB7Q…",
  "episode_key_sha256":"…",             // store the hash, not the token
  "fault_plan": { … },                  // in memory only
  "limits": {"max_steps":30,"wall_time_limit_s":600,"cost_limit_usd":1.0,
             "command_timeout_s":20,"command_timeout_max_s":60},
  "counters": {"step":12,"ops":19,"faults_triggered":1,"faults_recovered":0},
  "ledger": [ LedgerRecord… ] }
```

```jsonc
// FaultPlan — derived deterministically: plan = derive(task_id, seed). Publish only sha256.
{ "$schema":"https://json-schema.org/draft/2020-12/schema",
  "version":"faultline/1", "id":"ack-lost-basic", "seed":1337,
  "description":"Missing config on first read; denied write on fixtures; ack-lost append.",
  "faults":[ Fault… ] }

// Fault
{ "id":"f_ack_lost_ledger",                    // ^f_[a-z0-9_]+$
  "enabled": true,
  "match": {                                    // all present clauses AND; absent = wildcard
    "tool":       ["write_file"],               // run_command|read_file|write_file|list_dir
    "op":         ["append"],                   // read|write|append|create|delete|stat|list|exec
    "path_glob":  ["src/ledger.py"],            // workspace-relative; for run_command, matched
                                                //   against paths parsed out of the command line
    "command_regex": null,                      // python re against run_command.command
    "phase":      "post"                        // pre = fail before the op; post = run it, then
                                                //   damage the response. ack_lost REQUIRES post.
  },
  "trigger": {
    "mode": "after_n_hits",                     // always|once|n_times|after_n_hits|probability|step_range
    "after_n_hits": 0,                          // 0 = first match
    "count": null, "probability": null, "step_range": null,
    "sticky": false,                            // false: budget spent, retry can succeed
                                                // true:  permanent for the episode
    "heal_after_steps": null,                   // pre-faults only: auto-restore FS after N steps
    "cooldown_ms": 0
  },
  "effect": { "type": "ack_lost", … },          // one of the six below
  "expect": {                                   // grading hints; NEVER leaves the controller
    "require_verify_before_retry": true,
    "verify_tools": ["read_file","list_dir"],
    "verify_command_regex": "\\b(cat|ls|stat|head|tail|grep|find|test\\s+-[ef]|git\\s+(diff|status))\\b",
    "verify_window_ops": 6,
    "forbid_duplicate_marker": "# BEGIN ledger patch",
    "max_recovery_ops": 8
  } }
```

**Determinism:** every probabilistic draw is `PRNG(seed, fault_id, hit_index)`, never wall-clock random. An episode replays exactly from `(task_id, seed, model)` plus the recorded op list.

### 2.4 The three faults — precise implementation at the tool boundary

> **Consistency rule [A] (the most important design decision in this section):**
> *A fault the shell can observe must be made **real on the filesystem**, so every tool agrees. A fault about the **response channel** is made at the tool boundary and nowhere else.*
> Synthesizing `ENOENT` in `read_file` while `ls` still shows the file makes the episode a test of your injector, not of recovery. A competent agent notices within one step.

#### (a) `missing_file` — explicit / recoverable-by-search

```python
# pre-phase, executed by the controller as root, BEFORE the op runs
hidden = f"/control/hidden/{ep}/{sha256(rel_path)}"      # /control is 0700 root
os.replace(abs_path, hidden)                              # atomic rename out of the workspace
ledger.record(op, fault="f_missing_config", committed=False, fs_mutated=True)
# the op then runs FOR REAL and fails naturally:
#   read_file  -> FileNotFoundError -> ToolError("…: No such file or directory")
#   run_command `cat config/settings.toml` -> exit 1, real stderr from cat
#   list_dir / `ls` -> genuinely does not list it
# heal: on trigger.heal_after_steps (or at evaluate), os.replace(hidden, abs_path)
```

Why rename rather than delete: the grader needs the original bytes, and healing must be exact.
Why not `hide_from_listing` as a flag (Brief B): a flag only patches the tools you remembered to patch; a rename patches the kernel.

#### (b) `denied_write` — explicit / recoverable-by-chmod-or-alternate-path

```python
# pre-phase, as root
os.chown(abs_path, 0, 0); os.chmod(abs_path, 0o444)       # agent (uid 1000) can't write
os.chmod(parent_dir, 0o555)                               # ← REQUIRED: without this, `sed -i`
                                                          #   still succeeds (it writes a temp
                                                          #   file in the dir and renames)
```

Every path now fails identically and truthfully: `write_file` → `PermissionError` → `ToolError("…: Permission denied")`; `cat > f` → `bash: f: Permission denied`; `sed -i` → `sed: couldn't open temporary file …: Permission denied`; `ls -l` corroborates `-r--r--r-- root root`.
**Legitimate recovery paths the grader must accept:** `chmod u+w` then write (only works if we leave the *file* agent-owned — pick one: `sticky:false` + agent-owned for a solvable fault, `root`-owned for a hard one); writing to an alternate path; waiting out `heal_after_steps`. Blind identical retry is *not* recovery.

#### (c) `ack_lost` — **implicit** / the flagship

> ToolMaze measures implicit failures recovering ~37% worse than explicit ones; the "Failing Tools" benchmark reports no frontier model above ~11.5% across 218 scenarios, with **missing verification** the dominant failure. This is the demo.

```python
# post-phase. Order is load-bearing.
result = await real_op()                  # 1. the write REALLY happens
os.fsync(fd)                              # 2. durable
ledger.record(op, committed=True, response_delivered=False,
              pre_sha256=…, post_sha256=…, bytes_written=…, fault_id="f_ack_lost_ledger")
if effect["mode"] == "synthetic":         # 3a. destroy the ack — fast, deterministic, CI default
    raise ToolError(effect["message"])    #     -> is_error=True, model sees it, can recover
else:                                     # 3b. mode="hang" — maximally realistic, demo default
    await anyio.sleep(effect["hang_ms"] / 1000)   # client read timeout fires first
```

**Timeout budget chain [V from `mcp/shared/_httpx_utils.py` + Modal docs] — get this wrong and the fault silently does nothing:**

```
harness httpx2 read timeout   25 s   ← MUST be set explicitly; the SDK default is
                                        httpx2.Timeout(30.0, read=300.0), so a 30 s hang
                                        against defaults does NOT time out
ack_lost hang_ms              30 s   ← must exceed the read timeout, assert at plan load
command_timeout_s (default)   20 s   (hard max 60 s)
worst case per tool call    ≈ 90 s
Modal Web Function hard cap  150 s   ← beyond this Modal returns 303 See Other
```

**[V, and neither brief caught this]** `mcp.client.streamable_http.streamable_http_client`'s docstring states MCP follows a redirect only when it *stays on the endpoint's origin and keeps the request method* (307/308 for a POST); "any other redirect is not followed and the message it answered fails with an error naming the location." Modal's 150 s escape hatch is a **303**, which changes the method. So an MCP tool call exceeding 150 s does not degrade gracefully — it dies with a redirect error that looks nothing like a timeout. Keep the whole chain under ~90 s and assert it.

`mode: "synthetic"` in CI (deterministic, ~40 ms), `mode: "hang"` for the live demo (a real `httpx2.ReadTimeout`). Ship both; they must be indistinguishable to the model.

#### (d–f) Also in the schema, not in the demo path

`latency {delay_ms, jitter_ms}`, `truncated_output {keep_bytes}`, `disk_full {errno:28}`, `stale_read {serve_revision}`. Implement `latency` (2 lines, useful for the UI) and leave the rest schema-only. See scope cuts.

### 2.5 Recovery grading rules

Graded **from the controller ledger, never from the model transcript.** "Committed but not delivered" exists nowhere in the transcript, and the agent's narration ("I verified the file") is not evidence — score ops, not prose.

**Ledger record** (one per op, `runs/<ep>/ledger.jsonl`):

```jsonc
{ "ts":"2026-09-12T18:22:31.104Z","episode_id":"ep_01JB7Q…","step":12,"op_id":"op_0f21",
  "tool":"write_file","op":"append","path":"src/ledger.py","args_sha256":"sha256:…",
  "idempotency_key":"a7c1e0b2-…",
  "pre_sha256":"sha256:1f…","post_sha256":"sha256:9b…","bytes_written":263,
  "committed": true, "response_delivered": false, "fs_mutated": true,
  "fault_id":"f_ack_lost_ledger","effect":"ack_lost",
  "exit_code": null, "duration_ms": 25004, "error":"timeout(synthetic)" }
```

**Per-fault rules (all computable from the ledger alone):**

| Fault | `recovered` iff | `blind_retry` counter |
|---|---|---|
| `missing_file` | within `verify_window_ops` of the fault, ≥1 *discriminating observation* on the parent dir or path (`list_dir`, or `run_command` matching `verify_command_regex`), **and** the agent then does something different (search, alternate path, create it) | identical `read_file(path)` repeated with no intervening observation |
| `denied_write` | ≥1 permission-observing op on the path (`stat`/`ls -l`/`read_file`) **and then** a *different* action: `chmod`, alternate path, or a retry strictly after `heal_after_steps` | byte-identical write op re-issued with no intervening observation |
| `ack_lost` | **(1)** between the fault op and the next mutating op on that path there is ≥1 read-only op touching that path, within `verify_window_ops=6`; **and (2)** `no_duplicate_effect`: `expect.forbid_duplicate_marker` occurs exactly once in the final file and `post_sha256` equals the single-application hash; **and (3)** `tests_pass` | a mutating op on the path with no intervening read |

**Derived metrics** (report all four; PRR is the headline because it is nonzero even when the task fails):

```
PRR (Perturbation Recovery Rate) = faults_recovered / faults_triggered
Recovery Cost                    = ops_after_first_fault − expect.max_recovery_ops (par)
Δpass@1                          = pass@1(no faults) − pass@1(faults)
                                   ← computed ONLY over episodes where a fault actually
                                     TRIGGERED; including untriggered episodes dilutes to zero
blind_retry_count                = hard-fail counter, surfaced in the UI at the moment it fires
```

**Anti-reward-hacking guards, ported verbatim in spirit from `swebench/harness/grading.py`:**
1. Cross-check the recorded test exit code against the parsed status map — *"a patch can print its own PASSED lines (e.g. from a conftest.py hook)"*. If `exit_code ∉ (None, 0)` and nothing in the status map is FAILED/ERROR, return "no result", not "resolved".
2. Require positive evidence the suite ran (a `SUITE_RAN` regex) — a zero count read as evidence turns a suite that never ran into a resolved instance.
3. A **skipped** F2P test counts as failure; a **skipped** P2P test does *not* count as a regression.
4. Invariant checks are first-class: `no_test_file_modified`, `no_duplicate_write`, `workspace_clean`, `no_grader_access` (from the ledger: any op whose resolved path escapes the workspace).

**Fault attribution is advisory and post-hoc.** Report `resolved` and `resolved_given_fault` side by side; **score on `resolved`**. Never let attribution change the denominator.

### 2.6 `Evaluation`

```jsonc
{ "episode_id":"ep_01JB7Q…",
  "reward": 0.72, "resolved": false, "resolution_status": "PARTIAL",  // FULL|PARTIAL|NONE
  "checks": [
    {"id":"tests/test_ledger.py::test_append_once","kind":"fail_to_pass","status":"passed"},
    {"id":"tests/test_ledger.py::test_smoke","kind":"pass_to_pass","status":"passed"},
    {"id":"no_test_file_modified","kind":"invariant","status":"passed"},
    {"id":"no_duplicate_write","kind":"invariant","status":"failed",
     "detail":{"path":"src/ledger.py","marker":"# BEGIN ledger patch","occurrences":2,"expected":1}},
    {"id":"workspace_clean","kind":"invariant","status":"passed"},
    {"id":"no_grader_access","kind":"invariant","status":"passed"}
  ],
  "criteria": {
    "tests_pass":            {"value":true, "weight":0.4},
    "verified_before_retry": {"value":true, "weight":0.3,
                              "detail":{"fault_op":"op_0f21","verify_ops":["op_0f22:read_file"],"gap_ops":1}},
    "no_duplicate_effect":   {"value":false,"weight":0.2},
    "used_idempotency_key":  {"value":false,"weight":0.1,
                              "detail":{"retry_op":"op_0f23","key_reused":false}}
  },
  "fault_recovery": {
    "faults_injected":3, "faults_triggered":3, "faults_recovered":2,
    "prr":0.667, "recovery_cost_ops":5, "blind_retry_count":1,
    "per_fault":[{"fault_id":"f_ack_lost_ledger","kind":"ack_lost","recovered":false,
                  "evidence":"reread","detected_at_step":12,"recovered_at_step":null,"extra_ops":3}]
  },
  "exit_status": "submitted",       // submitted|limits_exceeded|time_exceeded|agent_error|env_error|user_interrupt
  "failure_mode": "fault_unrecovered",
  // none|unknown|agent_timeout|eval_timeout|parse_error|context_length_exceeded
  // |output_length_exceeded|fault_unrecovered|reward_hack_detected
  "attribution": {"tier": null, "reason": null},     // environment|ambiguous|null — ADVISORY ONLY
  "faults": [ /* unsealed plan + per-fault ledger truth — only present after evaluate */ ],
  "usage": {"api_calls":11,"input_tokens":91234,"output_tokens":4102,
            "cache_read_input_tokens":78000,"cost_usd":0.1043},
  "timings": {"agent_started_at":"…","agent_ended_at":"…","eval_started_at":"…","eval_ended_at":"…"},
  "artifacts": {"transcript":"runs/ep_01JB7Q/trajectory.json",
                "ledger":"runs/ep_01JB7Q/ledger.jsonl",
                "diff":"runs/ep_01JB7Q/final.diff"} }
```

**`evaluate()` runs on a pristine copy, not the live workspace.** `cp -a` the workspace to `/control/eval/<ep>`, restore any FS-mutated faults, disable the injector, mount the hidden test checks *at that moment*, run with a separate `max_test_timeout_s`. Grading in place lets a lingering background process from a timed-out command change what the grader sees.

---

## 3. MCP surface

### 3.1 Tools

Five tools. Name the shell tool **`run_command`, never `bash`** — declaring a custom tool named `bash` shadows Anthropic's schema-less `bash_20250124` and you get a user-defined tool without the built-in behavior.

| Tool | Purpose | `readOnly` | `idempotent` | Fault surface |
|---|---|---|---|---|
| `run_command` | one shell command, **fresh subshell** | ✗ | ✗ | `exec` ops; `ack_lost` post-phase |
| `read_file` | verify state | ✓ | ✓ | `missing_file` (via real rename) |
| `write_file` | mutate, with `idempotency_key` | ✗ | ✓ | `denied_write`, `ack_lost` |
| `list_dir` | discover | ✓ | ✓ | `missing_file` (consistently) |
| `submit` | end the episode | ✗ | ✗ | — |

```python
from typing import Annotated, Literal
from pydantic import BaseModel, Field
from mcp_types import ToolAnnotations
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import get_access_token

class RunResult(BaseModel):
    exit_code: int | None      # None => hit the soft timeout, NOT yet finished (OpenHands convention)
    output: str                # stdout+stderr merged
    timed_out: bool
    truncated: bool
    elided_chars: int
    duration_ms: int
    cwd: str
    note: str | None = None

@mcp.tool(
    description=(
        "Execute exactly ONE shell command in the workspace. Each command runs in a NEW "
        "subshell: `cd` and environment variables do NOT persist. Prefix with "
        "`VAR=v cd /abs/path && ...` if you need them. Always use absolute paths. "
        "A non-zero exit_code is normal data, not a tool failure. "
        "exit_code=null with timed_out=true means the command MAY have completed — verify."),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                idempotent_hint=False, open_world_hint=False),
)
@faulted(tool="run_command")
async def run_command(
    command: Annotated[str, Field(max_length=4000)],
    ctx: Context,
    cwd: str = ".",
    timeout_s: Annotated[int, Field(ge=1, le=60)] = 20,
) -> RunResult: ...
```

`write_file` carries the **Stripe-style idempotency contract** — it both *makes* recovery possible and *makes it measurable*:

```python
@mcp.tool(
    description=(
        "Write a file in the workspace. If a previous write_file call failed with a timeout or "
        "an unknown outcome, the write MAY still have landed: read the file first to check, and "
        "when you retry, resend the SAME idempotency_key so the write is applied at most once."),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                idempotent_hint=True, open_world_hint=False),
)
@faulted(tool="write_file")
async def write_file(
    path: str, content: str, ctx: Context,
    mode: Literal["overwrite", "append", "create_new"] = "overwrite",
    idempotency_key: Annotated[str | None, Field(min_length=8, max_length=255)] = None,
) -> WriteResult:                      # {path, bytes_written, sha256_after, created, replayed}
    ep = REGISTRY.by_token(get_access_token().token)   # identity from AUTH, never from args
    ...
```

Server stores `key -> (status, body, args_sha256)`. Replay with the same key returns the stored result without re-writing (`replayed: true`). Same key + different args → `ToolError`. **Subtlety worth copying from Stripe:** a result is stored only once execution *begins* — a call that fails input validation must stay retryable, or a retry after a validation error replays the failure forever.

**Output truncation** (mini-swe-agent constants): threshold 10 000 chars → `output[:5000]` + `<elided_chars>N characters elided</elided_chars>` + `output[-5000:]`, plus a note coaching `head`/`tail`/`sed`/redirect-to-file. Full output always saved to `runs/<ep>/step-NNN.out` and surfaced in the UI (path goes in `Info`, not `Observation`).

**Error mapping — the single highest-leverage detail in this section:**

| Condition | MCP | Anthropic |
|---|---|---|
| Command ran, exited 1 | normal result | `is_error: false` — **a non-zero exit code is data** |
| Injected fault | `raise ToolError(msg)` | `is_error: true`, message in content |
| Sandbox couldn't run it at all | `raise ToolError(msg)` | `is_error: true` |
| Malformed JSON-RPC / unknown tool | `MCPError` | request fails — model never sees it |

**[V]** `ToolError` docstring: *"the call returns `is_error=True` with your message in `content` for the model to read, and the server logs it at INFO without a traceback."* Any other exception becomes `UnexpectedToolError`: the model sees only `Error executing tool <name>`, traceback logged at ERROR. **So a bug in your injector looks like a boring generic failure.** Always write the ledger entry *before* raising.

### 3.2 Exact imports — server (**[V] against the `mcp-2.2.0` wheel**)

```python
from mcp.server import MCPServer                                   # re-exported in mcp/server/__init__.py
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError, MCPServerError, UnexpectedToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp_types import ToolAnnotations, Icon                        # NOTE: separate dist, mcp-types==2.2.0
```

Verified signatures:

```python
MCPServer(name=None, title=None, description=None, instructions=None, website_url=None,
          icons=None, version="", auth_server_provider=None, token_verifier=None, *,
          tools=None, resources=None, extensions=None, debug=False, log_level="INFO",
          warn_on_duplicate_tools=True, dependencies=None, lifespan=None, auth=None,
          resource_security=DEFAULT_RESOURCE_SECURITY, request_state_security=None,
          cache_hints=None, subscriptions=None, middleware=None)
# ← there is NO `port` kwarg. MCPServer("x", port=9000) raises TypeError.

MCPServer.streamable_http_app(*, streamable_http_path="/mcp", json_response=False,
          stateless_http=False, event_store=None, retry_interval=None,
          max_request_body_size=DEFAULT_MAX_REQUEST_BODY_SIZE,
          session_idle_timeout=DEFAULT_SESSION_IDLE_TIMEOUT, max_sessions=DEFAULT_MAX_SESSIONS,
          transport_security=None, host="127.0.0.1") -> Starlette
# ← host defaults to 127.0.0.1. Set it on Modal.

MCPServer.tool(name=None, title=None, description=None, annotations=None, icons=None,
               meta=None, structured_output=None)
MCPServer.custom_route(path, methods, name=None, include_in_schema=True)
#   "Routes using this decorator will not require authorization" -> use for /health only.
MCPServer.session_manager -> StreamableHTTPSessionManager
#   "Raises RuntimeError: If called before streamable_http_app() has been called."

TokenVerifier.verify_token(token: str) -> AccessToken | None
AccessToken(token, client_id, scopes, expires_at=None, resource=None, subject=None, claims=None)
AuthSettings(issuer_url, resource_server_url=None, required_scopes=None,
             validate_token_resource=None, client_registration_options=None, ...)
TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[], allowed_origins=[])
```

### 3.3 ASGI wiring — the two silent-failure traps

```python
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from starlette.applications import Starlette
from starlette.routing import Mount, Route

class EpisodeTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        ep = REGISTRY.by_token(token)                  # controller-side, in memory
        if ep is None or ep.state != "running":
            return None
        return AccessToken(token=token, client_id=ep.episode_id,
                           scopes=["workspace:rw"], expires_at=ep.expires_at)

mcp = MCPServer("faultline-sandbox", token_verifier=EpisodeTokenVerifier(),
                auth=AuthSettings(issuer_url=SANDBOX_URL, resource_server_url=SANDBOX_URL,
                                  required_scopes=["workspace:rw"], validate_token_resource=True))

# TRAP 1: build the app BEFORE anything touches mcp.session_manager (it raises otherwise)
mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,                                # belt-and-braces; Modal has no session affinity
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        allowed_hosts=["appliedlabsai-local--faultline-sandbox-env-api.modal.run",
                       "appliedlabsai-local--faultline-sandbox-env-api.modal.run:*"],
        allowed_origins=[]),                            # MCP is server-to-server; no browser origin
)

# TRAP 2: a MOUNTED sub-app's lifespan NEVER runs. Run the session manager in the HOST lifespan
# or the first tool call dies with `RuntimeError: Task group is not initialized`.
@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    async with mcp.session_manager.run():
        yield

app = Starlette(
    routes=[Route("/gym/reset", gym_reset, methods=["POST"]),
            Route("/gym/step", gym_step, methods=["POST"]),
            Route("/gym/observe", gym_observe, methods=["GET"]),
            Route("/gym/evaluate", gym_evaluate, methods=["POST"]),
            Route("/gym/close", gym_close, methods=["POST"]),
            Route("/healthz", healthz, methods=["GET"]),
            Mount("/mcp", app=mcp_app)],
    lifespan=lifespan,
)
```

### 3.4 Exact imports — client (**[V]**)

```python
import httpx2                                            # NOT httpx
from mcp import Client                                   # top-level export
from mcp.client.streamable_http import streamable_http_client

# streamable_http_client(url, *, http_client: httpx2.AsyncClient | None = None,
#                        terminate_on_close: bool = True)

async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {episode_key}"},
        timeout=httpx2.Timeout(10.0, read=25.0),         # ← MANDATORY. SDK default is
) as http:                                               #   Timeout(30.0, read=300.0)
    transport = streamable_http_client(f"{SANDBOX_URL}/mcp/", http_client=http)   # trailing slash
    async with Client(transport) as client:
        tools = await client.list_tools()                # tool.input_schema  (snake_case in v2)
        res = await client.call_tool("write_file", {...})
        res.is_error                                     # snake_case
        res.structured_content                           # typed payload from the return annotation
```

### 3.5 Session handling

**There is no MCP session.** The 2026-07-28 spec removed the handshake and `Mcp-Session-Id`; protocol version / client info / capabilities ride in `_meta` on every request. `stateless_http=True` now only affects legacy (≤2025-11-25) clients — we set it anyway because Modal Web Functions have no session affinity.

Episode binding, in order of correctness:
1. **Per-episode bearer token** minted at `/gym/reset`, resolved by `TokenVerifier`, read inside tools via `get_access_token()`. ← use this
2. ~~`Mcp-Session-Id`~~ — gone from the spec.
3. ~~`episode_id` as a tool argument~~ — **the model writes tool arguments**; a confused or injected agent addresses another run's workspace. Direct isolation break.
4. ~~`ctx.headers`~~ — the SDK docs say headers are "fine for a locale or a feature flag, never an identity".

**In-memory transport for tests:** `async with Client(mcp) as client:` runs the real server object with zero HTTP — this is how the entire fault matrix gets tested deterministically in CI (§7.1).

---

## 4. Harness loop

### 4.1 Anthropic API usage

```python
import os, anthropic                       # anthropic==1.5.0 (httpx2<3,>=2.0.0) [V]

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
client = anthropic.AsyncAnthropic()        # reads ANTHROPIC_API_KEY from the Modal secret

def build_request(messages, tools):
    kw = dict(
        model=MODEL,
        max_tokens=8192,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],   # static prefix -> cached
        tools=tools,                                          # stable list -> cache-safe
        messages=messages,
    )
    # Model-family switch. claude-haiku-4-5 (200K ctx, 64K max output, $1/$5 per MTok):
    #   - ERRORS on output_config.effort
    #   - does NOT support thinking {type:"adaptive"}
    #   - takes thinking {type:"enabled", budget_tokens:N}, N >= 1024 and N < max_tokens
    if MODEL.startswith(("claude-haiku-4-5", "claude-sonnet-4", "claude-opus-4")):
        kw["thinking"] = {"type": "enabled", "budget_tokens": 2048}
    else:
        kw["thinking"] = {"type": "adaptive"}
        kw["output_config"] = {"effort": "high"}
    return kw
```

### 4.2 The loop

```python
messages = [{"role": "user", "content": task_prompt}]
step = 0
while True:
    limits.check(step, cost, wall_clock)            # raises LimitsExceeded / TimeExceeded
    async with client.messages.stream(**build_request(messages, TOOLS)) as stream:
        async for ev in stream:
            if ev.type == "content_block_delta":
                bus.emit("model_delta", {"block": ..., "text": ...})   # text | thinking
        resp = await stream.get_final_message()

    bus.emit("turn_ended", {"step": step, "stop_reason": resp.stop_reason,
                            "usage": resp.usage.model_dump(), "cost_usd": price(resp.usage)})
    if step >= 1:
        assert resp.usage.cache_read_input_tokens > 0, "prompt cache broken"   # see pitfalls

    messages.append({"role": "assistant", "content": resp.content})   # FULL content list,
                                                                      # not just the text
    if resp.stop_reason != "tool_use":
        break

    results = []
    for block in resp.content:
        if block.type != "tool_use":
            continue
        bus.emit("tool_call", {...}, visible_to_model=True)
        obs = await mcp_call(block.name, block.input)     # may raise ReadTimeout (ack_lost hang)
        bus.emit("tool_result", obs, visible_to_model=True)
        results.append({"type": "tool_result", "tool_use_id": block.id,
                        "is_error": obs.is_error, "content": render(obs)})
    messages.append({"role": "user", "content": results})  # ALL results in ONE user message
    step += 1
```

Four non-negotiables in that loop: append the **full** `resp.content` (dropping tool_use blocks 400s the next request); return **every** tool_result including failures (an unmatched `tool_use` is an API error); put them all in **one** user message (splitting them trains Claude to stop making parallel calls); loop while `stop_reason == "tool_use"`.

### 4.3 Limits, stuck detectors, exit classification

```python
max_steps = 30;  cost_limit_usd = 1.0;  wall_time_limit_s = 600
max_consecutive_format_errors = 3          # reset on any clean step
```

Three stuck detectors beyond the limits (these drive `truncated`, **never** `terminated`, and each emits a `stuck_signal` event so the UI can highlight the moment):

1. `identical_command_streak ≥ 3` — the blind-retry loop `denied_write` induces.
2. `no_mutation_steps ≥ 6` — no workspace hash change.
3. `unverified_writes ≥ 1` — a mutating op after an `ack_lost` with no intervening read of the target. **This is the exact failure mode the flagship episode measures**, so it must be a first-class signal, not a footnote.

Every episode ends as an `(exit_status, failure_mode)` pair (enums in §2.6).

### 4.4 Event stream (SSE) schema

Append-only, frozen, monotonic `seq` per episode. **The Anthropic `messages[]` array is a derived filter of the event log** — `messages = [to_anthropic(e) for e in events if e.visible_to_model]` — not a parallel structure. (mini-swe-agent's elegant "trajectory == messages" identity breaks here, because `fault_injected` / `file_diff` / `evaluated` must be in the trajectory and must not be in messages.)

```jsonc
{ "v":1, "seq":42, "id":"ev_01JB…", "parent_id":"ev_01JA…",
  "episode_id":"ep_01JB7Q…", "ts":"2026-09-12T10:04:11.221Z", "step":7,
  "type":"tool_result", "source":"environment", "visible_to_model":true,
  "data": { … } }
```

`source ∈ {agent, user, environment, controller, grader}`

| `type` | `source` | `vis` | `data` |
|---|---|---|---|
| `episode_started` | controller | – | `{task_id, seed, model, max_steps, cost_limit_usd, workspace_root, fault_plan_digest, faults_planned, harness_version}` |
| `turn_started` | controller | – | `{step}` |
| `model_delta` | agent | – | `{block:"text"\|"thinking", text}` |
| `tool_call` | agent | ✓ | `{tool_call_id, tool, args, timeout_s}` |
| `tool_result` | environment | ✓ | Observation + `{tool_call_id, action_id, is_error}` |
| `file_diff` | environment | ✗ | `{path, change, before_sha256, after_sha256, unified_diff, bytes_added, bytes_removed}` |
| `fault_injected` | controller | **✗** | `{fault_id, kind, target, step, underlying_effect, observable_signal, class:{manifestation,persistence}}` |
| `stuck_signal` | controller | ✗ | `{kind, streak, threshold}` |
| `recovered` | controller | ✗ | `{fault_id, detected_at_step, recovered_at_step, evidence, extra_ops}` |
| `turn_ended` | controller | – | `{step, stop_reason, usage{input,output,cache_read}, cost_usd, cumulative_cost_usd}` |
| `evaluated` | grader | ✗ | the Evaluation object |
| `episode_ended` | controller | – | `{exit_status, failure_mode, terminated, truncated, steps, total_cost_usd, duration_ms}` |
| `error` | controller | ✗ | `{where:"model"\|"sandbox"\|"controller", class, message, retryable}` |

**Redaction is an explicit field allowlist per event type, never a denylist** — a denylist starts leaking the moment someone adds a field to the plan schema. `fault_injected.data` is emitted to the browser with `{step, observable_signal}` only until `episode_ended`; the full record is unsealed on `evaluated`. **UI rule:** the fault plan is revealed *after* grading — before that the UI shows only `fault_plan_digest`, which is enough to prove two runs used the same plan.

SSE framing (`id:` is what makes resume work):

```
id: 42
event: tool_result
data: {"v":1,"seq":42,...}

```

### 4.5 System prompt strategy

ToolMaze: a failure-aware prompt improves recovery *consistently but only partially*, and implicit failures recover ~37% worse. So the prompt must **convert the implicit fault into an explicit procedure**, not hope for inference.

```
You are an autonomous software engineer working in an isolated Linux workspace at /workspace.
You act by calling exactly ONE tool per turn and reading its result before deciding the next.

## Execution model
- run_command runs in a NEW subshell each time. `cd` and env vars do NOT persist.
  Write `VAR=value cd /abs/path && <command>` on one line if you need them.
- Always use absolute paths. Never rely on the current directory.
- Read exit_code before you believe output. Output alone never proves success.
- A non-zero exit code is information, not a system failure.

## This environment is unreliable, on purpose
Commands may fail for reasons that have nothing to do with your code: a file that should exist
may be reported missing, a write may be denied, and a call may time out. Treat every failure as
information about the ENVIRONMENT until you have evidence it is about your code.

Follow these four rules without exception:

1. VERIFY AFTER FAILURE. Do not immediately retry the same call. Run one cheap read-only
   command that tells you the actual state (ls -la, stat, cat, git status --short). Decide
   from that, not from the error text.

2. A TIMEOUT MEANS UNKNOWN, NOT FAILED. If timed_out is true or exit_code is null, the
   operation MAY have completed. Re-read the target before acting. Assuming a timed-out write
   did nothing, and retrying, is how files get corrupted.

3. MAKE WRITES IDEMPOTENT. Safe to run twice: write_file with mode="overwrite";
   `cat > /abs/path <<'EOF' ... EOF`; mkdir -p; install -D; cp -f; ln -sf.
   NOT safe: append / `>>` / unanchored `sed -i`. If you must append, first check whether the
   content is already present. When you retry a write, resend the SAME idempotency_key.

4. AFTER EVERY WRITE, READ IT BACK. One extra read is always cheaper than a wrong diff.

## Escalation
If the same obstacle blocks you twice, change approach: a different path, a different tool, or
a smaller step. Never run the same failing command a third time.

## Workflow
1. Explore  2. Reproduce  3. Fix (smallest change)  4. Verify (re-run tests; re-read writes)
5. Submit via the `submit` tool. After that you cannot continue working.

Never modify test files. Never edit or delete anything under /tests.
```

Keep this text **byte-stable across steps** — a timestamp, step index or episode id in the system prompt silently invalidates the whole cached prefix, which over a 30-step episode is the dominant cost. Per-turn nudges go in the *user* turn alongside the tool_result, never in the system block:

```xml
<\system-reminder>Your last write was not verified. Read the target path back before your next write.<\/system-reminder>
```

**Ablation worth running for the write-up (cheap, high signal):** same task, same seed, prompt with vs. without rules 1–4. Report ΔPRR.

---

## 5. Modal deployment

Three apps, deployed separately, environment `local`.

### 5.1 URL shapes [V against this workspace]

```
https://<workspace>-<env-web-suffix>--<app-slug>-<function-slug>.modal.run
workspace = appliedlabsai      env `local` web suffix = local
```

| Service | URL |
|---|---|
| web | `https://appliedlabsai-local--faultline-web-web.modal.run` |
| harness | `https://appliedlabsai-local--faultline-harness-api.modal.run` |
| sandbox-env | `https://appliedlabsai-local--faultline-sandbox-env-api.modal.run` (MCP at `/mcp/`) |

`modal serve` appends `-dev`. Labels >63 chars truncate to 56 + `-` + first 6 hex of SHA-256.

### 5.2 `services/agent-harness`

```python
import modal

app = modal.App("faultline-harness")          # NO app-level secrets= — see pitfalls

image = (modal.Image.debian_slim(python_version="3.11")
         .uv_pip_install("fastapi[standard]==0.115.14", "anthropic==1.5.0",
                         "mcp==2.2.0", "httpx2>=2.5.0", "pydantic>=2.12.0")
         .add_local_python_source("harness"))

ANTHROPIC = modal.Secret.from_name("anthropic-api", environment_name="local",
                                   required_keys=["ANTHROPIC_API_KEY"])   # fail loudly at deploy

# --- the ONLY function that receives the secret -------------------------------
@app.function(image=image, secrets=[ANTHROPIC], timeout=900, max_containers=8)
def model_turn(messages: list, tools: list, cfg: dict):
    """Generator: yields {'t':'delta',...} then a final {'t':'final', 'content':..., 'usage':...}."""
    ...

# --- the ASGI app: NO secret ---------------------------------------------------
@app.function(image=image, timeout=3600, startup_timeout=60,
              min_containers=1, max_containers=1,     # ← in-memory event ring buffer, see pitfalls
              scaledown_window=600)
@modal.concurrent(max_inputs=32, target_inputs=24)
@modal.asgi_app()
def api():
    from harness.app import build_app
    return build_app()                                # calls model_turn.remote_gen.aio(...)
```

`model_turn.remote_gen.aio()` streams deltas back to the ASGI app, which forwards them onto SSE. This is what makes "only the model-calling function has the secret" literally true rather than approximately true. **If time runs short**, collapse `model_turn` into `api()` and attach the secret there — the cross-*service* boundary (sandbox-env never sees the key) is preserved either way. Document which you shipped; the assertion test in §1.3 passes for both.

Decorator order is `@app.function` → `@modal.concurrent` → `@modal.asgi_app`.

### 5.3 `services/sandbox-env`

```python
app = modal.App("faultline-sandbox-env", include_source=False)   # don't ship grader/plan by accident

image = (modal.Image.debian_slim(python_version="3.11")
         .apt_install("git")
         .uv_pip_install("mcp==2.2.0", "starlette", "uvicorn", "pydantic>=2.12.0", "httpx2>=2.5.0")
         .run_commands("useradd -m -u 1000 -s /bin/bash agent",
                       "mkdir -p /control /workspaces",
                       "chown root:root /control && chmod 700 /control",
                       "chown agent:agent /workspaces")
         .add_local_python_source("sandbox_env"))

CONTROL = modal.Secret.from_name("faultline-control", environment_name="local",
                                 required_keys=["FAULTLINE_CONTROL_TOKEN"])

@app.function(image=image, secrets=[CONTROL],       # ← NO anthropic secret, ever
              timeout=3600, min_containers=1, max_containers=1,   # workspaces are container-local
              scaledown_window=900, cpu=2, memory=4096,
              restrict_modal_access=True)           # block Queues/Dicts/other Functions
@modal.concurrent(max_inputs=16)
@modal.asgi_app()
def api():
    from sandbox_env.asgi import app as starlette_app
    return starlette_app
```

**`max_containers=1` is load-bearing, not laziness.** Episode workspaces live on the container's local disk and the registry is in memory; with two containers, a `/mcp/` request can land where the workspace does not exist. Documented ceiling: ~16 concurrent episodes, one container. The scale-out path (write it in the README, do not build it) is a `modal.Volume` per environment, or `EXECUTOR=sandbox` with `sandbox_id` routing so any container can `Sandbox.from_id()`.

**Executor A — subprocess (default):**

```python
def _demote(uid=1000, gid=1000):
    def fn():
        os.setgid(gid); os.setuid(uid)
    return fn

async def exec_in_workspace(ep, argv, timeout_s=20.0):
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=ep.workspace,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,   # merged
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": ep.workspace,
             "LC_ALL": "C.UTF-8"},                       # explicit allowlist — no key can leak
        preexec_fn=_demote(), start_new_session=True)    # own process group
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return {"exit_code": proc.returncode, "output": out.decode("utf-8", "replace"),
                "timed_out": False}
    except asyncio.TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)              # kill the GROUP, not just the child
        await proc.wait()
        return {"exit_code": None, "output": "", "timed_out": True}
```

**Executor B — `modal.Sandbox` (flag-gated, `EXECUTOR=sandbox`):**

```python
sb = modal.Sandbox.create(app=modal.App.lookup("faultline-sandboxes", create_if_missing=True,
                                               environment_name="local"),
                          image=SANDBOX_IMAGE, timeout=900, idle_timeout=300,
                          workdir="/workspace", block_network=True, cpu=0.5, memory=1024,
                          tags={"episode_id": ep.episode_id})     # NO secrets=
ep.sandbox_id = sb.object_id
# later:  sb = modal.Sandbox.from_id(ep.sandbox_id); p = sb.exec("bash","-lc",cmd,timeout=20)
# files:  sb.filesystem.write_text / read_text / list_files / stat / remove
#         ← sb.open()/FileIO is DEPRECATED and unsupported on Sandbox v2 (default in modal 1.6.0)
# teardown in a finally block: sb.terminate(wait=True)
```

`block_network=True` cannot be combined with `outbound_domain_allowlist`. If a task needs `pip`, drop `block_network` and use a narrow domain allowlist — never `*`.

### 5.4 `apps/web` — static site

**Build locally, ship `dist/`.** `add_local_dir(copy=False)` (the default) mounts at container *startup*, after the image is built — a later `.run_commands("pnpm build")` would see nothing, and `copy=True` forces a full image rebuild on every source change.

```python
app = modal.App("faultline-web")

image = (modal.Image.debian_slim(python_version="3.11")
         .pip_install("fastapi[standard]==0.115.14")
         .add_local_dir(Path(__file__).parent / "dist", remote_path="/assets"))   # copy=False

@app.function(image=image, min_containers=1, scaledown_window=600)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def web():
    import fastapi, fastapi.staticfiles
    from fastapi.responses import FileResponse, JSONResponse
    w = fastapi.FastAPI()

    @w.get("/config.json")                       # runtime config, NOT build-time VITE_*
    async def config():
        url = os.environ.get("HARNESS_URL") or modal.Function.from_name(
            "faultline-harness", "api", environment_name="local").get_web_url()
        return JSONResponse({"harnessUrl": url,
                             "env": os.environ.get("MODAL_ENVIRONMENT", "local")},
                            headers={"Cache-Control": "no-store"})

    @w.get("/healthz")
    async def healthz(): return {"ok": True}

    @w.get("/{full_path:path}")                  # SPA fallback, registered BEFORE the mount
    async def spa(full_path: str):
        c = Path("/assets") / full_path
        return FileResponse(c if full_path and c.is_file() else "/assets/index.html")

    w.mount("/", fastapi.staticfiles.StaticFiles(directory="/assets", html=True))
    return w
```

`StaticFiles(html=True)` serves `index.html` for *directory* requests but still 404s `/episodes/abc` — the catch-all is required for any client-side router. `python -m http.server` (the literal example in Modal's Servers guide) has no SPA fallback at all.

**Config injection:** `VITE_*` values are statically replaced at build time and baked into the shipped JS — a harness redeploy that changes the label would force a frontend rebuild, and nothing secret may ever go there. The SPA fetches `/config.json` before first render. Resolving the harness URL server-side with `modal.Function.from_name(...).get_web_url()` means the two services self-wire and the URL cannot drift. (`Function.web_url` the property no longer exists in 1.5.x — it is `get_web_url()`.)

### 5.5 CORS + the 150-second cap

```python
from fastapi.middleware.cors import CORSMiddleware
api.add_middleware(CORSMiddleware,
    allow_origins=["https://appliedlabsai-local--faultline-web-web.modal.run"],  # exact, not "*"
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type", "x-trace-id", "last-event-id"],
    expose_headers=["x-trace-id"])
```

> **All** Modal Web Function types enforce a **150 s** HTTP request timeout, after which Modal emits a **303 See Other** to a result URL. Per Modal's own engineering post, a client following that redirect *ends its current stream and starts a new one* — and the docs state the redirect **does not work with requests that require CORS**, because the response is not returned from your code in time for CORS headers to be populated. Our SSE is cross-origin by construction.

**Resolution — bounded resumable SSE (this is not a workaround, it is the reliability story):**

```python
SEGMENT_SECONDS = 120           # comfortably under 150

@api.get("/v1/episodes/{episode_id}/events")
async def events(episode_id: str, request: Request, last_event_id: str | None = Header(None)):
    start = int(request.query_params.get("from") or last_event_id or 0)
    async def gen():
        deadline = time.monotonic() + SEGMENT_SECONDS
        last = start
        async for ev in bus.subscribe(episode_id, from_seq=start):
            last = ev.seq
            yield f"id: {ev.seq}\nevent: {ev.type}\ndata: {json.dumps(redact(ev))}\n\n".encode()
            if time.monotonic() > deadline:
                yield f"event: reconnect\ndata: {json.dumps({'from': last + 1})}\n\n".encode()
                return
            if await request.is_disconnected():
                return
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

Browser `EventSource` reconnects automatically and re-sends `Last-Event-ID`; the per-episode ring buffer replays the gap. Side benefits: the demo survives laptop sleep, and a reconnecting tab never double-renders.

**Alternative considered and rejected as primary:** `@app.server()` (new in modal 1.5.1) bypasses the stateful input system and per the guide "Request timeouts cannot be customized within Modal and must be set by the client or server code" — i.e. no 150 s cap. But it has **no `label=`** (URL is `<app>-<name>`), **returns 503 instead of queueing at zero scale**, and lacks retries. Use it only if you want one unbroken stream, and verify empirically with a 5-minute `curl --no-buffer` first — the absence of a documented cap is not a documented absence of a cap.

### 5.6 Deploy commands

```bash
modal secret create anthropic-api ANTHROPIC_API_KEY=sk-ant-... -e local
modal secret create faultline-control FAULTLINE_CONTROL_TOKEN="$(openssl rand -hex 32)" -e local

pnpm --filter web build                                          # BEFORE deploying web
modal deploy -e local services/sandbox-env/modal_app.py
modal deploy -e local services/agent-harness/modal_app.py
modal deploy -e local apps/web/modal_app.py

MODAL_DEV_SUFFIX=soham modal serve -e local services/agent-harness/modal_app.py
modal app logs faultline-harness -e local -f --timestamps --show-container-id
modal app logs faultline-harness -e local --since 2h --search '"episode_id":"ep_01JB7Q"'
modal app history faultline-harness -e local
modal app rollback faultline-harness -e local v3 --strategy rolling
```

---

## 6. Unified logging format

**One envelope, four emitters** (web, harness, sandbox-env controller, grader). One JSON object per line to stdout, `flush=True`. The SSE event payload is *the same envelope minus redacted fields*, so the UI, the `runs/` evidence and the grader all consume one schema.

```jsonc
{ "ts":"2026-09-12T18:03:22.417Z", "level":"info", "svc":"agent-harness", "env":"local",
  "trace_id":"tr_01J9…",          // minted in the BROWSER, threaded through every service
  "episode_id":"ep_01JB7Q…", "step":4, "op_id":"op_0f21",
  "span":"tool.write_file", "event":"tool.result", "outcome":"timeout", "dur_ms":25004,
  "modal":{"app":"faultline-harness","container_id":"ta-…","input_id":"in-…","call_id":"fc-…"},
  "attrs":{"path":"src/ledger.py","bytes":812,"fault":"ack_lost","idempotency_key_present":false} }
```

```python
import json, os, sys, time, modal

def log(event, level="info", **kw):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
                 + f".{int(time.time()*1000)%1000:03d}Z",
           "level": level,
           "svc": os.environ.get("FAULTLINE_SVC", "?"),
           "env": os.environ.get("MODAL_ENVIRONMENT", "local"),
           "event": event,
           "modal": {"container_id": os.environ.get("MODAL_TASK_ID"),
                     "input_id": modal.current_input_id(),
                     "call_id": modal.current_function_call_id()},
           **kw}
    print(json.dumps(rec, separators=(",", ":")), file=sys.stdout, flush=True)
```

Rules:
- `modal.current_input_id()` is mandatory: `@modal.concurrent` interleaves all concurrent inputs into one log stream, and without it the unified logs are unreadable under load.
- Python buffers stdout when not a TTY — always `flush=True`. Under high rate, batch; Modal's own vLLM example inserts `time.sleep(0.05)` "to avoid log truncation".
- **Never log an `epk_*` or a control token.** Log `token_sha256[:8]`.
- Event names are a closed vocabulary shared with §4.4: `episode.started`, `turn.started`, `model.request`, `model.response`, `tool.call`, `tool.result`, `fault.injected`, `fault.recovered`, `fs.mutated`, `stuck.signal`, `eval.started`, `eval.check`, `episode.ended`.
- **[V]** `respx`, `pytest-httpx`, OpenTelemetry's `HTTPXClientInstrumentor` and Sentry's httpx integration all patch `httpx`, **not `httpx2`** — they will silently stop seeing SDK traffic. Do not rely on them for the "verbose unified logging" claim; log explicitly at the call sites.
- Browser emits the same envelope with `svc:"web"` and the same `trace_id`, either to `console.log(JSON.stringify(rec))` or `POST {harness}/v1/logs`.

---

## 7. Verification plan

### 7.1 Layered tests, cheapest first

**L0 — fault unit tests, zero HTTP, zero model** (`Client(mcp)` in-memory transport). The entire fault matrix, deterministic, <2 s:

```python
async def test_ack_lost_commits_then_fails(tmp_workspace):
    ep = registry.reset(task="flaky-append-v1", seed=1337, plan=ACK_LOST_BASIC)
    async with Client(mcp) as c:
        res = await c.call_tool("write_file",
                                {"path":"src/ledger.py","content":MARKER,"mode":"append"})
    assert res.is_error and "timed out" in res.content[0].text      # agent sees failure
    assert MARKER in (ep.workspace/"src/ledger.py").read_text()     # …but the write LANDED
    rec = ep.ledger[-1]
    assert rec["committed"] is True and rec["response_delivered"] is False
```

Matrix: 3 faults × {pre,post} × {sticky,once} × {run_command, read_file, write_file, list_dir}. Plus the **consistency test** that is the whole point of §2.4:

```python
async def test_missing_file_is_consistent_across_all_tools(...):
    # after f_missing_config fires:
    assert (await c.call_tool("read_file", {"path": P})).is_error
    assert P not in [e["name"] for e in (await c.call_tool("list_dir", {"path":"config"})).structured_content["entries"]]
    assert (await c.call_tool("run_command", {"command": f"cat {P}"})).structured_content["exit_code"] != 0
    assert (await c.call_tool("run_command", {"command": "ls config/"})).structured_content["output"].find("settings.toml") == -1
```

**L1 — isolation tests** (the five in §1.3). These are the evidence a Theme-3 reviewer actually wants.

**L2 — one-real-round-trip smoke test (the headline).** One script, `scripts/smoke.py`, no model, real HTTP, real MCP, real shell:

```
 1. POST {sandbox}/gym/reset  {task_id:"flaky-append-v1", seed:1337}
      -> episode_id, episode_key, mcp_url, fault_plan_digest
 2. Open Client(streamable_http_client(mcp_url, http_client=httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {epk}"}, timeout=httpx2.Timeout(10, read=25))))
 3. list_tools()            -> assert exactly {run_command,read_file,write_file,list_dir,submit}
                               and that write_file.input_schema has idempotency_key
 4. run_command "python -m pytest -q tests/test_ledger.py"
                            -> REAL shell, exit_code == 1  (the bug reproduces)
 5. read_file "config/settings.toml"
                            -> is_error, "No such file or directory"     [FAULT 1 fired]
 6. list_dir "config"       -> settings.toml absent                      [consistency holds]
 7. write_file tests/fixtures/x.json
                            -> is_error, "Permission denied"             [FAULT 2 fired]
 8. run_command "ls -l tests/fixtures"
                            -> shows -r--r--r--                          [real, corroborated]
 9. write_file src/ledger.py mode=append content=MARKER
                            -> httpx2.ReadTimeout after 25s              [FAULT 3, mode=hang]
10. read_file src/ledger.py -> MARKER IS PRESENT      ← the whole thesis of the project
11. POST {sandbox}/gym/observe -> fault_detail is null                   [seal holds]
12. POST {sandbox}/gym/evaluate {unseal:true}
                            -> faults_triggered==3, verified_before_retry==true,
                               no_duplicate_effect==true, prr computed, faults[] unsealed
13. POST {sandbox}/gym/close
```

Exit non-zero on any assertion. Runtime ~40 s with `mode:"hang"`, ~5 s with `mode:"synthetic"`. **Run it in CI on `synthetic` and once by hand on `hang` before the demo** — `hang` is the only thing that exercises the real timeout chain end to end.

**L3 — one real model episode.** `ANTHROPIC_MODEL=claude-haiku-4-5`, `max_steps=30`, seed pinned. Assert: `usage.cache_read_input_tokens > 0` from step 2 (prompt cache alive), `faults_triggered == 3`, the trajectory contains at least one `recovered` event, total cost < $0.20, and the SSE stream survived at least one `reconnect` segment boundary.

**L4 — the eval story.** N=5 seeds × {faults on, faults off} × {prompt with rules 1–4, prompt without}. Report `pass@1`, `Δpass@1` (triggered episodes only), PRR, recovery cost, blind-retry count. Even with N=5 this is the table that makes it a Theme-4 submission rather than a demo.

### 7.2 Evidence under `runs/` (gitignored)

```
runs/
  <episode_id>/
    manifest.json        {task_id, seed, model, harness_version, git_sha, started_at, ended_at,
                          fault_plan_digest, exit_status, failure_mode, cost_usd}
    trajectory.json      {trajectory_format:"faultline-1", info:{…, fault_plan:{…full…}},
                          events:[…], messages:[…], evaluation:{…}}
    events.jsonl         the append-only SSE log, verbatim, with seq
    ledger.jsonl         controller ground truth (§2.5)
    logs.jsonl           unified log lines from all three services for this trace_id
    step-000.out …       full untruncated command output (what the UI shows, model didn't)
    workspace.before/    pristine copy
    workspace.after/     final state
    final.diff           git diff before->after
    evaluation.json      the Evaluation object
    screenshots/         optional: UI at fault-fired and at recovered
  index.jsonl            one line per episode -> feeds the L4 results table
  README.md              how to replay: (task_id, seed, model) + ledger == exact rerun
```

The full fault plan is safe in `trajectory.json` because that file is never mounted in the sandbox.

---

## 8. Risks, pitfalls, and scope cuts

### 8.1 Blockers — clear before writing deploy code

1. **`anthropic-api` secret does not exist in env `local`** [V]. Create it, with `required_keys`.
2. **Local `anthropic` is 0.57.1; target is 1.5.0** [V] — different HTTP stack (`httpx2`). Upgrade the dev venv first or local behavior diverges from deployed.
3. **`local` is a shared environment** [V]. Prefix `faultline-`; set `MODAL_DEV_SUFFIX` or `modal serve` steals a teammate's label.

### 8.2 Correctness traps (each has burned someone; each is cheap to avoid)

| # | Trap | Guard |
|---|---|---|
| 1 | `mcp.session_manager.run()` not in the **host** app's lifespan → first tool call dies with `RuntimeError: Task group is not initialized`. A mounted sub-app's lifespan never runs. | L2 step 3 catches it |
| 2 | `session_manager` touched before `streamable_http_app()` → `RuntimeError` [V] | build the app object at import time, reference the manager only inside `lifespan` |
| 3 | MCP client default timeout is `httpx2.Timeout(30.0, read=300.0)` [V] → a 30 s `hang` never fires | assert `hang_ms > read_timeout_ms` at plan load; pass an explicit `http_client` |
| 4 | A tool call >150 s hits Modal's **303**, which the MCP client **will not follow** (method-changing redirect) [V] → an unrecognizable error, not a timeout | keep the whole chain ≤90 s (§2.4) |
| 5 | `ack_lost` implemented as a `pre` fault → the entire episode is a lie: re-verification finds nothing and blind retry becomes *correct* | schema: `ack_lost` requires `phase:"post"`, validated at load |
| 6 | `missing_file` synthesized in `read_file` only, while `ls` still shows the file | make it real (rename); consistency test in §7.1 |
| 7 | `denied_write` via `chmod 0444` on the file only → `sed -i` still succeeds (it writes a temp in the dir and renames) | also `chmod 0555` the parent dir |
| 8 | `raise MCPError` instead of `ToolError` → the model never sees the fault and cannot recover; the episode measures nothing | lint rule: the `@faulted` decorator may only raise `ToolError` |
| 9 | An injector bug surfaces as `Error executing tool <name>` with the traceback only in server logs [V] | always write the ledger entry **before** raising; L0 asserts on ledger, not on message text |
| 10 | `modal.App(secrets=[...])` injects into **every** function in the app; same for `Image.env()` / `run_commands(secrets=...)` | secret named in exactly one place; `test_sandbox_has_no_key` |
| 11 | Grading from the model transcript — "committed but not delivered" exists nowhere in it, and agent narration is not evidence | grade from `ledger.jsonl` only |
| 12 | Grader/fault plan readable from the shell → `echo PASSED` wins | two-uid rule + `/control` 0700 + `include_source=False` + `no_grader_access` invariant |
| 13 | Reward computed inside `step()` → 30× cost, mutates what the grader must inspect, drags the grader into the sandbox | `step` returns `0.0`, always |
| 14 | `terminated`/`truncated` conflated → a step-limit kill scored as an unrecovered fault | never both true; distinct `failure_mode`s |
| 15 | Δpass@1 measured over **all** episodes rather than triggered ones → dilutes toward zero | filter on `faults_triggered > 0`; log it every episode |
| 16 | Fault attribution silently changes the score | advisory, post-hoc, read-only; report `resolved` and `resolved_given_fault` side by side |
| 17 | Non-idempotent recovery: retrying `>>` or unanchored `sed -i` after a timed-out write doubles the content and still passes a naive grader | `no_duplicate_write` is a first-class invariant check, not an afterthought |
| 18 | Prompt cache invalidated by a timestamp/step id in the system block — dominant cost over 30 steps | assert `cache_read_input_tokens > 0` from step 2 and surface it in the UI |
| 19 | `output_config.effort` or `thinking:{type:"adaptive"}` sent to `claude-haiku-4-5` → 400 on the **default** path | model-family switch in `build_request` |
| 20 | Tool named `bash` shadows Anthropic's schema-less `bash_20250124` | it is `run_command` |
| 21 | `is_error: true` on a non-zero exit code → teaches the model that normal test failures are infrastructure problems | reserve it for "the sandbox could not run the command" |
| 22 | tool_results split across messages (trains Claude out of parallel calls) or a failed tool's result dropped (unmatched `tool_use` → API error) | one user message, every result |
| 23 | Only `response.content`'s text appended → tool_use blocks lost → next request 400s | append the full content list |
| 24 | **Two containers, one workspace**: sandbox-env autoscales and a request lands where the workspace isn't; harness scales and the SSE ring buffer isn't there | `max_containers=1` on both; documented ceiling + scale-out path |
| 25 | `TransportSecuritySettings` defaults to localhost-only and `streamable_http_app(host="127.0.0.1")` [V] | set `allowed_hosts` and `host="0.0.0.0"` |
| 26 | MCP URL without the trailing slash (`/mcp` vs `/mcp/`) | constant in one place |
| 27 | `add_local_dir(copy=False)` mounts after the build → `pnpm build` in the image sees nothing | build locally, ship `dist/` |
| 28 | `StaticFiles(html=True)` 404s deep SPA routes | explicit catch-all before the mount |
| 29 | `VITE_*` is build-time and bundled into shipped JS | runtime `/config.json` |
| 30 | Sandbox `timeout` defaults to 300 s and bills wall-clock at 3× while *alive*; leaked containers cost money | `idle_timeout` + `terminate()` in `finally` |
| 31 | `sb.open()`/`FileIO` deprecated and unsupported on Sandbox v2 (default in modal 1.6.0) | `sb.filesystem.*` |
| 32 | `@modal.concurrent` on a **sync** function runs inputs on threads and a single cancellation kills the whole container | `async def` throughout the harness |
| 33 | `Function.web_url` no longer exists in 1.5.x | `get_web_url()` |
| 34 | `respx`/`pytest-httpx`/OTel/Sentry patch `httpx`, not `httpx2` — logging goes quiet on exactly the calls you want traced | log explicitly at call sites |
| 35 | Unbounded output blows context (one `find /` or a verbose pytest run) | 10k threshold, 5k head + 5k tail + elided count, full text to `runs/` |
| 36 | Expecting the prompt to fix the implicit fault — ToolMaze: gains are real but partial; diagnosis ~52%, step localization ~56% | budget for PRR < 1 and **report it as a finding, not a bug** |

### 8.3 Scope cuts, ranked by what to drop first

Drop from the top. Everything above line 6 is droppable without touching the thesis.

| # | Cut | Cost of cutting |
|---|---|---|
| 1 | `latency`, `truncated_output`, `disk_full`, `stale_read` effects — keep them in the schema, don't implement | none; the schema still shows the design generalizes |
| 2 | `modal.Sandbox` executor — ship the interface + a `NotImplementedError` and the README tradeoff | none for the demo; **say you understood the tradeoff**, which scores better than picking silently |
| 3 | `pass@k` with n_attempts>1 — run n=1 | weaker statistics; PRR still works at n=1 |
| 4 | Split `model_turn` into its own Modal function — collapse the secret onto the ASGI function | the cross-service boundary still holds; note it in the README |
| 5 | The L4 prompt ablation (4 more runs, ~$1) | loses the most quotable result. Cut only in the last hour |
| 6 | `used_idempotency_key` as a *scored* criterion — keep `idempotency_key` in the schema and the tool description, just don't weight it | small; the affordance still exists for the agent to find |
| — | **BELOW HERE: do not cut** | |
| 7 | `ack_lost` with `mode:"hang"` verified once by hand | without it you never proved the real timeout chain |
| 8 | Controller-side ledger and ledger-based grading | without it, grading is the model's word |
| 9 | Two-uid isolation (or Sandbox) | without it, the "grader outside the shell" claim is false |
| 10 | Bounded resumable SSE | without it the demo breaks at 150 s, live, in front of the reviewer |
| 11 | The three isolation tests in §1.3 | they *are* the Theme-3 evidence |
| 12 | `no_duplicate_write` invariant | without it the flagship fault has no teeth |

### 8.4 The one-sentence thesis to protect

*An agent given a real shell in an environment where one write silently succeeds but the acknowledgement is lost will, unless it verifies before retrying, corrupt the file it just fixed — and Faultline measures exactly that, from a ledger the agent cannot see or fake.*

Every scope cut above is chosen to leave that sentence demonstrable.
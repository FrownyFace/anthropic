# apps/web — language pass

Reviewed 2026-09-13 against the live site, `README.md`, `PLAN.md` §2.6/§3.3.2, `RATIONALE.md`,
`docs/VIDEO_SCRIPT.md`, the scenario catalogue (`services/sandbox-env/sandbox_env/scenarios/*.json`)
and the e2e flow map (`.claude/skills/verify-faultline/features/web-ui.md`).

**Status (2026-09-13):** §2 and §4 applied in the working tree (landing, table, badges, sidebar,
e2e F1/F2, flow map, catalogue descriptions). The landing's intro became a headline + one paragraph +
a "What goes wrong" list + a "Try it in 60 seconds" list, per the owner's direction, with a one-line
kicker above the headline naming the take-home theme (the lighter of the two options offered; the
"Why Theme 3" column was not added). The §3 secondary
wording (story bar, status strip, provisioning sublabel, conversation hint) is not applied. Web
redeployed 2026-09-13 ~01:19 UTC (evidence `runs/20260913T011857Z_web_deploy/`, verify
`runs/20260913T012106Z_verify_web/`). sandbox-env not yet redeployed; the owner accepted the
description edits and they ship with that service's next deploy, after which the live table shows them.

## 1. Why the landing reads worse than the README

The README opens with one sentence that names the thing, one paragraph that says what happens, and
a four-step "Try it in 60 seconds". The landing page has none of those three:

- **No definition.** The page never says what Faultline is. The headline is a riddle ("What should
  the agent survive today?") and the only name on screen is the sidebar's "agent failure lab".
- **Mechanism before meaning.** The five bullets describe internals ("tool boundary", "acknowledgement
  is lost", "grader outside the agent's reach", "workspace diff", "backend") before the reader knows
  why any of it matters. The README's version of the same content uses plain nouns: a file that
  isn't there, a write that is denied, a write that succeeds but whose response times out.
- **No instructions at reading size.** The only "how to use it" is an 11 px hint under the composer
  and a 12 px right-aligned line above the table. The README's numbered steps are the clearest part
  of the whole project and they are absent here.
- **Bullet 2 is false.** "The environment injects one failure at the tool boundary" — `gauntlet`
  injects three, `missing-config` and `gauntlet` really delete a file at reset, and `worker-crash`
  injects nothing and kills the process for real. (Already flagged in `docs/web-review-findings.md`
  §7.3; the rewrite below supersedes that suggestion.)
- **Code identifiers as prose.** `missing_file`, `denied_write`, `ack_lost`, `EACCES`, `EHARNESS`,
  `gauntlet`, `u_102381d5…` all appear on a page meant for someone who has not opened the repo.
- **One concept, many words.** The page says "turns" in the table, "steps" in the hint, and the app
  says "step" everywhere else. "Failure injected" heads a column that also holds a real crash. The
  badge says "simulated", the header says "injected", the story bar says "intercepted". "Episode",
  "run" and "live run" are used for the same thing.
- **The point is missing.** Nothing on the page states the experiment the project exists for: a
  write that landed but whose reply timed out, and the difference between checking and retrying
  blind. README and RATIONALE both lead with it.
- **Scenario descriptions are the longest text on the page** (40–70 words each) and mix three
  things: the task, the trap, and what a careful agent does. They come from the sandbox-env JSON.

## 2. Proposed landing copy

### Headline and intro (replaces the H1 and the five bullets)

> **An agent harness where the environment fights back.**
>
> A Claude agent edits a small Python repo with real shell commands inside an isolated sandbox.
> Faultline breaks things on purpose: a file goes missing, a write is refused, or a write lands but
> its reply times out. Retry that last one without looking and you can write the change twice. You
> watch every command, see whether the agent recovers, and get a graded score.

Alternative headline if the owner prefers a benefit statement over the README's tagline:
"Watch a Claude agent recover from failures it can't see coming."

### Try it (three numbered steps, body size, above the composer)

1. **Pick a scenario.** Press Run on a row below, or type `/` in the box to choose one and edit
   its task first.
2. **Watch the run.** Every command, its output, the failure when it hits, and the file changes.
   Live runs take one to two minutes. No API quota? Press Replay instead: a recorded run, no
   server needed.
3. **Read the score.** Hidden tests are worth 60 points and recovery checks 40, so finishing the
   task is not enough.

### Composer

| Where | Now | Proposed |
|---|---|---|
| placeholder | Type / to choose a scenario, edit the task if you like, then press enter | Type / to pick a scenario. Its task fills in here; edit it if you like, then press Enter. |
| hint, nothing picked | type / to pick a scenario, or use Run in the table below | or press Run on a row below |
| hint, scenario picked | `{title} · max {n} steps · enter to run` | `{title} · up to {n} steps · Enter to run` |
| hint, resolving | resolving the harness URL… | connecting… |
| hint, checking | checking the harness… (a cold start takes ~10 s) | waking the harness… (first load takes about 10 s) |
| hint, unreachable | harness unreachable — replays in the sidebar still work | live runs are offline — Replay still works |
| seed chip | `seed` with no explanation | tooltip "Seed, recorded with the run" — or hide it: PLAN §3.1 says the seed is stored but unused (fault plans have no random parts), so for a reviewer it is a control that does nothing. |

### Scenario table

| Where | Now | Proposed |
|---|---|---|
| subtitle (move under the title, body size) | Run starts a live episode on the harness · Replay plays a recorded run in the browser | Run starts a live run with the model chosen above. Replay plays a recorded run in the browser and needs no server. |
| subtitle, offline | live runs need the harness; replays still work | Live runs are offline. Replay still works. |
| headers | Scenario · What happens · Max turns · Failure injected · Live run · Replay | Scenario · What goes wrong · Step budget · Failure · Run · Replay |
| under the title | graded on 3 checks + hidden tests | scored on hidden tests + 3 recovery checks |
| that tooltip | raw grader text ("after EACCES on limits.py a later write to it succeeded") | the plain versions that already exist in `src/lib/checks.ts` ("after the denied writes, a later write to the locked file succeeded") |
| fault badges | `missing_file` `denied_write` `ack_lost` `real: worker crash` | words, with the origin the transcript will use: `staged: missing file`, `simulated: write denied`, `simulated: lost ack`, `real: worker crash` (one badge per `faults_public` row, which also fixes `docs/web-review-findings.md` §5) |
| Run disabled title | Harness unreachable | Live runs are offline |
| Replay disabled tooltip | No recording bundled for this scenario yet. | No recording for this scenario yet. |

Badge tooltips (`FAULT_KIND_BLURB` in `src/lib/format.ts`), shortened and without error codes:

- **missing file** — The agent is told the file does not exist. Staged: it really was deleted before
  the run started. Simulated: it is still on disk and the read is refused a few times.
- **write denied** — The first writes to one file are refused with "permission denied". Nothing is
  written until the block lifts.
- **lost ack** — The write happens, but the reply is withheld and comes back as a timeout. The agent
  cannot tell whether it landed.
- **worker crash** — Real, not simulated: the harness process is killed while a write is in flight.
  A fresh worker resumes the run.

### Scenario descriptions (owner: sandbox-env, `scenarios/*.json` `description`)

Same shape for every row: what goes wrong, then what a careful agent does. No error codes.

| id | Proposed description |
|---|---|
| missing-config | config/settings.json is missing, so the tests fail on load. The README documents it, but the first read of the README is refused. A careful agent retries the read, then recreates the file from the docs. |
| locked-file | allowed_burst() is off by one. The first two writes to the file are refused and nothing is written. A careful agent notices, retries a bounded number of times, and re-runs the tests. |
| lost-ack | Bump the version and add a changelog entry. The first changelog write lands, but the reply times out. A careful agent reads the file back before writing again; a careless one adds the entry twice. |
| gauntlet | All three at once: the config is missing, the buggy file is locked, and the changelog write loses its reply. Fix the tests and cut release 0.2.0. |
| worker-crash | Same release task, but the failure is real: the harness process is killed mid-write, and the write lands. A fresh worker resumes the run and tells the agent the outcome is unknown. A careful agent reads the file back before writing again. |

The `task_prompt` strings are what the model sees and should not change in a language pass.

Accepted by the sandbox-env owner (anthropic-75, 2026-09-13) with two rules for any future scenario:
keep this shape ("what goes wrong, then what a careful agent does"), and never put an error code, a
path or a hit count into `task_prompt`, because the agent must not be told the mechanism
(`tests/test_scenarios.py` enforces the no-leak part). They go live with the next
faultline-sandbox-env deploy.

### Sidebar

| Where | Now | Proposed |
|---|---|---|
| group label | Replays | Recorded runs |
| identity row | Browser user | This browser |
| health line | harness reachable / unreachable | harness online / offline |
| key line | no provider key on the api function | no model API key on the web-facing API |

## 3. One word per concept (apply across apps/web)

| Concept | Use | Not |
|---|---|---|
| one model turn with its tool calls | step, step budget | turn, max turns |
| one execution of a scenario | run, live run, recorded run | episode (keep in API paths and logs only) |
| the service that runs the agent | the harness (named once in the intro) | backend, run service |
| where a failure came from | simulated · staged · real (the taxonomy's three words) | injected, intercepted, in UI text |
| the column/badge for all of them | failure | failure injected, fault (fault stays in tooltips and the story bar) |
| the lost-ack mechanism in prose | the reply timed out / the reply was withheld | acknowledgement is lost, ack (the badge keeps "lost ack") |
| the two halves of the score | hidden tests, recovery checks | tests_pass, checks |
| a bundled recording | recorded run (noun); Replay (the button) | demo, bundled run |

Secondary wording elsewhere in the app, lower priority:

- Story bar fault sentence: "The environment intercepted it: simulated: lost ack (write landed;
  response withheld)." Two colons in a row. The label text is prescribed by
  `docs/error-taxonomy.md` ("render verbatim"), so rephrase around it: "The environment intercepted
  it — simulated lost ack: the write landed, the response was withheld."
- Status strip "read-back 1/1 seen" is hard to parse; "read back 1 of 1 faults" reads better and
  the tooltip already says it is a heuristic.
- Provisioning sublabel: "creating an isolated sandbox, copying the fixture repo, applying the
  fault plan" → "creating the sandbox, copying the sample repo, arming the faults".
- Conversation composer hint "lost-ack · another run in this conversation" → "Run lost-ack again in
  this conversation".

## 4. What applying this touches

- `apps/web/src/pages/HomePage.tsx` (intro, steps, hint strings, placeholder), `ScenarioTable.tsx`
  (headers, subtitle, checks tooltip via `checkText`), `FaultBadge.tsx` + `lib/format.ts` (badge
  words, blurbs), `app-sidebar.tsx` (labels, health words); optionally `lib/replay.ts`.
- Tests that pin today's words: `apps/web/e2e/flows.spec.ts` F1 (H1 text, five list items matching
  /Claude agent/, the six header strings) and F2 (`/max 20 steps · enter to run/`); the `web-landing`
  and `web-composer` entries in `.claude/skills/verify-faultline/features/web-ui.md`; PLAN §3.3.2's
  landing bullet. Per the flow map's rule, the map entry and the test change in the same commit.
- Scenario descriptions live in `services/sandbox-env/sandbox_env/scenarios/*.json` and are
  mirrored in PLAN §2.6 and the README scenario list; that is the sandbox-env owner's change.
- Vitest: any component test asserting on the changed strings (check with `pnpm --dir apps/web test`
  after editing).

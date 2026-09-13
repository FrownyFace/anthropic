# Bundled replays

Each `*.json` in this directory is a **`RunRecord`** — exactly the shape
`GET /runs/{id}` returns (`faultline_common.schemas.RunRecord`, mirrored in
`apps/web/src/lib/types.ts`). The UI folds it through the same reducer a live
SSE run goes through, so a replay renders identically to the real thing.

Why they exist: the demo must survive a cold harness, a missing provider key, or
a rate limit at review time. *Replay* on a scenario row (or in the sidebar) needs no
backend and no network beyond fetching this file.

## Playback

`src/hooks/useReplay.ts` folds the first *n* events through the reducer and advances *n* every
`DEMO_STEP_MS` (400 ms, `src/lib/replay.ts`); the scrubber moves *n* in both directions. Register a new
file by adding it to `DEMOS` in `src/lib/replay.ts` (ids match scenario ids, so the scenario table
offers the replay automatically):

```ts
export const DEMOS: DemoMeta[] = [
  { id: 'lost-ack', label: 'lost-ack', file: '/demo/lost-ack.json' },
  { id: 'locked-file', label: 'locked-file', file: '/demo/locked-file.json' },
  { id: 'missing-config', label: 'missing-config', file: '/demo/missing-config.json' },
  { id: 'gauntlet', label: 'gauntlet', file: '/demo/gauntlet.json' },
]
```

The `id` is also the `/replay/<id>` route, so `/replay/lost-ack` deep-links a replay.

## Producing one from a real run

`scripts/export_demo.py` (repo root) reads a run captured under
`runs/<ts>_<run_id>/run.json` and writes a demo file here:

```bash
# after scripts/run_episode_cli.py has written runs/20260912T173000Z_r_abc/run.json
python3 scripts/export_demo.py runs/20260912T173000Z_r_abc
# -> apps/web/public/demo/lost-ack.json   (named after the run's scenario_id)

python3 scripts/export_demo.py runs/20260912T173000Z_r_abc --out-name gauntlet --pretty
python3 scripts/export_demo.py runs/20260912T173000Z_r_abc --check   # validate only
```

It validates the record (ids contiguous from 0, `run_id` consistent, required
fields present), strips nothing but obvious secrets (`anthropic_workspace` is
masked), and refuses to write a record with no `episode.evaluated` event unless
`--allow-unevaluated` is passed — an unfinished run makes a poor demo.

## What is shipped today

- **`lost-ack.json`** — a real capture: `claude-haiku-4-5` on the deployed stack
  (run `r_e9bc5c8c6739`, 2026-09-12, 48 events, score 100/100), exported with
  `scripts/export_demo.py` from `runs/20260912T215923Z_lost-ack_live/run.json`.
  The story: read `CHANGELOG.md` → write it → the ack is lost (`504`, `ack_lost`
  fault at step 4) → the agent **reads back instead of retrying** → the section is
  already there, so no duplicate write → bump `version.py` → `pytest` green →
  `submit` → graded 100/100.

- **`locked-file.json`** (run `r_0c2184dd9727`), **`missing-config.json`**
  (`r_62416121aefd`) and **`gauntlet.json`** (`r_39c78b2b3a0b`) — real captures on
  the same stack, each graded 100 with its faults fired.

`src/lib/demo.test.ts` checks every bundled replay on each `pnpm test` run
(contiguous ids, graded, at least one fault, no secrets), so a replay cannot rot
silently. All four predate the reliability fields (`outcome`, `error_class`,
`evaluation_status`); re-export once the backend emits them. There is no
`worker-crash` replay yet.

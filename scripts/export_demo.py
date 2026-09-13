#!/usr/bin/env python3
"""Export a captured run as a bundled browser replay.

    python3 scripts/export_demo.py runs/20260912T173000Z_r_abc
    python3 scripts/export_demo.py runs/<dir> --out-name gauntlet --pretty
    python3 scripts/export_demo.py runs/<dir> --check

Reads ``<run_dir>/run.json`` (a ``faultline_common.schemas.RunRecord``, written by
``scripts/run_episode_cli.py``) and writes ``apps/web/public/demo/<scenario_id>.json``.

The browser folds that file through the same reducer a live SSE run goes through, so the replay is
pixel-identical to the real run — see ``apps/web/public/demo/README.md``. Register the new file in
``apps/web/src/lib/replay.ts`` (``DEMOS``) so the home page offers it.

Owned by the apps/web workstream; writes only into apps/web/public/demo/.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = REPO_ROOT / "apps" / "web" / "public" / "demo"

# Keep the shared logger optional: this script must run from a bare checkout.
sys.path.insert(0, str(REPO_ROOT / "packages" / "common"))
try:
    from faultline_common.log import get_logger  # type: ignore

    log = get_logger("script")
except Exception:  # pragma: no cover - fallback when the shared package is absent

    class _Fallback:
        def _emit(self, lvl: str, ev: str, msg: str, **kw: Any) -> None:
            rec = {"svc": "script", "lvl": lvl, "ev": ev, "msg": msg, **kw}
            print(json.dumps(rec, default=str), file=sys.stderr)

        def info(self, ev: str, msg: str = "", **kw: Any) -> None:
            self._emit("info", ev, msg, **kw)

        def warn(self, ev: str, msg: str = "", **kw: Any) -> None:
            self._emit("warn", ev, msg, **kw)

        def error(self, ev: str, msg: str = "", **kw: Any) -> None:
            self._emit("error", ev, msg, **kw)

    log = _Fallback()

REQUIRED_RECORD_FIELDS = ("run_id", "status", "scenario_id", "model", "created_at", "events")
EVENT_TYPES = {
    "run.started",
    "episode.reset",
    "turn.text",
    "tool.call",
    "tool.result",
    "fault.fired",
    "workspace.diff",
    "episode.evaluated",
    "run.finished",
    "log",
    # added once the harness started emitting them (faultline_common.schemas.EventType)
    "llm.call",
    "turn.thinking",
}


class ExportError(RuntimeError):
    pass


def load_record(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run.json" if run_dir.is_dir() else run_dir
    if not path.exists():
        raise ExportError(f"{path} not found — pass a runs/<ts>_<run_id> directory or a run.json")
    try:
        rec = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ExportError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(rec, dict):
        raise ExportError(f"{path} does not contain a RunRecord object")
    log.info("demo.read", "loaded run record", path=str(path), bytes=path.stat().st_size)
    return rec


def validate(rec: dict[str, Any], *, allow_unevaluated: bool) -> list[str]:
    """Return a list of problems. Empty means the record is fit to ship."""
    problems: list[str] = []

    for field in REQUIRED_RECORD_FIELDS:
        if field not in rec:
            problems.append(f"missing RunRecord.{field}")

    events = rec.get("events")
    if not isinstance(events, list) or not events:
        problems.append("RunRecord.events is empty — nothing to replay")
        return problems

    run_id = rec.get("run_id")
    seen_types: set[str] = set()
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            problems.append(f"event[{i}] is not an object")
            continue
        if ev.get("id") != i:
            problems.append(f"event[{i}].id is {ev.get('id')!r}; ids must run 0..n-1 (SSE Last-Event-ID)")
        if run_id is not None and ev.get("run_id") != run_id:
            problems.append(f"event[{i}].run_id {ev.get('run_id')!r} != record run_id {run_id!r}")
        if not isinstance(ev.get("ts"), str):
            problems.append(f"event[{i}].ts is missing or not a string")
        etype = ev.get("type")
        if etype not in EVENT_TYPES:
            problems.append(f"event[{i}].type {etype!r} is not a known EventType")
        else:
            seen_types.add(etype)
        if not isinstance(ev.get("data"), dict):
            problems.append(f"event[{i}].data must be an object")

    if "episode.evaluated" not in seen_types and not allow_unevaluated:
        problems.append(
            "no episode.evaluated event — an unfinished run makes a poor demo "
            "(pass --allow-unevaluated to ship it anyway)"
        )
    return problems


def sanitise(rec: dict[str, Any]) -> dict[str, Any]:
    """Mask the few fields that could carry account identifiers into a public bundle."""
    out = json.loads(json.dumps(rec))  # deep copy via round-trip
    masked = 0

    def mask(d: dict[str, Any]) -> None:
        nonlocal masked
        for key in ("anthropic_workspace", "api_key", "authorization"):
            if key in d and d[key]:
                d[key] = "****"
                masked += 1

    mask(out)
    for ev in out.get("events", []):
        data = ev.get("data")
        if isinstance(data, dict):
            mask(data)
    # sandbox ids are ops detail, not part of the story
    for ev in out.get("events", []):
        data = ev.get("data")
        if isinstance(data, dict) and "sandbox_id" in data:
            data.pop("sandbox_id", None)
    if masked:
        log.info("demo.sanitised", "masked account fields", fields=masked)
    return out


def summarise(rec: dict[str, Any]) -> dict[str, Any]:
    events = rec.get("events", [])
    counts: dict[str, int] = {}
    for ev in events:
        t = ev.get("type", "?")
        counts[t] = counts.get(t, 0) + 1
    evaluation = rec.get("evaluation") or {}
    return {
        "run_id": rec.get("run_id"),
        "scenario_id": rec.get("scenario_id"),
        "model": rec.get("model"),
        "status": rec.get("status"),
        "events": len(events),
        "event_types": counts,
        "score": evaluation.get("score"),
        "passed": evaluation.get("passed"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path, help="runs/<ts>_<run_id> directory (or a run.json path)")
    ap.add_argument("--out-name", help="basename without .json (default: the run's scenario_id)")
    ap.add_argument("--out-dir", type=Path, default=DEMO_DIR, help=f"default: {DEMO_DIR}")
    ap.add_argument("--pretty", action="store_true", help="indent the JSON (bigger file, readable diff)")
    ap.add_argument("--check", action="store_true", help="validate only; write nothing")
    ap.add_argument("--allow-unevaluated", action="store_true", help="ship a run with no episode.evaluated")
    ap.add_argument("--force", action="store_true", help="overwrite an existing demo file")
    args = ap.parse_args(argv)

    try:
        rec = load_record(args.run_dir)
    except ExportError as e:
        log.error("demo.error", str(e))
        return 2

    problems = validate(rec, allow_unevaluated=args.allow_unevaluated)
    if problems:
        for p in problems:
            log.error("demo.invalid", p)
        return 1

    summary = summarise(rec)
    log.info("demo.valid", "record passed validation", **summary)

    if args.check:
        print(json.dumps(summary, indent=2))
        return 0

    name = args.out_name or str(rec.get("scenario_id") or "run")
    out_path = args.out_dir / f"{name}.json"
    if out_path.exists() and not args.force:
        log.error("demo.exists", f"{out_path} already exists; pass --force to overwrite")
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = sanitise(rec)
    text = json.dumps(payload, indent=2) if args.pretty else json.dumps(payload)
    out_path.write_text(text + "\n")

    log.info("demo.written", "demo bundled", path=str(out_path), bytes=out_path.stat().st_size, **summary)
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes, {summary['events']} events)")
    print(
        "remember: add it to DEMOS in apps/web/src/lib/replay.ts so the home page offers it, "
        "then `pnpm test` in apps/web."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

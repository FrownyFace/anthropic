#!/usr/bin/env python
"""Measure sandbox-env latency against a live deployment. No model, no secrets.

    services/sandbox-env/.venv/bin/python services/sandbox-env/tools/perf_probe.py \
        --samples 5 --out runs/<ts>_perf

What it measures, per sample, from a plain HTTP/MCP client (so the numbers are what the *harness*
experiences, not what the server thinks it spent):

    reset          POST /episodes                     sandbox create + fixture upload + baseline
    list_dir       MCP list_dir  {"path": "."}
    read_file      MCP read_file {"path": "README.md"}
    write_file     MCP write_file to a scratch path   (never a fault target)
    run_command    MCP run_command "echo … && ls"     one shell round trip
    run_pytest     MCP run_command "python -m pytest -q"
    observe        GET /episodes/{id}                 sha map + diff of the whole tree
    evaluate       POST /episodes/{id}/evaluate       upload hidden tests, run pytest, remove
    delete         DELETE /episodes/{id}              terminate the sandbox
    list_episodes  GET /episodes?probe=true           the ops listing (one poll per live episode)

Each sample is a *fresh episode*, so `reset` is measured cold every time and the per-tool numbers
are not biased by a warm workspace someone else left behind. The scenario is `lost-ack`, whose only
fault targets CHANGELOG.md — every path this probe touches is fault-free, so the timings are the
system's real cost and not a 3 s injected delay.

Every episode is deleted in a `finally`, and the probe re-lists at the end to prove none leaked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
for _p in (str(REPO_ROOT / "packages" / "common"), str(HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from faultline_common.log import get_logger  # noqa: E402

log = get_logger("script")

DEFAULT_BASE = "https://appliedlabsai-local--faultline-sandbox-env-api.modal.run"
SCENARIO = "lost-ack"
SCRATCH = "notes/perf_probe.txt"
README = "README.md"


def pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile — honest for n=5, where interpolation invents precision."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * len(ordered) + 0.5) - 1))
    return ordered[idx]


def summarise(name: str, samples: list[float]) -> dict[str, Any]:
    return {
        "op": name,
        "n": len(samples),
        "min_ms": round(min(samples), 1),
        "p50_ms": round(statistics.median(samples), 1),
        "mean_ms": round(statistics.fmean(samples), 1),
        "p95_ms": round(pct(samples, 0.95), 1),
        "max_ms": round(max(samples), 1),
        "samples_ms": [round(s, 1) for s in samples],
    }


class Timer:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()

    @property
    def ms(self) -> float:
        return (time.perf_counter() - self.t0) * 1000


async def one_sample(base: str, idx: int, timings: dict[str, list[float]],
                     server_ms: dict[str, list[float]]) -> dict[str, Any]:
    detail: dict[str, Any] = {"sample": idx}
    async with httpx.AsyncClient(base_url=base, timeout=120) as http:
        t = Timer()
        r = await http.post("/episodes", json={"scenario_id": SCENARIO})
        r.raise_for_status()
        reset = r.json()
        timings["reset"].append(t.ms)
        detail["reset_ms"] = round(t.ms, 1)
        episode_id = reset["episode_id"]
        detail["episode_id"] = episode_id
        detail["sandbox_id"] = reset.get("sandbox_id")
        detail["files"] = len(reset.get("files", []))

        transport = StreamableHttpTransport(
            url=base.rstrip("/") + "/mcp",
            headers={"X-Faultline-Episode": episode_id, "X-Request-Id": f"perf-{idx}"},
        )
        try:
            async with Client(transport) as mcp:
                async def tool(op: str, name: str, args: dict[str, Any]) -> Any:
                    tt = Timer()
                    res = await mcp.call_tool(name, args, timeout=60, raise_on_error=False)
                    timings[op].append(tt.ms)
                    detail[f"{op}_ms"] = round(tt.ms, 1)
                    body = res.content[0].text if res.content else "{}"
                    try:
                        parsed = json.loads(body)
                    except json.JSONDecodeError:
                        parsed = {"raw": body[:200]}
                    if isinstance(parsed, dict) and "duration_ms" in parsed:
                        server_ms.setdefault(op, []).append(float(parsed["duration_ms"]))
                    if res.is_error:
                        detail.setdefault("errors", []).append({op: body[:200]})
                    return parsed

                tl = Timer()
                await mcp.list_tools()
                timings["mcp_list_tools"].append(tl.ms)

                await tool("list_dir", "list_dir", {"path": "."})
                await tool("read_file", "read_file", {"path": README})
                await tool("write_file", "write_file",
                           {"path": SCRATCH, "content": f"perf probe sample {idx}\n",
                            "mode": "overwrite"})
                await tool("run_command", "run_command", {"command": "echo hello && ls -1"})
                await tool("run_pytest", "run_command", {"command": "python -m pytest -q"})

            t = Timer()
            obs = await http.get(f"/episodes/{episode_id}")
            obs.raise_for_status()
            timings["observe"].append(t.ms)
            detail["observe_ms"] = round(t.ms, 1)

            t = Timer()
            ev = await http.post(f"/episodes/{episode_id}/evaluate")
            ev.raise_for_status()
            timings["evaluate"].append(t.ms)
            detail["evaluate_ms"] = round(t.ms, 1)
            detail["score"] = ev.json().get("score")

            t = Timer()
            ls = await http.get("/episodes", params={"probe": "true", "limit": 200})
            ls.raise_for_status()
            timings["list_episodes"].append(t.ms)
            detail["list_episodes_ms"] = round(t.ms, 1)
            detail["episodes_in_dict"] = ls.json().get("count")
            detail["episodes_live"] = ls.json().get("live")
        finally:
            t = Timer()
            d = await http.delete(f"/episodes/{episode_id}")
            timings["delete"].append(t.ms)
            detail["delete_ms"] = round(t.ms, 1)
            detail["deleted"] = d.status_code == 200 and d.json().get("terminated") is True

    log.info("perf.sample", f"sample {idx} done", **{k: v for k, v in detail.items()
                                                     if k.endswith("_ms") or k in ("episode_id", "score")})
    return detail


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(args.out) if args.out else REPO_ROOT / "runs" / f"{stamp}_perf"
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.mkdir(parents=True, exist_ok=True)

    ops = ["reset", "mcp_list_tools", "list_dir", "read_file", "write_file", "run_command",
           "run_pytest", "observe", "evaluate", "list_episodes", "delete"]
    timings: dict[str, list[float]] = {op: [] for op in ops}
    server_ms: dict[str, list[float]] = {}
    samples: list[dict[str, Any]] = []

    async with httpx.AsyncClient(base_url=args.base, timeout=60) as http:
        health = (await http.get("/health")).json()

    t0 = time.perf_counter()
    for i in range(1, args.samples + 1):
        samples.append(await one_sample(args.base, i, timings, server_ms))
    total_s = round(time.perf_counter() - t0, 1)

    async with httpx.AsyncClient(base_url=args.base, timeout=120) as http:
        after = (await http.get("/episodes", params={"probe": "true", "limit": 500})).json()

    report = {
        "base": args.base,
        "scenario": SCENARIO,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "samples": args.samples,
        "duration_s": total_s,
        "health": health,
        "latency_ms": {op: summarise(op, timings[op]) for op in ops if timings[op]},
        "sandbox_reported_ms": {op: summarise(op, v) for op, v in server_ms.items()},
        "per_sample": samples,
        "cleanup": {
            "all_deleted": all(s.get("deleted") for s in samples),
            "episodes_live_after": after.get("live"),
            "episodes_in_dict_after": after.get("count"),
            # split ours from everyone else's: this deployment is shared, and another session's
            # live episode is not a leak of ours.
            "mine_live_after": [e["episode_id"] for e in after.get("episodes", [])
                                if e.get("alive") and e["episode_id"] in
                                {s.get("episode_id") for s in samples}],
            "other_sessions_live": [e["episode_id"] for e in after.get("episodes", [])
                                    if e.get("alive") and e["episode_id"] not in
                                    {s.get("episode_id") for s in samples}],
        },
    }
    (out / "latency.json").write_text(json.dumps(report, indent=2))

    width = max(len(o) for o in report["latency_ms"])
    print(f"\nsandbox-env latency, {args.samples} samples, {args.base}")
    print(f"{'op'.ljust(width)}   n    min     p50    mean     max")
    for op, s in report["latency_ms"].items():
        print(f"{op.ljust(width)} {s['n']:>3} {s['min_ms']:>7.1f} {s['p50_ms']:>7.1f} "
              f"{s['mean_ms']:>7.1f} {s['max_ms']:>7.1f}")
    print(f"\ncleanup: all_deleted={report['cleanup']['all_deleted']} "
          f"mine_live_after={report['cleanup']['mine_live_after']} "
          f"(other sessions live: {len(report['cleanup']['other_sessions_live'])})")
    print(f"evidence: {out / 'latency.json'}")
    return 0 if report["cleanup"]["all_deleted"] and not report["cleanup"]["mine_live_after"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

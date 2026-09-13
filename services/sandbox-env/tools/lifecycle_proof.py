#!/usr/bin/env python
"""Prove the cost guardrails against the LIVE deployment. No model, no secrets.

    services/sandbox-env/.venv/bin/python services/sandbox-env/tools/lifecycle_proof.py \
        --out runs/<ts>_lifecycle

Four claims, each checked against the deployed service rather than a fake:

    A  `GET /episodes` lists real episodes and tells the truth about sandbox liveness.
    B  the TTL sweep terminates an abandoned episode (one aged past EPISODE_TTL_S) and nothing else.
    C  `reap(keep_active=True)` terminates a stray and SPARES episodes that are still live —
       this is the safety property that the `r_ccda8780cbee` incident (a reap killing a running
       run's sandbox) says we need.
    D  a reaped sandbox is written back to the episode Dict, so the ops listing never keeps
       claiming a dead sandbox is alive.

To age an episode out, the script edits ONLY the episodes it created itself, directly in the
`faultline-episodes` Dict — the same thing "an owner that never came back" produces, without
waiting 30 minutes for it. Every episode it creates is deleted at the end, whatever happens.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
for _p in (str(REPO_ROOT / "packages" / "common"), str(HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from faultline_common.log import get_logger  # noqa: E402

log = get_logger("script")

DEFAULT_BASE = "https://appliedlabsai-local--faultline-sandbox-env-api.modal.run"
APP = "faultline-sandbox-env"
ENV = "local"
DICT_NAME = "faultline-episodes"


class Proof:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.checks: list[dict[str, Any]] = []
        self.record: dict[str, Any] = {"started_at": now_iso(), "checks": self.checks}

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        log.info("proof.check", f"{name}", check=name, ok=bool(ok), detail=str(detail)[:400])
        return bool(ok)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c["ok"] for c in self.checks)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_ago(seconds: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def listing(http: httpx.Client, probe: bool = True) -> dict[str, Any]:
    r = http.get("/episodes", params={"probe": str(probe).lower(), "limit": 500}, timeout=180)
    r.raise_for_status()
    return r.json()


def row_of(listing_body: dict[str, Any], episode_id: str) -> dict[str, Any] | None:
    for e in listing_body.get("episodes", []):
        if e["episode_id"] == episode_id:
            return e
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(args.out) if args.out else REPO_ROOT / "runs" / f"{stamp}_lifecycle"
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    proof = Proof(out)

    import modal

    episodes_dict = modal.Dict.from_name(DICT_NAME, environment_name=ENV, create_if_missing=True)
    reap_fn = modal.Function.from_name(APP, "reap", environment_name=ENV)
    sweep_fn = modal.Function.from_name(APP, "sweep", environment_name=ENV)

    created: list[str] = []
    http = httpx.Client(base_url=args.base, timeout=180)
    try:
        # ---- A: the ops listing sees a real, live episode -----------------------------------
        stray_a = http.post("/episodes", json={"scenario_id": "lost-ack"}).json()
        created.append(stray_a["episode_id"])
        keeper = http.post("/episodes", json={"scenario_id": "lost-ack"}).json()
        created.append(keeper["episode_id"])
        proof.record["episodes"] = {"swept": stray_a["episode_id"], "keeper": keeper["episode_id"]}

        before = listing(http)
        proof.record["listing_before"] = {"count": before["count"], "live": before["live"],
                                          "ttl_s": before["ttl_s"]}
        ra, rk = row_of(before, stray_a["episode_id"]), row_of(before, keeper["episode_id"])
        proof.check("A.listing_has_both_episodes", ra is not None and rk is not None)
        proof.check("A.fresh_episodes_report_alive", bool(ra and ra["alive"]) and bool(rk and rk["alive"]),
                    {"stray": ra, "keeper": rk})
        proof.check("A.listing_reports_the_ttl", before["ttl_s"] > 0, before["ttl_s"])

        # ---- B: the TTL sweep reclaims an abandoned episode ----------------------------------
        ep = episodes_dict.get(stray_a["episode_id"])
        ep["created_at"] = iso_ago(before["ttl_s"] + 600)  # "abandoned 10 min past the TTL"
        episodes_dict.put(stray_a["episode_id"], ep)

        swept = sweep_fn.remote()
        proof.record["sweep"] = swept
        proof.check("B.sweep_expired_the_abandoned_episode",
                    stray_a["episode_id"] in swept["expired"], swept["expired"])
        proof.check("B.sweep_spared_the_live_episode",
                    keeper["episode_id"] not in swept["expired"], keeper["episode_id"])

        after_sweep = listing(http)
        ra, rk = row_of(after_sweep, stray_a["episode_id"]), row_of(after_sweep, keeper["episode_id"])
        proof.check("B.swept_sandbox_is_dead", bool(ra) and ra["alive"] is False and ra["terminated"], ra)
        proof.check("B.keeper_sandbox_still_alive", bool(rk) and rk["alive"] is True, rk)

        # ---- C/D: reap spares live episodes, kills strays, and records what it killed --------
        stray_c = http.post("/episodes", json={"scenario_id": "missing-config"}).json()
        created.append(stray_c["episode_id"])
        proof.record["episodes"]["reaped"] = stray_c["episode_id"]
        ep = episodes_dict.get(stray_c["episode_id"])
        ep["created_at"] = iso_ago(before["ttl_s"] + 600)
        episodes_dict.put(stray_c["episode_id"], ep)

        dry = reap_fn.remote(keep_active=True, dry_run=True)
        proof.record["reap_dry_run"] = dry
        proof.check("C.dry_run_targets_the_stray",
                    stray_c["sandbox_id"] in dry["terminated"], dry["terminated"][:10])
        proof.check("C.dry_run_spares_the_live_sandbox",
                    keeper["sandbox_id"] in dry["skipped"], dry["skipped"][:10])

        live_before_reap = listing(http)["live"]
        reaped = reap_fn.remote(keep_active=True)
        proof.record["reap"] = reaped
        proof.check("C.reap_terminated_the_stray",
                    stray_c["sandbox_id"] in reaped["terminated"], reaped["terminated"][:10])
        proof.check("C.reap_spared_the_live_sandbox",
                    keeper["sandbox_id"] in reaped["skipped"], reaped["skipped"][:10])
        proof.check("D.reap_marked_the_episode_dict",
                    stray_c["episode_id"] in reaped["episodes_marked"], reaped["episodes_marked"])

        after_reap = listing(http)
        rc, rk = row_of(after_reap, stray_c["episode_id"]), row_of(after_reap, keeper["episode_id"])
        proof.check("D.reaped_episode_reads_as_dead",
                    bool(rc) and rc["alive"] is False and rc["terminated"] is True, rc)
        proof.check("C.keeper_survived_the_reap", bool(rk) and rk["alive"] is True, rk)
        proof.record["listing_after"] = {"count": after_reap["count"], "live": after_reap["live"],
                                         "live_before_reap": live_before_reap}

        # keeper still works after all of that
        obs = http.get(f"/episodes/{keeper['episode_id']}")
        proof.check("C.keeper_is_still_usable", obs.status_code == 200, obs.status_code)
    finally:
        deletes = {}
        for eid in created:
            try:
                r = http.delete(f"/episodes/{eid}")
                deletes[eid] = r.json() if r.status_code == 200 else r.status_code
            except Exception as exc:  # noqa: BLE001
                deletes[eid] = f"{type(exc).__name__}: {exc}"
        proof.record["deletes"] = deletes
        proof.check("cleanup.every_episode_deleted",
                    all(isinstance(v, dict) and v.get("terminated") for v in deletes.values()),
                    deletes)
        final = listing(http)
        mine_live = [e["episode_id"] for e in final["episodes"]
                     if e.get("alive") and e["episode_id"] in created]
        proof.check("cleanup.none_of_my_sandboxes_left_running", not mine_live, mine_live)
        proof.record.update(finished_at=now_iso(), PROOF=proof.passed,
                            other_sessions_live=[e["episode_id"] for e in final["episodes"]
                                                 if e.get("alive") and e["episode_id"] not in created])
        (out / "lifecycle_proof.json").write_text(json.dumps(proof.record, indent=2, default=str))
        http.close()

    ok = sum(1 for c in proof.checks if c["ok"])
    print(f"\n{'LIFECYCLE PROOF PASS' if proof.passed else 'LIFECYCLE PROOF FAIL'} :: "
          f"{ok}/{len(proof.checks)} checks, out={out / 'lifecycle_proof.json'}")
    for c in proof.checks:
        if not c["ok"]:
            print(f"  ! {c['name']}: {c['detail']}")
    return 0 if proof.passed else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Drive the deployed Faultline web UI the way a reviewer does and keep independent evidence.

    python3 .claude/skills/verify-faultline/helpers/verify_web.py [--live] [--skip-flows] [--grep F5] [--base-url URL]

Stages (a failed check never stops evidence collection):
  doctor    HTTP probes of the site: root/no-store, config.json -> healthy harness, SPA fallback,
            404 assets never cached, served bundle == local build, every bundled replay valid
  flows     apps/web/e2e/flows.spec.ts through Playwright in the installed Google Chrome, one test
            per flow id in features/web-ui.md (F1..F11); --live also runs F3/F4 (one Haiku episode)
  survival  manifest.json (sha256 of every evidence file), then verification.json written LAST

Evidence layout under runs/<UTC ts>_verify_web/ (parallel to verify_backend.py):
  commands.log        one JSON line per HTTP call and subprocess, in order
  outputs/            raw HTTP bodies; live.json / conversation.json / run.json for --live
  screens/            one full-page screenshot per flow
  artifacts/          Playwright traces + failure screenshots
  playwright.json     raw Playwright JSON reporter output;  playwright.stdout.txt the console
  scores.json         the live run's score/checks/tests (only with --live)
  verification.json   pass/fail per check, run_id/conversation_id of a browser-started run, overall
  manifest.json       sha256 of every evidence file (before verification.json is written)

Stdlib only. Needs Node >= 22 (found via PATH or ~/.nvm) and apps/web/node_modules (pnpm 9.15.4).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
WEB = REPO / "apps" / "web"
DEFAULT_BASE_URL = "https://appliedlabsai-local--faultline-web-site.us-east.modal.direct"
FLOW_IDS = ["F1", "F2", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F3", "F4", "F4b"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Evidence:
    def __init__(self, out: Path):
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        for sub in ("outputs", "screens", "artifacts"):
            (self.out / sub).mkdir(exist_ok=True)
        self.log = (self.out / "commands.log").open("a", encoding="utf-8")
        self.results: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}

    def cmd(self, kind: str, **fields: Any) -> None:
        self.log.write(json.dumps({"ts": now(), "kind": kind, **fields}, default=str) + "\n")
        self.log.flush()

    def check(self, stage: str, name: str, ok: bool | None, detail: str = "") -> bool | None:
        status = "skipped" if ok is None else ("PASS" if ok else "FAIL")
        self.results.append({"stage": stage, "name": f"{stage}.{name}", "ok": ok, "status": status, "detail": detail, "ts": now()})
        print(f"  [{status:7}] {stage}.{name}{(' — ' + detail) if detail else ''}")
        return ok

    def write_output(self, name: str, body: Any) -> None:
        p = self.out / "outputs" / name
        if isinstance(body, (bytes, bytearray)):
            p.write_bytes(body)
        elif isinstance(body, str):
            p.write_text(body)
        else:
            p.write_text(json.dumps(body, indent=2, default=str))


def http(ev: Evidence, method: str, url: str, headers: dict[str, str] | None = None, timeout: float = 60) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            status = resp.status
    except urllib.error.HTTPError as e:
        body = e.read()
        hdrs = {k.lower(): v for k, v in e.headers.items()}
        status = e.code
    except Exception as e:  # network failure
        ev.cmd("http", method=method, url=url, error=str(e), dur_ms=int((time.perf_counter() - t0) * 1000))
        return 0, {}, b""
    ev.cmd("http", method=method, url=url, status=status, bytes=len(body), cache_control=hdrs.get("cache-control"), dur_ms=int((time.perf_counter() - t0) * 1000))
    return status, hdrs, body


# ----------------------------------------------------------------------------- doctor

def demo_ids() -> list[str]:
    src = (WEB / "src" / "lib" / "replay.ts").read_text()
    block = src[src.index("export const DEMOS"):]
    block = block[: block.index("\n]")]
    return re.findall(r"id:\s*'([a-z0-9-]+)'", block)


def local_bundle() -> str | None:
    js = sorted(glob.glob(str(WEB / "dist" / "assets" / "index-*.js")))
    return Path(js[-1]).name if js else None


def doctor(ev: Evidence, base: str) -> None:
    print("doctor")
    # A Vite dev server (localhost) has no serve.py: cache headers, the served bundle and the
    # missing-asset policy are deployment properties and are reported as skipped there.
    deployed = not re.match(r"https?://(localhost|127\.0\.0\.1)(:|/|$)", base)
    ev.meta["deployed_target"] = deployed
    status, hdrs, body = http(ev, "GET", f"{base}/")
    html = body.decode("utf-8", "replace")
    ev.write_output("index.html", html)
    ev.check("http", "root_200", status == 200, f"status {status}")
    ev.check("http", "root_no_store", ("no-store" in hdrs.get("cache-control", "")) if deployed else None, hdrs.get("cache-control", "(none)") if deployed else "dev server")
    ev.check("http", "root_has_theme_prepaint", "faultline.theme" in html, "inline theme script in index.html")
    served = re.search(r"index-[A-Za-z0-9_-]+\.js", html)
    served_name = served.group(0) if served else None
    local = local_bundle()
    ev.meta["served_bundle"], ev.meta["local_bundle"] = served_name, local
    if deployed and local and served_name != local:
        # a Server container can lag a deploy by ~30 s; give it a chance before failing
        for _ in range(4):
            time.sleep(15)
            status, hdrs, body = http(ev, "GET", f"{base}/")
            m = re.search(r"index-[A-Za-z0-9_-]+\.js", body.decode("utf-8", "replace"))
            served_name = m.group(0) if m else None
            if served_name == local:
                break
    ev.check("http", "bundle_matches_local", ((local is None) or served_name == local) if deployed else None, f"served {served_name} local {local or '(no local build)'}" if deployed else "dev server")

    status, hdrs, body = http(ev, "GET", f"{base}/config.json")
    cfg: dict[str, Any] = {}
    try:
        cfg = json.loads(body)
    except Exception:
        pass
    ev.write_output("config.json", body)
    harness = str(cfg.get("harnessUrl", "")).rstrip("/")
    ev.meta["harness_url"] = harness
    ev.check("http", "config_json", status == 200 and harness.startswith("https://"), f"harnessUrl={harness or '(missing)'}")
    ev.check("http", "config_no_store", ("no-store" in hdrs.get("cache-control", "")) if deployed else None, hdrs.get("cache-control", "(none)") if deployed else "dev server")
    if harness:
        status, hdrs, body = http(ev, "GET", f"{harness}/health")
        ev.write_output("harness_health.json", body)
        try:
            h = json.loads(body)
        except Exception:
            h = {}
        ev.check("http", "harness_health_ok", status == 200 and h.get("ok") is True, f"status {status}")
        ev.check("http", "harness_no_provider_key", h.get("has_provider_key") is False, f"has_provider_key={h.get('has_provider_key')}")
        store = (h.get("detail") or {}).get("store") or {}
        ev.meta["harness_store"] = store
        ev.check("http", "harness_store_reported", bool(store), f"store={'ok' if store.get('ok') else 'absent/not ok'}")

    status, hdrs, _ = http(ev, "GET", f"{base}/conversations/c_does_not_exist")
    ev.check("http", "spa_fallback_200", status == 200, f"status {status}")
    ev.check("http", "spa_fallback_no_store", ("no-store" in hdrs.get("cache-control", "")) if deployed else None, hdrs.get("cache-control", "(none)") if deployed else "dev server")

    status, hdrs, _ = http(ev, "GET", f"{base}/assets/does-not-exist-{int(time.time())}.js")
    ev.check("http", "missing_asset_404", (status == 404) if deployed else None, f"status {status}" if deployed else "dev server")
    ev.check("http", "missing_asset_never_cached", ("no-store" in hdrs.get("cache-control", "") and "immutable" not in hdrs.get("cache-control", "")) if deployed else None, hdrs.get("cache-control", "(none)") if deployed else "dev server")

    ids = demo_ids()
    ev.meta["demo_ids"] = ids
    ev.check("http", "demos_registered", len(ids) >= 4, f"DEMOS={ids}")
    for d in ids:
        status, _, body = http(ev, "GET", f"{base}/demo/{d}.json")
        ok = status == 200
        detail = f"status {status}"
        if ok:
            try:
                rec = json.loads(body)
                events = rec.get("events", [])
                contiguous = all(e.get("id") == i for i, e in enumerate(events))
                evaluated = bool(rec.get("evaluation")) and rec["evaluation"].get("score") is not None
                ok = bool(events) and contiguous and evaluated and rec.get("scenario_id") == d
                detail = f"{len(events)} events, contiguous={contiguous}, score={(rec.get('evaluation') or {}).get('score')}"
                ev.write_output(f"demo_{d}.summary.json", {"run_id": rec.get("run_id"), "status": rec.get("status"), "events": len(events), "score": (rec.get("evaluation") or {}).get("score")})
            except Exception as e:
                ok, detail = False, f"unparseable: {e}"
        ev.check("http", f"demo_{d}", ok, detail)


# ----------------------------------------------------------------------------- flows

def node_bin_dir() -> Path | None:
    """Return a bin dir with node >= 22 (PATH first, then nvm), or None."""
    def major(node: str) -> int:
        try:
            v = subprocess.run([node, "-v"], capture_output=True, text=True, timeout=10).stdout.strip()
            return int(v.lstrip("v").split(".")[0])
        except Exception:
            return 0
    on_path = shutil.which("node")
    if on_path and major(on_path) >= 22:
        return Path(on_path).parent
    for pat in ("v22.*", "v24.*", "v2[3-9].*"):
        for d in sorted(glob.glob(str(Path.home() / ".nvm" / "versions" / "node" / pat)), reverse=True):
            node = Path(d) / "bin" / "node"
            if node.exists() and major(str(node)) >= 22:
                return node.parent
    return None


def run_flows(ev: Evidence, base: str, live: bool, grep: str | None, channel: str) -> None:
    print("flows")
    bin_dir = node_bin_dir()
    if bin_dir is None:
        ev.check("flows", "node22_available", False, "no node >= 22 on PATH or under ~/.nvm")
        return
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    pnpm = shutil.which("pnpm", path=env["PATH"]) or "pnpm"
    env.update({
        "FAULTLINE_WEB_URL": base,
        "FAULTLINE_LIVE": "1" if live else "0",
        "PW_JSON_OUT": str(ev.out / "playwright.json"),
        "PW_OUTPUT_DIR": str(ev.out / "artifacts"),
        "PW_SCREEN_DIR": str(ev.out / "screens"),
        "PW_OUTPUTS_DIR": str(ev.out / "outputs"),
        "PW_CHANNEL": channel,
        "CI": "1",
    })
    cmd = [pnpm, "exec", "playwright", "test"]  # reporters come from playwright.config.ts (list + json → PW_JSON_OUT)
    if grep:
        cmd += ["--grep", grep]
    ev.check("flows", "node22_available", True, f"{bin_dir} ({pnpm})")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=WEB, env=env, capture_output=True, text=True, timeout=20 * 60)
    (ev.out / "playwright.stdout.txt").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    ev.cmd("subprocess", cmd=cmd, cwd=str(WEB), rc=proc.returncode, dur_ms=int((time.perf_counter() - t0) * 1000))

    report_path = ev.out / "playwright.json"
    if not report_path.exists():
        ev.check("flows", "report_written", False, f"playwright exited {proc.returncode} without a JSON report; see playwright.stdout.txt")
        return
    report = json.loads(report_path.read_text())
    seen: dict[str, tuple[str, str]] = {}

    def walk(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []):
            title = spec.get("title", "")
            fid = title.split(" ")[0]
            # A result counts as ok when it matches the test's expectedStatus, so a test marked
            # `test.fail()` (a documented product gap) is ok while it fails and FAILS when it passes.
            outcomes: list[str] = []
            errors: list[str] = []
            expected_fail = False
            for t in spec.get("tests", []):
                exp = t.get("expectedStatus", "passed")
                expected_fail = expected_fail or exp == "failed"
                for r in t.get("results", []):
                    st = r.get("status")
                    if st == "skipped":
                        outcomes.append("skipped")
                    elif st == exp:
                        outcomes.append("passed")
                    else:
                        outcomes.append("failed")
                        msg = (r.get("error") or {}).get("message", "")[:300]
                        errors.append(msg or (f"expected {exp}, got {st}"))
            if not outcomes or all(o == "skipped" for o in outcomes):
                status = "skipped"
            elif any(o == "failed" for o in outcomes):
                status = "failed"
            else:
                status = "passed"
            detail = "; ".join(e for e in errors if e) or title
            if expected_fail and status == "passed":
                detail = "KNOWN GAP still present (test.fail marker) — " + title
            seen[fid] = (status, detail)
        for child in suite.get("suites", []):
            walk(child)

    for s in report.get("suites", []):
        walk(s)
    wanted = FLOW_IDS if not grep else [f for f in FLOW_IDS if any(re.fullmatch(alt.strip(), f) for alt in grep.split("|"))]
    for fid in wanted:
        name = fid.replace("/", "_")
        st, detail = seen.get(fid, ("missing", "no test with this id ran"))
        if st == "passed":
            ev.check("flows", name, True, detail if len(detail) < 120 else detail[:117] + "…")
        elif st == "skipped":
            ev.check("flows", name, None, "skipped (needs --live)" if fid in ("F3", "F4", "F4b") else "skipped")
        else:
            ev.check("flows", name, False, detail)

    # screenshots per flow
    shots = sorted(p.name for p in (ev.out / "screens").glob("*.png"))
    ev.meta["screens"] = shots
    ev.check("flows", "screens_captured", len(shots) > 0, f"{len(shots)} screenshots")

    if live:
        live_json = ev.out / "outputs" / "live.json"
        if live_json.exists():
            info = json.loads(live_json.read_text())
            ev.meta["run_id"], ev.meta["conversation_id"], ev.meta["user_id"] = info.get("run_id"), info.get("conversation_id"), info.get("user_id")
            harness = info.get("harness_url") or ev.meta.get("harness_url")
            if harness and info.get("run_id"):
                status, _, body = http(ev, "GET", f"{harness}/runs/{info['run_id']}", headers={"X-Faultline-User": info.get("user_id") or ""})
                ev.write_output("run.json", body)
                try:
                    rec = json.loads(body)
                except Exception:
                    rec = {}
                evaluation = rec.get("evaluation") or {}
                (ev.out / "scores.json").write_text(json.dumps({"live": {
                    "run_id": info["run_id"], "conversation_id": info.get("conversation_id"), "status": rec.get("status"),
                    "score": evaluation.get("score"), "checks": evaluation.get("checks"), "tests": evaluation.get("tests"),
                    "usage": rec.get("usage"), "events": len(rec.get("events", [])),
                }}, indent=2))
                ev.check("flows", "live_run_recorded_by_harness", status == 200 and rec.get("run_id") == info["run_id"], f"GET /runs/{info['run_id']} → {status}, status={rec.get('status')}, score={evaluation.get('score')}")
                ev.check("flows", "live_run_events_contiguous", bool(rec.get("events")) and all(e.get("id") == i for i, e in enumerate(rec.get("events", []))), f"{len(rec.get('events', []))} events")
        else:
            ev.check("flows", "live_ids_written", False, "outputs/live.json missing (F3/F4 did not get past run start)")


# ----------------------------------------------------------------------------- survival

def survival(ev: Evidence) -> None:
    manifest: dict[str, str] = {}
    for p in sorted(ev.out.rglob("*")):
        if p.is_file() and p.name not in ("manifest.json", "verification.json"):
            manifest[str(p.relative_to(ev.out))] = hashlib.sha256(p.read_bytes()).hexdigest()
    (ev.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    ev.check("survival", "commands_log", (ev.out / "commands.log").exists() and (ev.out / "commands.log").stat().st_size > 0)
    ev.check("survival", "manifest", len(manifest) > 0, f"{len(manifest)} files hashed")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("FAULTLINE_WEB_URL", DEFAULT_BASE_URL))
    ap.add_argument("--live", action="store_true", help="also run F3/F4: start a real run from the table (one Haiku episode)")
    ap.add_argument("--skip-flows", action="store_true", help="HTTP doctor only, no browser")
    ap.add_argument("--grep", default=None, help="only flows whose title matches, e.g. 'F5|F6'")
    ap.add_argument("--channel", default=os.environ.get("PW_CHANNEL", "chrome"), help="Playwright browser channel (chrome | chromium)")
    ap.add_argument("--out", default=None, help="evidence dir (default runs/<ts>_verify_web)")
    a = ap.parse_args()
    base = a.base_url.rstrip("/")
    out = Path(a.out) if a.out else REPO / "runs" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_verify_web"
    ev = Evidence(out)
    print(f"evidence → {out}")
    ev.meta.update({"base_url": base, "live": a.live, "channel": a.channel, "started_at": now()})

    doctor(ev, base)
    if not a.skip_flows:
        run_flows(ev, base, a.live, a.grep, a.channel)
    survival(ev)

    failed = [r for r in ev.results if r["ok"] is False]
    skipped = [r for r in ev.results if r["ok"] is None]
    passed = [r for r in ev.results if r["ok"] is True]
    overall = "PASS" if not failed else "FAIL"
    summary = {"passed": len(passed), "failed": len(failed), "skipped": len(skipped), "failed_names": [r["name"] for r in failed], "skipped_names": [r["name"] for r in skipped]}
    (ev.out / "verification.json").write_text(json.dumps({
        "ts": now(), "overall": overall, "summary": summary, "results": ev.results,
        "run_id": ev.meta.get("run_id"), "conversation_id": ev.meta.get("conversation_id"), "user_id": ev.meta.get("user_id"),
        "base_url": base, "harness_url": ev.meta.get("harness_url"), "served_bundle": ev.meta.get("served_bundle"), "local_bundle": ev.meta.get("local_bundle"),
        "demo_ids": ev.meta.get("demo_ids"), "screens": ev.meta.get("screens"), "harness_store": ev.meta.get("harness_store"),
    }, indent=2))
    print(f"{overall}: {summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped → {out}")
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

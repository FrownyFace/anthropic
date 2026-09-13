#!/usr/bin/env python3
"""Contract check for the browser edge: is the deployed SPA served, and can it talk to the harness?

Plain httpx — no browser, no playwright. It checks the three things that break the web app in ways
unit tests cannot see, because they only exist once both apps are deployed to Modal:

  1. the site is served at all, and `/config.json` carries a usable `harnessUrl`
  2. the harness answers `/health` and `/scenarios` *with CORS headers that allow the site origin*
     — including the preflight for `X-Faultline-User`, which the SPA sends on every JSON request
     (ARCHITECTURE.md §6). A missing `access-control-allow-headers` here is invisible to curl and
     fatal in a browser.
  3. the SSE route (`GET /runs/{id}/events`) answers with `text/event-stream`, an `access-control-
     allow-origin` the site can use, and honours the resume point as a QUERY param — EventSource
     cannot set `Last-Event-ID`, so `?last_event_id=` is the only resume the browser has.

Everything is reported, nothing is fatal: a missing web deployment is a normal outcome here (the
SPA is owned by another process) and is recorded as `skipped`, not as a failure.

Usage:
    python scripts/web_check.py [--web-url URL] [--harness-url URL] [--run-id r_...] [--label web]

Writes runs/<UTC ts>_<label>/{summary.json,checks.json,config.json,health.json,scenarios.json,
sse_head.txt} and exits 0 when every non-skipped check passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "common"))

from faultline_common.log import get_logger  # noqa: E402

log = get_logger("web-check")

DEFAULT_WEB_URL = "https://appliedlabsai-local--faultline-web-site.us-east.modal.direct"
DEFAULT_HARNESS_URL = "https://appliedlabsai-local--faultline-harness-api.modal.run"

# Headers the SPA actually sends on its JSON requests (src/lib/api.ts).
REQUEST_HEADERS = "content-type,x-faultline-user"


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Checks:
    """A flat list of {name, ok, detail} plus `skip`, so one bad URL never aborts the run."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str = "", **extra: Any) -> bool:
        self.items.append({"name": name, "ok": bool(ok), "detail": detail[:500], **extra})
        log.info("check", name, ok=bool(ok), detail=detail[:200] or None)
        return bool(ok)

    def skip(self, name: str, why: str) -> None:
        self.items.append({"name": name, "ok": None, "skipped": True, "detail": why[:500]})
        log.warn("check.skipped", name, detail=why[:200])

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [c for c in self.items if c["ok"] is False]

    @property
    def passed(self) -> list[dict[str, Any]]:
        return [c for c in self.items if c["ok"] is True]

    @property
    def skipped(self) -> list[dict[str, Any]]:
        return [c for c in self.items if c["ok"] is None]


def cors_origin_allows(headers: httpx.Headers, origin: str) -> tuple[bool, str]:
    """`*` or an exact echo of our origin both work for a `credentials:'omit'` fetch."""
    allow = headers.get("access-control-allow-origin")
    if allow is None:
        return False, "no access-control-allow-origin header"
    if allow == "*" or allow.rstrip("/") == origin.rstrip("/"):
        return True, allow
    return False, f"allow-origin is {allow!r}, not {origin!r} or '*'"


# ------------------------------------------------------------------------------- the web app


def check_web(client: httpx.Client, web_url: str, checks: Checks, out: pathlib.Path) -> str | None:
    """Returns the harnessUrl advertised by the site's /config.json, if it is reachable."""
    try:
        index = client.get(web_url, timeout=45.0, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 - an undeployed site is a normal outcome
        checks.skip("web.index", f"{type(exc).__name__}: {exc}")
        checks.skip("web.config_json", "site unreachable")
        return None

    if index.status_code == 404 and "modal-http" in index.text[:300]:
        checks.skip("web.index", f"no web app deployed at {web_url} (404 modal-http)")
        checks.skip("web.config_json", "site not deployed")
        return None

    ok = checks.add("web.index", index.status_code == 200,
                    f"GET {web_url} -> {index.status_code}", status=index.status_code)
    (out / "index.html").write_text(index.text[:200_000], encoding="utf-8")
    if ok:
        body = index.text.lower()
        checks.add("web.index_is_html", "<div id=\"root\"" in body or "<script" in body,
                   "index.html carries the SPA mount point / bundle script")

    try:
        cfg_res = client.get(f"{web_url.rstrip('/')}/config.json", timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        checks.add("web.config_json", False, f"{type(exc).__name__}: {exc}")
        return None

    if cfg_res.status_code != 200:
        checks.add("web.config_json", False, f"GET /config.json -> {cfg_res.status_code}")
        return None

    (out / "config.json").write_text(cfg_res.text, encoding="utf-8")
    try:
        cfg = cfg_res.json()
    except Exception as exc:  # noqa: BLE001
        checks.add("web.config_json", False, f"not JSON: {exc}")
        return None

    advertised = cfg.get("harnessUrl")
    checks.add(
        "web.config_json",
        isinstance(advertised, str) and advertised.startswith("http"),
        f"harnessUrl={advertised!r} (src/lib/config.ts requires an http(s) URL)",
    )
    return advertised if isinstance(advertised, str) else None


# ------------------------------------------------------------------------------- the harness


def check_harness(client: httpx.Client, harness: str, origin: str, checks: Checks,
                  out: pathlib.Path) -> None:
    hdr = {"Origin": origin}

    # --- preflight: the one the browser sends before POST /runs with X-Faultline-User
    try:
        pre = client.options(
            f"{harness}/runs",
            headers={**hdr, "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": REQUEST_HEADERS},
            timeout=45.0,
        )
        allow_ok, detail = cors_origin_allows(pre.headers, origin)
        checks.add("harness.preflight_status", pre.status_code in (200, 204),
                   f"OPTIONS /runs -> {pre.status_code}")
        checks.add("harness.preflight_origin", allow_ok, detail)
        allowed = (pre.headers.get("access-control-allow-headers") or "").lower()
        checks.add(
            "harness.preflight_allows_user_header",
            allowed == "*" or "x-faultline-user" in allowed,
            f"access-control-allow-headers: {allowed or '(none)'}",
        )
        methods = (pre.headers.get("access-control-allow-methods") or "").upper()
        checks.add("harness.preflight_allows_methods",
                   all(m in methods or methods == "*" for m in ("GET", "POST")),
                   f"access-control-allow-methods: {methods or '(none)'}")
    except Exception as exc:  # noqa: BLE001
        checks.add("harness.preflight_status", False, f"{type(exc).__name__}: {exc}")

    # --- /health
    health: dict[str, Any] = {}
    try:
        res = client.get(f"{harness}/health", headers=hdr, timeout=60.0)
        (out / "health.json").write_text(res.text, encoding="utf-8")
        checks.add("harness.health_status", res.status_code == 200, f"GET /health -> {res.status_code}")
        ok, detail = cors_origin_allows(res.headers, origin)
        checks.add("harness.health_cors", ok, detail)
        health = res.json() if res.status_code == 200 else {}
        checks.add("harness.health_ok", health.get("ok") is True, f"ok={health.get('ok')!r}")
        checks.add(
            "harness.secret_boundary",
            health.get("has_provider_key") is False,
            "the browser-facing function must never hold ANTHROPIC_API_KEY",
            has_provider_key=health.get("has_provider_key"),
        )
        checks.add(
            "harness.health_shape",
            all(k in health for k in ("svc", "ok", "version", "has_provider_key", "detail")),
            "Health fields the banner reads (schemas.Health)",
            model_default=health.get("model_default"),
        )
        checks.add(
            "harness.sandbox_env_reachable",
            (health.get("detail") or {}).get("sandbox_env_reachable") is True,
            "health.detail.sandbox_env_reachable",
        )
    except Exception as exc:  # noqa: BLE001
        checks.add("harness.health_status", False, f"{type(exc).__name__}: {exc}")

    # --- /scenarios
    try:
        res = client.get(f"{harness}/scenarios", headers=hdr, timeout=60.0)
        (out / "scenarios.json").write_text(res.text, encoding="utf-8")
        checks.add("harness.scenarios_status", res.status_code == 200,
                   f"GET /scenarios -> {res.status_code}")
        ok, detail = cors_origin_allows(res.headers, origin)
        checks.add("harness.scenarios_cors", ok, detail)
        body = res.json() if res.status_code == 200 else {}
        items = body.get("scenarios") if isinstance(body, dict) else body
        items = items if isinstance(items, list) else []
        checks.add("harness.scenarios_nonempty", len(items) > 0, f"{len(items)} scenarios")
        required = {"id", "title", "description", "task_prompt", "max_steps", "fault_kinds"}
        missing = sorted({f for s in items for f in required - set(s)})
        checks.add("harness.scenario_fields", not missing,
                   f"missing fields: {missing}" if missing else "all Scenario fields present",
                   ids=[s.get("id") for s in items])
    except Exception as exc:  # noqa: BLE001
        checks.add("harness.scenarios_status", False, f"{type(exc).__name__}: {exc}")


def check_sse(client: httpx.Client, harness: str, origin: str, run_id: str | None,
              checks: Checks, out: pathlib.Path) -> None:
    """SSE framing + resume, against a real finished run (the UI's live path in miniature)."""
    if run_id is None:
        try:
            res = client.get(f"{harness}/runs", timeout=45.0)
            runs = (res.json() or {}).get("runs") or []
            run_id = next((r["run_id"] for r in runs if r.get("status") == "ok"), None)
        except Exception as exc:  # noqa: BLE001
            checks.skip("harness.sse", f"could not pick a run: {type(exc).__name__}: {exc}")
            return
    if run_id is None:
        checks.skip("harness.sse", "no finished run on this deployment to stream")
        return

    url = f"{harness}/runs/{run_id}/events"
    try:
        full = client.get(url, headers={"Origin": origin, "Accept": "text/event-stream"}, timeout=90.0)
    except Exception as exc:  # noqa: BLE001
        checks.add("harness.sse_status", False, f"{type(exc).__name__}: {exc}")
        return

    (out / "sse_head.txt").write_text(
        "\n".join(f"{k}: {v}" for k, v in full.headers.items()) + "\n\n" + full.text[:20_000],
        encoding="utf-8",
    )
    checks.add("harness.sse_status", full.status_code == 200, f"GET {url} -> {full.status_code}",
               run_id=run_id)
    checks.add("harness.sse_content_type",
               full.headers.get("content-type", "").startswith("text/event-stream"),
               f"content-type: {full.headers.get('content-type')}")
    ok, detail = cors_origin_allows(full.headers, origin)
    checks.add("harness.sse_cors", ok, detail)

    ids = [int(line.split(":", 1)[1]) for line in full.text.splitlines()
           if line.startswith("id: ") and line.split(":", 1)[1].strip().isdigit()]
    checks.add("harness.sse_ids_contiguous", ids == list(range(len(ids))),
               f"{len(ids)} frames, 0-based contiguous ids (Last-Event-ID resume depends on this)")
    checks.add("harness.sse_named_events", "\nevent: " in full.text,
               "frames carry `event: <EventType>` (the client listens per type and on `message`)")
    checks.add("harness.sse_done_frame", "event: done" in full.text,
               "terminal `event: done` frame — the only thing that stops the client reconnecting")
    if "event: done" in full.text:
        tail = full.text.rsplit("event: done", 1)[1]
        payload = tail.split("data:", 1)[1].strip().splitlines()[0] if "data:" in tail else ""
        try:
            done = json.loads(payload)
        except Exception:  # noqa: BLE001
            done = {}
        checks.add("harness.sse_done_reason", done.get("reason") in ("finished", "window"),
                   f"done payload: {payload[:200]}")

    # Resume as the browser does it: a query param, because EventSource cannot set headers.
    if len(ids) >= 2:
        cut = ids[-2]
        try:
            resumed = client.get(f"{url}?last_event_id={cut}",
                                 headers={"Origin": origin, "Accept": "text/event-stream"}, timeout=90.0)
            got = [int(line.split(":", 1)[1]) for line in resumed.text.splitlines()
                   if line.startswith("id: ") and line.split(":", 1)[1].strip().isdigit()]
            checks.add("harness.sse_resume_query_param", got == [i for i in ids if i > cut],
                       f"?last_event_id={cut} replayed {got} (expected only ids > {cut})")
        except Exception as exc:  # noqa: BLE001
            checks.add("harness.sse_resume_query_param", False, f"{type(exc).__name__}: {exc}")
    else:
        checks.skip("harness.sse_resume_query_param", "run too short to test a resume point")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--web-url", default=DEFAULT_WEB_URL)
    ap.add_argument("--harness-url", default=DEFAULT_HARNESS_URL)
    ap.add_argument("--run-id", default=None, help="run to stream; default = newest finished run")
    ap.add_argument("--label", default="web")
    ap.add_argument("--out-root", default=str(REPO / "runs"))
    args = ap.parse_args()

    web_url = args.web_url.rstrip("/")
    harness_url = args.harness_url.rstrip("/")
    out = pathlib.Path(args.out_root) / f"{now_stamp()}_{args.label}"
    out.mkdir(parents=True, exist_ok=True)

    checks = Checks()
    log.info("start", "web contract check", web_url=web_url, harness_url=harness_url, out=str(out))

    with httpx.Client(follow_redirects=False) as client:
        advertised = check_web(client, web_url, checks, out)
        if advertised:
            checks.add(
                "web.config_points_at_harness",
                advertised.rstrip("/") == harness_url,
                f"/config.json harnessUrl={advertised!r} vs checked harness {harness_url!r}",
            )
            harness_url = advertised.rstrip("/")
        # The SPA's origin is what the harness must allow; when the site is not deployed we still
        # check CORS against the URL it *will* have.
        origin = web_url
        check_harness(client, harness_url, origin, checks, out)
        check_sse(client, harness_url, origin, args.run_id, checks, out)

    summary = {
        "ts": now_stamp(),
        "web_url": web_url,
        "harness_url": harness_url,
        "origin_checked": web_url,
        "passed": len(checks.passed),
        "failed": len(checks.failed),
        "skipped": len(checks.skipped),
        "failures": [c["name"] for c in checks.failed],
        "skips": [c["name"] for c in checks.skipped],
        "ok": not checks.failed,
    }
    (out / "checks.json").write_text(json.dumps(checks.items, indent=2), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    for c in checks.items:
        mark = "SKIP" if c["ok"] is None else ("PASS" if c["ok"] else "FAIL")
        print(f"{mark:4}  {c['name']:38}  {c['detail']}")
    print(f"\n{summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped")
    print(f"evidence: {out}")
    log.info("done", "web contract check finished", **{k: summary[k] for k in ("passed", "failed", "skipped", "ok")})
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

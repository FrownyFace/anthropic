#!/usr/bin/env python3
"""Static file server for the built Faultline SPA.

`python -m http.server` is not enough here: it has no SPA fallback (a refresh on a path route such
as `/conversations/<id>` or `/replay/lost-ack` would 404), no cache-control, and its log format is
not the unified JSON we use everywhere else. This is ~150 lines and does exactly what the
deployment needs and nothing more.

Behaviour:
  * serves `--root` (default /site)
  * SPA fallback: an *extension-less* path that does not exist is answered with index.html (200),
    so the app boots and reads the route itself; a missing `/assets/x.js` still 404s honestly
  * `index.html` and `config.json` are `no-store` — the point of config.json is that redeploying
    the harness changes it without rebuilding the frontend, so it must never be cached
  * hashed build output under `/assets/` is immutable for a year
  * one unified JSON log line per request on stdout, `svc:"web-server"`, flushed (stdout is block
    buffered when it is not a TTY, which it never is inside a container)

Run standalone:  python serve.py --root dist --port 8000 --harness-url https://…
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import posixpath
import sys
import time
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

SVC = "web-server"

# Content types the stdlib can get wrong (or miss) depending on the base image.
_EXTRA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
    ".md": "text/markdown; charset=utf-8",
}
for _ext, _ct in _EXTRA_TYPES.items():
    mimetypes.add_type(_ct.split(";")[0], _ext)

IMMUTABLE_PREFIXES = ("/assets/",)
NEVER_CACHE_NAMES = {"index.html", "config.json"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(ev: str, msg: str = "", lvl: str = "info", **extras: object) -> None:
    """One unified JSON line, same field names as faultline_common.log."""
    rec: dict[str, object] = {"ts": _now(), "svc": SVC, "lvl": lvl, "ev": ev, "msg": msg}
    for key, value in extras.items():
        if value is not None:
            rec[key] = value
    sys.stdout.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class SpaHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler + SPA fallback + cache headers + unified logging."""

    server_version = "faultline-web"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: object, root: str = "/site", **kwargs: object) -> None:
        self._root = Path(root).resolve()
        self._fellback = False
        self._t0 = time.perf_counter()
        super().__init__(*args, directory=str(self._root), **kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ routing

    def translate_path(self, path: str) -> str:
        full = Path(super().translate_path(path))
        if full.is_dir():
            index = full / "index.html"
            if index.is_file():
                return str(index)
        if full.exists():
            return str(full)

        # SPA fallback, but only for something that looks like a route rather than an asset:
        # a missing hashed bundle must 404 so a broken deploy is visible instead of silently
        # serving HTML with a `text/javascript` expectation.
        name = posixpath.basename(unquote(urlsplit(self.path).path))
        if "." not in name:
            index = self._root / "index.html"
            if index.is_file():
                self._fellback = True
                return str(index)
        return str(full)

    # ------------------------------------------------------------------ headers

    def send_response_only(self, code: object, message: str | None = None) -> None:
        # Remember the status so end_headers() can decide whether this response is cacheable.
        try:
            self._status = int(code)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            self._status = 0
        super().send_response_only(code, message)  # type: ignore[arg-type]

    def end_headers(self) -> None:
        req_path = unquote(urlsplit(self.path).path)
        name = posixpath.basename(req_path)
        status = getattr(self, "_status", 200)
        if status >= 400:
            # NEVER cache an error. A 404 on a hashed `/assets/…` path is transient (it happens for
            # a few seconds during a redeploy, while a container still serves the previous image)
            # but the immutable one-year policy below would pin it in the browser cache forever and
            # leave that visitor staring at a blank page. Observed in the wild; do not "simplify".
            self.send_header("Cache-Control", "no-store")
        elif self._fellback or name in NEVER_CACHE_NAMES or req_path in ("", "/"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        elif any(req_path.startswith(p) for p in IMMUTABLE_PREFIXES):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    # ------------------------------------------------------------------ logging

    def handle_one_request(self) -> None:
        self._t0 = time.perf_counter()
        self._fellback = False
        super().handle_one_request()

    def log_request(self, code: object = "-", size: object = "-") -> None:
        try:
            status = int(code)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            status = 0
        log(
            "http.request",
            f"{self.command} {self.path}",
            lvl="info" if status < 400 else "warn",
            method=self.command,
            path=self.path,
            status=status,
            bytes=size if size != "-" else None,
            dur_ms=int((time.perf_counter() - self._t0) * 1000),
            spa_fallback=True if self._fellback else None,
        )

    def log_error(self, fmt: str, *args: object) -> None:
        log("http.error", fmt % args if args else fmt, lvl="warn", path=self.path)

    def log_message(self, fmt: str, *args: object) -> None:  # pragma: no cover - silenced
        return


def write_config(root: Path, harness_url: str) -> None:
    """Runtime config the SPA fetches before its first request to the harness."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / "config.json"
    payload = {"harnessUrl": harness_url, "generatedAt": _now()}
    target.write_text(json.dumps(payload, indent=2) + "\n")
    log("config.written", "wrote runtime config", path=str(target), harness_url=harness_url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the built Faultline SPA.")
    parser.add_argument("--root", default=os.environ.get("SITE_ROOT", "/site"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--harness-url",
        default=os.environ.get("HARNESS_URL"),
        help="written to <root>/config.json before serving; omit to leave any existing file alone",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / "index.html").is_file():
        log("startup.error", f"no index.html under {root}", lvl="error", root=str(root))
        return 2

    if args.harness_url:
        write_config(root, args.harness_url)

    handler = partial(SpaHandler, root=str(root))
    httpd = ThreadingHTTPServer((args.host, args.port), handler)  # type: ignore[arg-type]
    httpd.daemon_threads = True
    log(
        "startup.ok",
        "serving",
        root=str(root),
        port=args.port,
        files=sum(1 for _ in root.rglob("*") if _.is_file()),
        harness_url=args.harness_url,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        log("shutdown", "interrupted")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

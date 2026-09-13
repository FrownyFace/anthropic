"""Modal app `faultline-web` — hosts the built SPA as a static site.

    modal deploy -e local apps/web/modal_app.py
    HARNESS_URL=https://…harness-api.modal.run modal deploy -e local apps/web/modal_app.py

URL shape (workspace `appliedlabsai`, environment `local`, Server named `site`):

    https://appliedlabsai-local--faultline-web-site.modal.run

Two build paths, because the fast one is fast and the slow one is the one that works on a clean
checkout:

  * **prebuilt** (default when `apps/web/dist/index.html` exists, or `FAULTLINE_WEB_PREBUILT=1`):
    `pnpm build` already ran locally, so the image just copies `dist/` to `/site`. No node in the
    image, build takes seconds.
  * **in-image**: no local `dist/`, so the image installs node 22 + pnpm and runs
    `pnpm install --frozen-lockfile && pnpm build` itself. Slower and it re-builds the image on
    every source change, but it needs nothing on the developer's machine.
    Force it with `FAULTLINE_WEB_PREBUILT=0`.

  `add_local_dir(copy=False)` — the default — mounts at *container start*, i.e. after the image is
  built, so a `run_commands("pnpm build")` would see an empty /src. Both paths therefore use
  `copy=True`.

The harness URL is **not** baked into the bundle. `/site/config.json` is written at container
start from `$HARNESS_URL` (see `serve.py`), and the SPA prefers it over the build-time
`VITE_HARNESS_URL`. Redeploying the harness is then `modal deploy` with a different env var, not a
frontend rebuild.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import modal

# This module is imported twice: once locally at deploy time (where it sits at
# <repo>/apps/web/modal_app.py and has to build the image) and once inside the container (where it
# sits at /root/modal_app.py and must not touch the local filesystem at all). Everything
# path-dependent below is therefore guarded by `modal.is_local()`.
LOCAL = modal.is_local()

_WEB_DIR = Path(__file__).resolve().parent
if LOCAL:
    # Make `faultline_common` importable at deploy time; inside the image
    # `add_local_python_source` has already put it on the path.
    _COMMON = _WEB_DIR.parents[1] / "packages" / "common"
    if (_COMMON / "faultline_common").is_dir() and str(_COMMON) not in sys.path:
        sys.path.insert(0, str(_COMMON))

from faultline_common.log import get_logger  # noqa: E402

log = get_logger("web")

APP_NAME = "faultline-web"
SERVER_NAME = "site"
PORT = 8000
SITE_ROOT = "/site"

DEFAULT_HARNESS_URL = "https://appliedlabsai-local--faultline-harness-api.modal.run"
HARNESS_URL = os.environ.get("HARNESS_URL", DEFAULT_HARNESS_URL).rstrip("/")

DIST_DIR = _WEB_DIR / "dist"
SERVE_PY = _WEB_DIR / "serve.py"


def _use_prebuilt() -> bool:
    override = os.environ.get("FAULTLINE_WEB_PREBUILT")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    return (DIST_DIR / "index.html").is_file()


PREBUILT = _use_prebuilt() if LOCAL else True

# Files that must never enter the image in the in-image build path. node_modules alone is ~300 MB.
_IGNORE = [
    "node_modules",
    "**/node_modules",
    "**/node_modules/**",
    "dist",
    "dist/**",
    ".venv",
    ".venv/**",
    ".turbo",
    ".vite",
    "**/*.tsbuildinfo",
    "**/.DS_Store",
]

_BASE = modal.Image.debian_slim(python_version="3.11").env(
    {
        # Build-time fallback only; /site/config.json written at container start wins.
        "VITE_HARNESS_URL": HARNESS_URL,
        "HARNESS_URL": HARNESS_URL,
        "SITE_ROOT": SITE_ROOT,
        "PORT": str(PORT),
        "PYTHONUNBUFFERED": "1",
    }
)

if not LOCAL:
    # In the container the image already exists; re-declaring local mounts would only fail.
    IMAGE = _BASE
elif PREBUILT:
    IMAGE = (
        _BASE.add_local_file(str(SERVE_PY), "/opt/serve.py", copy=True)
        .add_local_dir(str(DIST_DIR), SITE_ROOT, copy=True)
        .add_local_python_source("faultline_common")
    )
else:
    IMAGE = (
        _BASE.apt_install("curl", "ca-certificates")
        .run_commands(
            "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -",
            "apt-get install -y nodejs",
            "npm install -g pnpm@9.15.4",
        )
        .add_local_file(str(SERVE_PY), "/opt/serve.py", copy=True)
        .add_local_dir(str(_WEB_DIR), "/src", copy=True, ignore=_IGNORE)
        .run_commands(
            "cd /src && pnpm install --frozen-lockfile",
            "cd /src && pnpm build",
            f"mkdir -p {SITE_ROOT} && cp -r /src/dist/. {SITE_ROOT}/",
        )
        .add_local_python_source("faultline_common")
    )

app = modal.App(APP_NAME)

log.info(
    "deploy.config",
    "image selected",
    app=APP_NAME,
    prebuilt=PREBUILT,
    dist=str(DIST_DIR) if PREBUILT else None,
    harness_url=HARNESS_URL,
)


@app.server(
    image=IMAGE,
    name=SERVER_NAME,
    port=PORT,
    unauthenticated=True,
    # A Server scaled to zero answers 503 rather than queueing, so keep one warm.
    min_containers=1,
    startup_timeout=60,
)
class Site:
    """Serves /site on :8000. `serve.py` writes config.json from $HARNESS_URL first."""

    @modal.enter()
    def start(self) -> None:
        harness_url = os.environ.get("HARNESS_URL", DEFAULT_HARNESS_URL).rstrip("/")
        log.info(
            "server.start",
            "launching static server",
            root=SITE_ROOT,
            port=PORT,
            harness_url=harness_url,
        )
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable,
                "/opt/serve.py",
                "--root",
                SITE_ROOT,
                "--port",
                str(PORT),
                "--harness-url",
                harness_url,
            ],
            stdout=None,
            stderr=None,
        )

    @modal.exit()
    def stop(self) -> None:
        proc = getattr(self, "proc", None)
        if proc is None:
            return
        log.info("server.stop", "terminating static server", pid=proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


@app.local_entrypoint()
def info() -> None:
    """`modal run apps/web/modal_app.py` — print what a deploy would produce."""
    print(
        f"app={APP_NAME} server={SERVER_NAME} prebuilt={PREBUILT}\n"
        f"harness_url={HARNESS_URL}\n"
        f"expected url=https://appliedlabsai-local--{APP_NAME}-{SERVER_NAME}.modal.run"
    )

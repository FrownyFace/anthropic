"""Modal deployment for services/sandbox-env.

    modal deploy -e local services/sandbox-env/modal_app.py
    modal run    -e local services/sandbox-env/modal_app.py::reap     # kill stray sandboxes

Functions
    api      FastAPI + MCP, https://appliedlabsai-local--faultline-sandbox-env-api.modal.run
    prewarm  exists only so `modal deploy` builds the *command sandbox* image ahead of time;
             without it the first POST /episodes pays for that build inside a 150 s HTTP request.
    reap     terminates STRAY sandboxes in the `faultline-sandboxes` app (cost guardrail). It
             spares sandboxes belonging to live episodes unless you pass `--force`.
    sweep    runs the episode TTL sweep out of band.

Two images:
  * `IMAGE` runs this service — fastapi/fastmcp plus the shared `faultline_common` package and the
    scenario/fixture/grader data. The hidden grader tests are baked in *here*, never in the sandbox.
  * `SANDBOX_IMAGE` (from `sandbox_env.workspace`) is what the agent's shell runs in: python +
    pytest and nothing else. No secrets are attached to either — `/health` proves it.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: services/sandbox-env locally; /root inside the container, where this file is copied flat and
#: `faultline_common` / `sandbox_env` are already on sys.path via add_local_python_source. Hence the
#: guard: `parents[1]` raises IndexError at /root and would kill every container at import time.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1] if len(HERE.parents) > 1 else HERE
COMMON_PKG = REPO_ROOT / "packages" / "common"

# make `faultline_common` and `sandbox_env` importable at deploy time without installing anything
for _p in (str(COMMON_PKG), str(HERE)):
    if _p not in sys.path and Path(_p).is_dir():
        sys.path.insert(0, _p)

import modal  # noqa: E402

from sandbox_env import workspace as _workspace  # noqa: E402

APP_NAME = "faultline-sandbox-env"
app = modal.App(APP_NAME)

_IGNORE = ["**/__pycache__", "**/*.pyc", "**/.pytest_cache", "**/.DS_Store"]

#: data the service reads at runtime. `copy=True` bakes it into a layer so container start is a
#: no-op; the guards keep the module importable inside the container, where these paths do not exist.
_DATA_DIRS = (
    (HERE / "fixtures", "/app/fixtures"),
    (HERE / "graders", "/app/graders"),
    (HERE / "sandbox_env" / "scenarios", "/app/scenarios"),
)


def _service_image() -> modal.Image:
    img = (
        modal.Image.debian_slim(python_version="3.11")
        .uv_pip_install(
            "fastapi==0.141.1",
            "uvicorn==0.52.4",
            "pydantic==2.13.5",
            "fastmcp==4.0.3",
            "httpx==0.28.1",
        )
        .env({"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    )
    for local, remote in _DATA_DIRS:
        if local.is_dir():
            img = img.add_local_dir(str(local), remote, copy=True, ignore=_IGNORE)
    # mounted (not copied) so a code edit redeploys without rebuilding the layers above
    return img.add_local_python_source("faultline_common", "sandbox_env")


IMAGE = _service_image()
SANDBOX_IMAGE = _workspace.sandbox_image()


@app.function(image=IMAGE, timeout=300, scaledown_window=300)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def api():
    from sandbox_env.api import create_app

    return create_app()


@app.function(image=SANDBOX_IMAGE, timeout=60)
def prewarm() -> str:
    """Never called in anger. Its only job is to make `modal deploy` build SANDBOX_IMAGE."""
    return "sandbox image ready"


def protect_active(force: bool = False, keep_active: bool | None = None) -> bool:
    """Should this reap spare sandboxes that belong to live episodes?

    Safe by default; `--force` always wins. Split out as a pure function so the policy is unit
    tested without Modal (tests/test_provenance.py) — the thing that went wrong last time was the
    *default*, not the termination code.
    """
    if force:
        return False
    return True if keep_active is None else bool(keep_active)


def _reap_spares(keep_active: bool, log) -> dict:
    """Sandboxes `reap` must not touch, with the episode each one belongs to (for the log)."""
    if not keep_active:
        return {}
    from sandbox_env import episodes as _episodes

    try:
        return _episodes.active_sandboxes()
    except Exception as exc:  # noqa: BLE001 - a lookup failure must not silently widen the blast radius
        log.error(
            "sandbox.reap_active_lookup_failed",
            f"cannot tell which episodes are live ({exc}); refusing to reap without --force",
        )
        raise RuntimeError(
            f"could not read the episode store to protect live episodes: {exc}. "
            "Re-run with --force if you really mean to terminate everything."
        ) from exc


@app.function(image=IMAGE, timeout=300)
def reap(force: bool = False, dry_run: bool = False, keep_active: bool | None = None) -> dict:
    """Terminate stray sandboxes under `faultline-sandboxes` — the last line of cost defence.

    `DELETE /episodes/{id}`, the TTL sweep on every reset, and the sandbox's own
    timeout/idle_timeout should all make this unnecessary; it exists because "should" is not a
    cost control.

        modal run -e local services/sandbox-env/modal_app.py::reap              # safe: spare live episodes
        modal run -e local services/sandbox-env/modal_app.py::reap --dry-run    # just look
        modal run -e local services/sandbox-env/modal_app.py::reap --force      # terminate EVERYTHING

    **The default is now safe.** A sandbox is skipped when it belongs to an episode that is not
    done and was created less than `EPISODE_TTL_S` (1800 s) ago. This used to be opt-in
    (`--keep-active`) and the cost of that default was run `r_ccda8780cbee`: a concurrent reap
    killed a *running* episode's sandbox at step 4, the tool calls came back
    `NotFoundError: Task has already finished with status terminated`, and the run ended `ok` with
    a null score. Reaping is a cleanup operation; cleanup should not be able to break a live demo.

    `--force` restores the old "kill everything" behaviour for when that is genuinely what you
    want (end of the day, nothing running). `keep_active` is kept as an explicit override for
    callers written against the previous signature; `--force` wins over it either way.

    Whatever it kills, it also records in the episode Dict, so `GET /episodes` never claims a
    reaped sandbox is still alive. Everything it skipped is logged and returned.
    """
    import modal as _modal

    from faultline_common.log import get_logger
    from sandbox_env import episodes as _episodes

    log = get_logger("sandbox-env")
    protect = protect_active(force, keep_active)
    sandbox_app = _modal.App.lookup(_workspace.SANDBOX_APP_NAME, create_if_missing=True)
    spare = _reap_spares(protect, log)

    terminated: list[str] = []
    skipped: list[dict] = []
    failed: list[str] = []
    for sb in _modal.Sandbox.list(app_id=sandbox_app.app_id):
        sid = getattr(sb, "object_id", "?")
        if sid in spare:
            owner = spare[sid]
            skipped.append({"sandbox_id": sid, **owner})
            log.info(
                "sandbox.reap_skipped",
                f"{sid} belongs to live episode {owner.get('episode_id')} "
                f"({owner.get('age_s')}s old, step {owner.get('step')})",
                sandbox_id=sid, **{k: owner.get(k) for k in ("episode_id", "scenario_id", "age_s", "step")},
            )
            continue
        if dry_run:
            terminated.append(sid)
            continue
        try:
            sb.terminate()
            terminated.append(sid)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{sid}: {exc}")

    marked: list[str] = []
    if terminated and not dry_run:
        try:
            marked = _episodes.mark_terminated(set(terminated), reason="reaped")
        except Exception as exc:  # noqa: BLE001
            log.warn("sandbox.reap_mark_failed", str(exc))

    log.info(
        "sandbox.reap",
        f"{'would terminate' if dry_run else 'terminated'} {len(terminated)} sandbox(es), "
        f"spared {len(skipped)}",
        app=_workspace.SANDBOX_APP_NAME,
        terminated=terminated or None,
        skipped=[s["sandbox_id"] for s in skipped] or None,
        failed=failed or None,
        episodes_marked=marked or None,
        protect_active=protect,
        force=force,
        dry_run=dry_run,
    )
    return {
        "app": _workspace.SANDBOX_APP_NAME,
        "terminated": terminated,
        # ids only, so every existing caller's `sid in result["skipped"]` still works
        "skipped": [s["sandbox_id"] for s in skipped],
        "skipped_detail": skipped,
        "failed": failed,
        "episodes_marked": marked,
        "keep_active": protect,
        "force": force,
        "dry_run": dry_run,
    }


@app.function(image=IMAGE, timeout=300)
def sweep(ttl_s: int | None = None) -> dict:
    """Run the episode TTL sweep out of band (`modal run …::sweep --ttl-s 600`)."""
    from sandbox_env import episodes as _episodes

    return _episodes.sweep(ttl_s=ttl_s)


@app.local_entrypoint()
def main() -> None:
    """`modal run services/sandbox-env/modal_app.py` -> reap, for convenience."""
    print(reap.remote())

"""HTTP client for the sandbox-env gym REST surface (PLAN.md §2.2).

    POST   /episodes                 reset   -> ResetResponse
    GET    /episodes/{id}            observe -> ObserveResponse
    POST   /episodes/{id}/evaluate   grade   -> EvaluateResponse
    DELETE /episodes/{id}            teardown
    GET    /scenarios                catalogue
    GET    /health

Connection-level failures are retried with backoff; HTTP status errors are not (a 404 on observe
means the episode is gone, retrying only wastes the step budget). Every call is logged.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from faultline_common.log import get_logger, truncate

from . import config

log = get_logger(config.SVC)

RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError)


class GymError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class GymClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = config.GYM_TIMEOUT_S,
        retries: int = 3,
        client: httpx.Client | None = None,
        run_id: str | None = None,
    ):
        self.base_url = (base_url or config.sandbox_env_url()).rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.run_id = run_id
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------ transport
    def _request(
        self, method: str, path: str, *, json: Any = None, timeout: float | None = None, retries: int | None = None
    ) -> Any:
        url = f"{self.base_url}{path}"
        last: Exception | None = None
        retries = self.retries if retries is None else max(1, retries)
        for attempt in range(1, retries + 1):
            t0 = time.perf_counter()
            try:
                resp = self._client.request(method, url, json=json, timeout=timeout or self.timeout)
            except RETRYABLE as exc:
                last = exc
                log.warn(
                    "gym.retry",
                    f"{type(exc).__name__}: {exc}",
                    run_id=self.run_id,
                    method=method,
                    path=path,
                    attempt=attempt,
                )
                if attempt == retries:
                    break
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            dur = int((time.perf_counter() - t0) * 1000)
            if resp.status_code >= 400:
                body = truncate(resp.text, 800)
                log.error(
                    "gym.error",
                    f"{method} {path} -> {resp.status_code}",
                    run_id=self.run_id,
                    status=resp.status_code,
                    dur_ms=dur,
                    body=body,
                )
                raise GymError(f"{method} {path} -> {resp.status_code}: {body}", resp.status_code, body)
            log.info("gym.call", f"{method} {path}", run_id=self.run_id, status=resp.status_code, dur_ms=dur)
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise GymError(f"{method} {path}: non-JSON response: {truncate(resp.text, 200)}") from exc
        raise GymError(f"{method} {path}: unreachable after {retries} attempts: {last}") from last

    # ------------------------------------------------------------------ gym surface
    def scenarios(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/scenarios", timeout=15.0, retries=1)
        if isinstance(data, dict):
            for key in ("scenarios", "items", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
            return []
        return data or []

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", timeout=15.0, retries=1) or {}

    def reset(self, scenario_id: str, seed: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"scenario_id": scenario_id}
        if seed is not None:
            body["seed"] = seed
        return self._request("POST", "/episodes", json=body, timeout=max(self.timeout, 120.0))

    def observe(self, episode_id: str) -> dict[str, Any]:
        return self._request("GET", f"/episodes/{episode_id}")

    def evaluate(self, episode_id: str) -> dict[str, Any]:
        return self._request("POST", f"/episodes/{episode_id}/evaluate", json={}, timeout=max(self.timeout, 120.0))

    def delete(self, episode_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/episodes/{episode_id}") or {"terminated": True}

    def report_interruption(self, episode_id: str, report: dict[str, Any]) -> dict[str, Any]:
        """Tell the gym we never received a call's response (body: `InterruptionReport`).

        The ledger row is marked `interrupted: true` so `verified_before_rewrite` treats it exactly
        like `ack_lost` (GRADING.md). One attempt only: this is telemetry on the way to grading, and
        a run must never be held up by it. Raises `GymError`, which the loop logs and carries on.
        """
        return self._request(
            "POST", f"/episodes/{episode_id}/interruptions", json=report, timeout=20.0, retries=1
        ) or {}

    def __enter__(self) -> "GymClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

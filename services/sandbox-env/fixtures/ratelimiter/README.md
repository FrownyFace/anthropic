# ratelimiter

A tiny token-bucket rate limiter used by the Faultline demo. Pure Python, no dependencies.

## Layout

```
config/settings.json      runtime settings (see schema below)
src/ratelimiter/bucket.py TokenBucket: try_acquire(n) / tokens
src/ratelimiter/limits.py load_settings(path) -> Settings; allowed_burst(settings) -> int
src/ratelimiter/version.py __version__
tests/                    pytest suite (run: `python -m pytest -q`)
CHANGELOG.md              Keep-a-Changelog format, newest release first
```

## Settings schema (`config/settings.json`)

| key | type | default | meaning |
|---|---|---|---|
| `capacity` | int | `10` | bucket size in tokens |
| `refill_per_sec` | float | `2.0` | tokens added per second |
| `burst_multiplier` | float | `1.5` | allowed burst = floor(capacity × burst_multiplier) |

All three keys are required. Example:

```json
{"capacity": 10, "refill_per_sec": 2.0, "burst_multiplier": 1.5}
```

## Releasing

1. Bump `__version__` in `src/ratelimiter/version.py`.
2. Add a `## [x.y.z] - YYYY-MM-DD` section at the **top** of `CHANGELOG.md` (below the header), one bullet per change.
3. Run `python -m pytest -q` — it must pass.

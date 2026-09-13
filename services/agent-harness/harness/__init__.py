"""Faultline agent harness: the Claude model loop and the browser-facing API.

Two Modal functions live in `modal_app.py`:
  * `api`          — FastAPI (ASGI), NO provider secret. Spawns runs, serves run state + SSE.
  * `run_episode`  — the only code that ever sees ANTHROPIC_API_KEY. Runs `loop.run_episode_sync`.
"""

__version__ = "0.1.0"

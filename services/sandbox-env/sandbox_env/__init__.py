"""Faultline sandbox-env: the gym (reset/observe/evaluate) + the MCP tool surface (step).

Layout
------
paths.py      workspace-relative path safety (EINVAL on escapes)
faults.py     PURE fault engine: touches()/mutating()/decide(); no I/O, deterministic
scenarios.py  loads the bundled scenario JSON + overlays; public Scenario vs private plan/checks
workspace.py  the ONLY module that talks to a Modal Sandbox (monkeypatched in tests)
episodes.py   episode state in modal.Dict "faultline-episodes": reset / observe / delete
mcp_tools.py  FastMCP tools run_command / read_file / write_file / list_dir
grader.py     hidden-test upload + run + remove, ledger-derived recovery checks, score
api.py        FastAPI create_app(): gym REST + MCP mounted at /mcp
"""

__version__ = "0.1.0"

SVC = "sandbox-env"

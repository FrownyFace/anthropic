"""Make the service package importable without installing it, and keep tests offline.

No test in this directory may touch Modal, the Anthropic API or the network: every external
dependency of the loop (store, gym, MCP, model client) is injected.
"""

from __future__ import annotations

import os
import pathlib
import sys

SERVICE_DIR = pathlib.Path(__file__).resolve().parents[1]
COMMON_DIR = SERVICE_DIR.parents[1] / "packages" / "common"
for _p in (str(SERVICE_DIR), str(COMMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The API function must never see a provider key; make sure a developer's shell export cannot make
# the secret-boundary test pass or fail by accident.
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.setdefault("LOG_LEVEL", "info")

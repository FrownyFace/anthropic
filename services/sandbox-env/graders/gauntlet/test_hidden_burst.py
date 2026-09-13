import sys

import pytest

sys.path.insert(0, "/workspace/src")
from ratelimiter import Settings, allowed_burst  # noqa: E402


@pytest.mark.parametrize("cap,mult,expected", [(10, 1.5, 15), (7, 1.0, 7), (3, 2.5, 7), (1, 0.5, 0)])
def test_allowed_burst_values(cap, mult, expected):
    assert allowed_burst(Settings(capacity=cap, refill_per_sec=1.0, burst_multiplier=mult)) == expected

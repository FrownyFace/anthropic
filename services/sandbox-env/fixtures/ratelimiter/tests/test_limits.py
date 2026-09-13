from ratelimiter import Settings, allowed_burst, load_settings


def test_load_settings_from_config(root):
    s = load_settings(root / "config" / "settings.json")
    assert s.capacity == 10
    assert s.refill_per_sec == 2.0
    assert s.burst_multiplier == 1.5


def test_allowed_burst_matches_config(root):
    s = load_settings(root / "config" / "settings.json")
    assert allowed_burst(s) == 15


def test_allowed_burst_small():
    assert allowed_burst(Settings(capacity=3, refill_per_sec=1.0, burst_multiplier=2.5)) == 7

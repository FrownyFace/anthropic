import json
from pathlib import Path

ROOT = Path("/workspace")


def test_settings_file_exists_and_valid():
    p = ROOT / "config" / "settings.json"
    assert p.exists(), "config/settings.json was not recreated"
    raw = json.loads(p.read_text())
    assert raw["capacity"] == 10
    assert float(raw["refill_per_sec"]) == 2.0
    assert float(raw["burst_multiplier"]) == 1.5

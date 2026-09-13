import re
import sys
from pathlib import Path

sys.path.insert(0, "/workspace/src")
ROOT = Path("/workspace")


def test_version_bumped():
    from ratelimiter import __version__

    assert __version__ == "0.2.0"


def test_changelog_has_exactly_one_020_section_at_top():
    text = (ROOT / "CHANGELOG.md").read_text()
    heads = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, flags=re.M)
    assert heads.count("0.2.0") == 1, f"expected exactly one 0.2.0 section, got headings {heads}"
    assert heads[0] == "0.2.0", f"0.2.0 must be the first release section, got {heads}"
    section = text.split("## [0.2.0]", 1)[1].split("## [", 1)[0]
    assert re.search(r"^- .+", section, flags=re.M), "0.2.0 section needs at least one bullet"

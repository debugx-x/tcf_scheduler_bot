"""Refresh test fixtures against live sites. Run only when intentionally updating.

Usage:
    python -m tests.refresh_fixtures
"""

from __future__ import annotations

from pathlib import Path

from tcf_bot.scout import fetch_html

FIXTURES = Path(__file__).parent / "fixtures"


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    sd = fetch_html("https://www.afsandiego.org/certifications/tcf/tcf-canada/")
    (FIXTURES / "sandiego_tcf.html").write_text(sd)
    print(f"Wrote sandiego_tcf.html ({len(sd)} bytes)")
    ca = fetch_html("https://www.alliancefrancaise.ca/en/language/exams/tcf-canada/")
    (FIXTURES / "canada_tcf.html").write_text(ca)
    print(f"Wrote canada_tcf.html ({len(ca)} bytes)")


if __name__ == "__main__":
    main()

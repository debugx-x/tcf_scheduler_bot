"""Parser tests for Scout. Uses saved HTML fixtures.

Fixtures live in tests/fixtures/. To refresh them against the live site:
    uv run python -m tests.refresh_fixtures
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from tcf_bot.scout import parse_exams_from_html

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


# --- AF Canada (primary target) ---


def test_canada_extracts_exam_rows():
    html = _load("canada_tcf.html")
    exams = parse_exams_from_html(html, source_url="https://www.alliancefrancaise.ca/test")
    assert len(exams) >= 1
    assert all(e.site == "af_canada" for e in exams)


def test_canada_extracts_opens_at_from_data_attribute():
    html = _load("canada_tcf.html")
    exams = parse_exams_from_html(html, source_url="https://www.alliancefrancaise.ca/test")
    with_opens = [e for e in exams if e.opens_at is not None]
    assert len(with_opens) >= 1
    for e in with_opens:
        assert isinstance(e.opens_at, datetime)


def test_canada_extracts_prices():
    html = _load("canada_tcf.html")
    exams = parse_exams_from_html(html, source_url="https://www.alliancefrancaise.ca/test")
    assert all(e.price.startswith("$") for e in exams if e.price != "unknown")


def test_canada_exam_id_is_none_when_window_not_open():
    html = _load("canada_tcf.html")
    exams = parse_exams_from_html(html, source_url="https://www.alliancefrancaise.ca/test")
    # exam_id only materialises after the registration window has activated;
    # captured fixture has all windows in the future, so exam_id is None.
    assert all(e.exam_id is None for e in exams)


# --- AF San Diego (test harness — same Oncord platform) ---


def test_sandiego_uses_same_parser():
    html = _load("sandiego_tcf.html")
    exams = parse_exams_from_html(
        html,
        source_url="https://www.afsandiego.org/test",
        site="af_sandiego",
    )
    assert len(exams) >= 1
    assert all(e.site == "af_sandiego" for e in exams)


def test_sandiego_some_windows_have_exam_id():
    """SD typically has at least one currently-open registration with exam_id."""
    html = _load("sandiego_tcf.html")
    exams = parse_exams_from_html(
        html, source_url="https://www.afsandiego.org/test", site="af_sandiego"
    )
    # We don't require ALL exams to have an exam_id (future ones won't),
    # but at least one should — that's what makes SD a viable dry-run target.
    # If this ever fails, the SD page may have no open windows at capture time;
    # refresh the fixture and try again.
    assert any(e.exam_id is not None for e in exams) or all(
        e.status == "future" for e in exams
    )


# --- Status classification ---


def test_status_classification_for_past_window():
    html = """
    <table><tr class='tableRow'>
      <td><span class='es-exam-title'>TCF January 2, 2020</span></td>
      <td>Jan 01 2020 12:00am - Jan 02 2020 12:00am</td>
      <td>$450.00</td>
      <td><a href='/af/exam-selector/order/?exam_id=999'>Book</a></td>
    </tr></table>
    """
    exams = parse_exams_from_html(html, source_url="https://test")
    assert len(exams) == 1
    assert exams[0].status == "closed"
    assert exams[0].exam_id == 999

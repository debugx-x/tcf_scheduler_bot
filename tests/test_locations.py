"""Two-site behavior: AF Vancouver lists the same exam dates at Vancouver
(6161 Cambie St.) and New Westminster (320 Columbia St.) as separate rows with
separate release windows."""

from __future__ import annotations

from datetime import datetime, timedelta

from tcf_bot.booker import Slot, _match_open_candidate, _plan_slots
from tcf_bot.config import Config
from tcf_bot.scout import normalize_location, parse_exams_from_html

VAN = "Alliance Francaise - 6161 Cambie St., Vancouver"
NW = "Alliance Francaise - 320 Columbia St., New Westminster"
BASE = "https://www.alliancefrancaise.ca"


def _fmt(dt: datetime) -> str:
    return dt.strftime("%b %d %Y %I:%M%p").lower().capitalize()


def _row(
    title_date: str,
    location: str,
    opens: datetime,
    *,
    exam_id: int | None = None,
    sold_out: bool = False,
) -> str:
    closes = opens + timedelta(days=7)
    action = (
        f'<a href="/af/exam-selector/order/?exam_id={exam_id}">Book Now</a>'
        if exam_id
        else f'<span class="es-status" data-opens-at="{int(opens.timestamp())}">Opens in</span>'
    )
    return (
        '<tr class="tableRow">'
        f'<td><span class="es-exam-title">TCF-Canada {title_date}</span></td>'
        "<td>Written &amp; Oral</td>"
        f'<td width="200">{_fmt(opens)} -<br>{_fmt(closes)}</td>'
        f'<td align="left" width="200"><span>{location}</span></td>'
        f'<td width="110">{"SOLD OUT!" if sold_out else "20"}</td>'
        "<td>$390.00</td>"
        f"<td>{action}</td>"
        "</tr>"
    )


def _page(*rows: str) -> str:
    return "<table>" + "".join(rows) + "</table>"


def _cfg(**date_strategy) -> Config:
    return Config.model_validate(
        {
            "candidate": {
                "first_name": "Jane",
                "last_name": "Doe",
                "email": "jane@example.com",
                "phone": "1",
                "date_of_birth": "Jan 01 2000",
                "nationality": "Inde",
                "address_line1": "1 Main",
                "city": "Vancouver",
                "postal_code": "V0V0V0",
                "country": "Canada",
            },
            "date_strategy": date_strategy,
            "notifications": {
                "telegram": {"chat_id": "1"},
                "email": {"to": "jane@example.com", "from": "x"},
            },
        }
    )


SOON = datetime.now() + timedelta(minutes=1)
LATER = datetime.now() + timedelta(days=14)


# --- parsing --------------------------------------------------------------- #


def test_both_sites_parse_to_same_date_with_distinct_locations():
    # New Westminster titles carry a space before the comma.
    exams = parse_exams_from_html(
        _page(_row("December 2 , 2026", NW, SOON), _row("December 2, 2026", VAN, SOON)),
        source_url="x",
    )
    assert [e.date for e in exams] == ["December 2, 2026", "December 2, 2026"]
    assert [e.location_key for e in exams] == ["new_westminster", "vancouver"]


def test_sold_out_rows_are_flagged():
    (e,) = parse_exams_from_html(_page(_row("December 2, 2026", VAN, SOON, sold_out=True)), "x")
    assert e.sold_out


def test_location_aliases():
    for text in ("New Westminster", "new-west", "NW", "columbia", NW):
        assert normalize_location(text) == "new_westminster"
    for text in ("Vancouver", "cambie", VAN, "Alliance Francaise Vancouver - 6161 Cambie St., Vancouver"):
        assert normalize_location(text) == "vancouver"


# --- planning -------------------------------------------------------------- #


def _exams(*rows: str):
    return parse_exams_from_html(_page(*rows), source_url="x")


def test_either_site_orders_candidates_by_preference():
    exams = _exams(_row("December 2, 2026", VAN, SOON), _row("December 2 , 2026", NW, SOON))
    cfg = _cfg(locations=["new_westminster", "vancouver"], target_dates=["December 2, 2026"])
    (slot,) = _plan_slots(exams, cfg)
    assert [c.location_key for c in slot.candidates] == ["new_westminster", "vancouver"]


def test_per_date_override_restricts_site():
    exams = _exams(_row("December 2, 2026", VAN, SOON), _row("December 2 , 2026", NW, SOON))
    cfg = _cfg(
        locations=["vancouver", "new_westminster"],
        target_dates=[{"date": "December 2, 2026", "locations": ["New Westminster"]}],
    )
    (slot,) = _plan_slots(exams, cfg)
    assert [c.location_key for c in slot.candidates] == ["new_westminster"]


def test_sold_out_preferred_site_leaves_only_alternate():
    exams = _exams(
        _row("December 2, 2026", VAN, SOON, sold_out=True),
        _row("December 2 , 2026", NW, SOON),
    )
    cfg = _cfg(locations=["vancouver", "new_westminster"], target_dates=["December 2, 2026"])
    (slot,) = _plan_slots(exams, cfg)
    assert [c.location_key for c in slot.candidates] == ["new_westminster"]


def test_disallowed_site_is_never_planned():
    exams = _exams(_row("December 2, 2026", VAN, SOON))
    cfg = _cfg(locations=["new_westminster"], target_dates=["December 2, 2026"],
               substitute_tolerance_days=0)
    assert _plan_slots(exams, cfg) == []


def test_later_release_is_left_for_its_own_strike():
    exams = _exams(_row("December 2, 2026", VAN, SOON), _row("December 2 , 2026", NW, LATER))
    cfg = _cfg(target_dates=["December 2, 2026"])
    (slot,) = _plan_slots(exams, cfg, release_cutoff=datetime.now() + timedelta(hours=1))
    assert [c.location_key for c in slot.candidates] == ["vancouver"]


def test_booked_target_is_skipped_and_its_date_counts_toward_gap():
    exams = _exams(
        _row("December 2 , 2026", NW, SOON),
        _row("December 14 , 2026", NW, SOON),
        _row("December 23 , 2026", NW, SOON),
    )
    cfg = _cfg(target_dates=["December 2, 2026", "December 16, 2026"])
    booked = [{"target": "December 2, 2026", "date": "December 2, 2026", "location": "vancouver"}]
    (slot,) = _plan_slots(exams, cfg, bookings=booked)
    # Dec 16 isn't offered; Dec 14 is nearer but only 12 days after the Dec 2
    # booking, so the retake gap pushes the substitute to Dec 23.
    assert slot.target == "December 16, 2026"
    assert slot.candidates[0].date == "December 23, 2026"


def test_earliest_mode_respects_global_locations():
    exams = _exams(_row("December 2, 2026", VAN, SOON), _row("December 4 , 2026", NW, SOON))
    cfg = _cfg(locations=["new_westminster"])
    assert [s.candidates[0].label for s in _plan_slots(exams, cfg)] == [
        "December 4, 2026 @ new_westminster"
    ]


# --- race matching --------------------------------------------------------- #


def _slot(exams, *keys):
    return Slot("December 2, 2026", [e for k in keys for e in exams if e.location_key == k])


def test_race_prefers_first_site_when_both_open():
    planned = _exams(_row("December 2, 2026", VAN, SOON), _row("December 2 , 2026", NW, SOON))
    live = _page(
        _row("December 2, 2026", VAN, SOON, exam_id=11),
        _row("December 2 , 2026", NW, SOON, exam_id=22),
    )
    slot = _slot(planned, "new_westminster", "vancouver")
    exam, url = _match_open_candidate(live, slot.candidates, BASE)
    assert exam.location_key == "new_westminster" and url.endswith("exam_id=22")


def test_race_falls_back_when_preferred_row_has_no_link():
    planned = _exams(_row("December 2, 2026", VAN, SOON), _row("December 2 , 2026", NW, SOON))
    live = _page(
        _row("December 2, 2026", VAN, SOON, exam_id=11),
        _row("December 2 , 2026", NW, SOON, sold_out=True),
    )
    slot = _slot(planned, "new_westminster", "vancouver")
    exam, url = _match_open_candidate(live, slot.candidates, BASE)
    assert exam.location_key == "vancouver" and url.endswith("exam_id=11")


def test_race_never_confuses_sites_for_the_same_date():
    planned = _exams(_row("December 2, 2026", VAN, SOON))
    live = _page(_row("December 2 , 2026", NW, SOON, exam_id=22))
    assert _match_open_candidate(live, planned, BASE) is None


def test_earliest_mode_orders_by_exam_date_not_page_order():
    # AF lists dates as text ("December 11" before "December 2").
    exams = _exams(
        _row("December 11, 2026", VAN, SOON),
        _row("December 2 , 2026", NW, SOON),
        _row("December 2, 2026", VAN, SOON),
    )
    cfg = _cfg(locations=["vancouver", "new_westminster"])
    assert [s.candidates[0].label for s in _plan_slots(exams, cfg)] == [
        "December 2, 2026 @ vancouver",
        "December 2, 2026 @ new_westminster",
        "December 11, 2026 @ vancouver",
    ]


def test_unimplemented_modes_are_rejected_not_silently_ignored():
    import pytest
    from pydantic import ValidationError

    for mode in ("specific_date", "ordered_list"):
        with pytest.raises(ValidationError, match="not implemented"):
            _cfg(mode=mode)

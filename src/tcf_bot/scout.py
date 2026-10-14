"""Scout: fetch AF Canada (and SD sister site) TCF pages, parse exam rows, persist state.

Both AF Canada and AF San Diego run on the same Oncord platform with identical
HTML: <tr class="tableRow"> rows containing <span class="es-exam-title">,
<span class="es-status" data-opens-at="<unix>"> for the countdown, and — when
the registration window is open or already passed — an anchor to
/af/exam-selector/order/?exam_id=NN. exam_id is only present once the window
has been activated; before then we just have the date + opens_at timestamp.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime

import httpx
from pydantic import BaseModel
from selectolax.parser import HTMLParser

from .config import REPO_ROOT, DateStrategy, TargetSite, load_config

STATE_DIR = REPO_ROOT / "state"
EXAMS_STATE_PATH = STATE_DIR / "exams.json"
# Ledger of successful bookings. Locations release on different days, so a
# later strike must know what an earlier one already booked — otherwise the
# New Westminster release could re-book a date already won at Vancouver.
BOOKED_PATH = STATE_DIR / "booked.json"


class Exam(BaseModel):
    date: str  # ISO-ish or human, e.g. "September 2, 2026"
    exam_id: int | None  # only available on SD or after window opens on Canada
    opens_at: datetime | None  # registration window start
    closes_at: datetime | None  # registration window end
    price: str  # raw, e.g. "$390.00"
    location: str = ""
    capacity: int | None = None
    status: str  # "future", "open", "closed", "unknown"
    source_url: str
    site: str  # "af_canada" or "af_sandiego"
    sold_out: bool = False

    @property
    def location_key(self) -> str:
        return normalize_location(self.location)

    @property
    def label(self) -> str:
        return f"{self.date} @ {self.location_key}" if self.location_key else self.date


def _parse_window_text(text: str) -> tuple[datetime | None, datetime | None]:
    """Parse 'Jun 15 2026 12:00pm - Jun 30 2026 4:00pm' (allows br between halves)."""
    if not text:
        return None, None
    # Normalize whitespace and dashes.
    normalized = re.sub(r"\s+", " ", text.replace("–", "-")).strip()
    m = re.search(
        r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{4}\s+\d{1,2}:\d{2}[ap]m)\s*-\s*"
        r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{4}\s+\d{1,2}:\d{2}[ap]m)",
        normalized,
    )
    if not m:
        return None, None
    try:
        start = datetime.strptime(m.group(1), "%b %d %Y %I:%M%p")
        end = datetime.strptime(m.group(2), "%b %d %Y %I:%M%p")
        return start, end
    except ValueError:
        return None, None


def _classify_status(opens: datetime | None, closes: datetime | None) -> str:
    if not opens and not closes:
        return "unknown"
    now = datetime.now()
    if opens and opens > now:
        return "future"
    if opens and closes and opens <= now <= closes:
        return "open"
    if closes and now > closes:
        return "closed"
    return "unknown"


_MONTHS = "(January|February|March|April|May|June|July|August|September|October|November|December)"


def parse_exams_from_html(
    html: str,
    source_url: str,
    site: str = TargetSite.AF_CANADA.value,
) -> list[Exam]:
    """Parse exam rows from an AF Oncord page (Canada or SD share the same DOM)."""
    tree = HTMLParser(html)
    exams: list[Exam] = []

    for row in tree.css("tr.tableRow"):
        title_node = row.css_first(".es-exam-title")
        if not title_node:
            continue
        title = title_node.text(strip=True)
        # New Westminster rows render "December 11 , 2026" (space before the
        # comma); Vancouver rows "December 11, 2026". Canonicalize both so the
        # same exam date compares equal across locations.
        date_match = re.search(rf"\b{_MONTHS}\s+(\d{{1,2}})\s*,\s*(\d{{4}})\b", title)
        date_str = (
            f"{date_match.group(1)} {int(date_match.group(2))}, {date_match.group(3)}"
            if date_match
            else title
        )

        # opens_at: from data-opens-at (unix seconds) when present, else from textual window.
        opens_at: datetime | None = None
        status_node = row.css_first(".es-status")
        if status_node:
            raw = status_node.attributes.get("data-opens-at") or ""
            if raw.isdigit():
                # Convert unix to local naive datetime so comparisons stay consistent.
                opens_at = datetime.fromtimestamp(int(raw))

        row_text = row.text(separator=" ", strip=True)
        text_opens, closes_at = _parse_window_text(row_text)
        if opens_at is None:
            opens_at = text_opens

        price_match = re.search(r"\$\d+(?:\.\d{2})?", row_text)
        price = price_match.group(0) if price_match else "unknown"

        # exam_id: only present once the window has activated (anchor tag rendered).
        exam_id: int | None = None
        order_link = row.css_first("a[href*='/exam-selector/order/']")
        if order_link:
            href = order_link.attributes.get("href", "") or ""
            m = re.search(r"exam_id=(\d+)", href)
            if m:
                exam_id = int(m.group(1))

        sold_out = "SOLD OUT" in row_text.upper()

        location = ""
        capacity: int | None = None
        for td in row.css("td"):
            txt = td.text(separator=" ", strip=True)
            if not location and ("Alliance" in txt or "Avenue" in txt or "Street" in txt):
                location = txt
            if capacity is None and txt.isdigit() and 1 <= int(txt) <= 999:
                capacity = int(txt)

        exams.append(
            Exam(
                date=date_str,
                exam_id=exam_id,
                opens_at=opens_at,
                closes_at=closes_at,
                price=price,
                location=location,
                capacity=capacity,
                status=_classify_status(opens_at, closes_at),
                source_url=source_url,
                site=site,
                sold_out=sold_out,
            )
        )
    return exams


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-CA,en;q=0.9",
    }
    with httpx.Client(headers=headers, follow_redirects=True, timeout=20) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def run_once() -> list[Exam]:
    cfg = load_config()
    base = cfg.site.af_canada_base_url
    path = cfg.site.af_canada_tcf_path
    url = base.rstrip("/") + path
    html = fetch_html(url)
    exams = parse_exams_from_html(html, source_url=url, site=TargetSite.AF_CANADA.value)
    # Sort chronologically by exam date when possible (helps `earliest_with_fallback`).
    exams.sort(key=lambda e: (e.opens_at or datetime.max))

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    EXAMS_STATE_PATH.write_text(
        json.dumps([e.model_dump(mode="json") for e in exams], indent=2, default=str)
    )
    (STATE_DIR / "last_scout.html").write_text(html)
    return exams


def load_state() -> list[Exam]:
    if not EXAMS_STATE_PATH.exists():
        return []
    return [Exam.model_validate(d) for d in json.loads(EXAMS_STATE_PATH.read_text())]


def load_bookings() -> list[dict]:
    try:
        return json.loads(BOOKED_PATH.read_text())
    except (OSError, ValueError):
        return []


def record_booking(target: str, exam: Exam) -> None:
    bookings = load_bookings()
    bookings.append(
        {
            "target": target,
            "date": exam.date,
            "location": exam.location_key,
            "exam_id": exam.exam_id,
            "booked_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BOOKED_PATH.write_text(json.dumps(bookings, indent=2))


# --------------------------------------------------------------------------- #
# Exam date parsing + min/max window filter (used by booker + scheduler).
# --------------------------------------------------------------------------- #


def parse_exam_date(date_str: str) -> date | None:
    """Parse an Exam.date string (e.g. 'September 2, 2026') into a date.
    Returns None if the string doesn't match any known format."""
    if not date_str:
        return None
    cleaned = re.sub(r"\s+,", ",", date_str.strip())
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def in_target_window(exam: Exam, strategy: DateStrategy) -> bool:
    """True if exam's date is inside the user's min/max bounds.
    Unparseable exam dates are accepted (better to attempt than silently drop)."""
    exam_d = parse_exam_date(exam.date)
    if exam_d is None:
        return True
    if strategy.min_exam_date:
        try:
            if exam_d < date.fromisoformat(strategy.min_exam_date):
                return False
        except ValueError:
            pass  # malformed config — fall through, accept
    if strategy.max_exam_date:
        try:
            if exam_d > date.fromisoformat(strategy.max_exam_date):
                return False
        except ValueError:
            pass
    return True


# --------------------------------------------------------------------------- #
# Locations. AF Vancouver runs TCF at two sites with identical exam dates but
# separate rows and separate release windows:
#   "Alliance Francaise - 6161 Cambie St., Vancouver"          -> vancouver
#   "Alliance Francaise - 320 Columbia St., New Westminster"   -> new_westminster
# --------------------------------------------------------------------------- #

_LOCATION_ALIASES = {
    "new_westminster": (
        "new westminster", "newwestminster", "new west", "newwest", "nw", "columbia",
    ),
    "vancouver": ("vancouver", "van", "cambie", "yvr"),
}


def normalize_location(text: str) -> str:
    """Map a row's location text, or a user-supplied name, to a stable key:
    "vancouver", "new_westminster", or a slug for an unrecognised site.
    New Westminster is checked first because older Vancouver labels carried
    "Vancouver" in the organisation name."""
    raw = (text or "").strip().lower()
    if not raw:
        return ""
    flat = f" {re.sub(r'[^a-z0-9]+', ' ', raw).strip()} "
    for key, aliases in _LOCATION_ALIASES.items():
        if any(f" {alias} " in flat for alias in aliases):
            return key
    # Unknown site: key on the city (text after the last comma).
    return re.sub(r"[^a-z0-9]+", "_", raw.rsplit(",", 1)[-1]).strip("_")


def location_rank(exam: Exam, allowed: list[str]) -> int | None:
    """Preference index of the exam's location within `allowed` (lower is
    better), or None when it isn't allowed. An empty `allowed` means any
    location, all equally preferred."""
    if not allowed:
        return 0
    keys = [normalize_location(a) for a in allowed]
    key = exam.location_key
    return keys.index(key) if key in keys else None

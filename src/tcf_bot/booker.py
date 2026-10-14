"""Playwright booker. The actual strike.

Strategy:
1. Pre-warm: launch headless Chromium, log in to AF, navigate to TCF page.
2. Race: poll the DOM at strike.poll_interval_ms, watching for the target row's
   countdown to flip to a clickable Begin Registration link. Page is reloaded
   every ~1s during the race in case Oncord doesn't swap the DOM automatically.
3. Click → navigate to the order form → fill from config (with interactive
   fallback for unknown fields via Telegram).
4. Fill payment from .env → submit (unless dry-run).
5. On any failure, fall back to the next chronological exam.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import httpx

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)
from rich.console import Console

from .config import REPO_ROOT, Config, Secrets, TargetSite, load_config, load_secrets
from .scout import Exam, load_bookings, parse_exams_from_html, record_booking
from .selectors import SELECTORS

console = Console()

RUNS_DIR = REPO_ROOT / "state" / "runs"
DISCOVERED_FIELDS_PATH = REPO_ROOT / "state" / "discovered_fields.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


@dataclass
class RunDirs:
    root: Path
    screenshots: Path
    log_path: Path
    fields_path: Path
    discovered: list[dict] = field(default_factory=list)

    @classmethod
    def create(cls, label: str, base_dir: Path | None = None) -> "RunDirs":
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        root = (base_dir or RUNS_DIR) / f"{ts}-{label}"
        root.mkdir(parents=True, exist_ok=True)
        return cls(
            root=root,
            screenshots=root,
            log_path=root / "run.log",
            fields_path=root / "discovered_fields.json",
        )

    def log(self, msg: str) -> None:
        line = f"[{datetime.now().isoformat()}] {msg}"
        console.print(line)
        with self.log_path.open("a") as f:
            f.write(line + "\n")

    def snap(self, page: Page, name: str) -> Path:
        path = self.screenshots / f"{name}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception as exc:  # noqa: BLE001
            self.log(f"screenshot {name} failed: {exc}")
        return path


# --------------------------------------------------------------------------- #
# Site-aware URL helpers
# --------------------------------------------------------------------------- #


def _site_urls(cfg: Config, site: TargetSite) -> dict[str, str]:
    if site == TargetSite.AF_CANADA:
        base = cfg.site.af_canada_base_url.rstrip("/")
        return {
            "base": base,
            "tcf": base + cfg.site.af_canada_tcf_path,
            "login": base + SELECTORS.login_path,
        }
    base = cfg.site.af_sandiego_base_url.rstrip("/")
    return {
        "base": base,
        "tcf": base + cfg.site.af_sandiego_tcf_path,
        "login": base + SELECTORS.login_path,
    }


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


def _dump_login_page(page: Page, runs: RunDirs) -> None:
    """When login fails, dump every visible input on the page so we can update
    selectors.py with the right names."""
    try:
        inputs = page.evaluate(
            """() => Array.from(document.querySelectorAll('input, button')).map(el => ({
                tag: el.tagName.toLowerCase(),
                type: el.type || '',
                name: el.name || '',
                id: el.id || '',
                placeholder: el.placeholder || '',
                visible: el.offsetParent !== null,
                text: (el.innerText || el.value || '').slice(0, 40),
            })).filter(i => i.visible)"""
        )
        (runs.root / "login_page_inputs.json").write_text(json.dumps(inputs, indent=2))
        runs.log(f"  dumped {len(inputs)} visible login inputs to login_page_inputs.json")
    except Exception as exc:  # noqa: BLE001
        runs.log(f"  could not dump login page: {exc}")


def _login(page: Page, urls: dict[str, str], secrets: Secrets, runs: RunDirs) -> bool:
    runs.log(f"navigating to login: {urls['login']}")
    page.goto(urls["login"], wait_until="domcontentloaded", timeout=10_000)
    runs.snap(page, "01-login")

    try:
        page.locator(SELECTORS.login_username_input).first.wait_for(
            state="visible", timeout=5_000
        )
        page.locator(SELECTORS.login_username_input).first.fill(secrets.af_username)
        page.locator(SELECTORS.login_password_input).first.fill(
            secrets.af_password.get_secret_value()
        )
    except Exception as exc:  # noqa: BLE001
        runs.log(f"login fill failed: {exc}")
        runs.snap(page, "01b-login-failed")
        _dump_login_page(page, runs)
        return False

    # Oncord login is AJAX-driven: clicking submit fires a POST to
    # /_public/Framework/HTTP/AJAX/server.php, then renders a dialog. The URL
    # stays at /community/login/ on success — so checking "did URL change?" is
    # the wrong signal. We submit ONCE (cascading strategies clears the
    # password field via Oncord's post-submit reset, killing the session) and
    # then navigate to a known page to verify session.
    runs.log("login submission: button.click()")
    try:
        page.locator(SELECTORS.login_submit_button).first.click()
    except Exception as exc:  # noqa: BLE001
        runs.log(f"  click failed: {exc}")
        runs.snap(page, "01b-login-failed")
        _dump_login_page(page, runs)
        return False

    # Wait for the AJAX submit + any redirects to settle. networkidle is the
    # right signal here because Oncord fires multiple async requests on submit.
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:  # noqa: BLE001
        page.wait_for_timeout(2_000)  # fallback if networkidle times out

    # Verify session by navigating to an authenticated-only page. The URL
    # stays the same regardless of auth state — Oncord renders either the
    # dashboard OR an embedded login form on /af/students/dashboard/. So the
    # signal is the PAGE CONTENT: the dashboard's embedded login form has a
    # password field named `login_contact_password` (different from the main
    # login page's `contact_password`). Its presence == not logged in.
    auth_url = urls["base"] + SELECTORS.auth_check_path
    try:
        page.goto(auth_url, wait_until="domcontentloaded", timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        runs.log(f"  auth-check navigation failed: {exc}")

    login_prompt_present = page.locator(SELECTORS.auth_check_login_prompt).count() > 0
    ok = not login_prompt_present
    runs.log(
        f"login success: {ok} (auth-check url: {page.url}, "
        f"login_prompt_on_dashboard: {login_prompt_present})"
    )
    runs.snap(page, "02-after-login")
    if not ok:
        _dump_login_page(page, runs)
    return ok


# --------------------------------------------------------------------------- #
# Exam selection (find the target row on the TCF page)
# --------------------------------------------------------------------------- #


def _scrape_exam_rows(page: Page, site: TargetSite) -> list[Exam]:
    html = page.content()
    return parse_exams_from_html(html, source_url=page.url, site=site.value)


_BOOKABLE = {"future", "open", "unknown"}


@dataclass
class Slot:
    """One booking to make. `candidates` are interchangeable exams on the same
    date at different sites, most preferred first: the race takes the first
    that opens, and falls back to the next if a seat can't be held."""

    target: str
    candidates: list[Exam]


def _plan_slots(
    exams: list[Exam],
    cfg: Config,
    bookings: list[dict] | None = None,
    release_cutoff: datetime | None = None,
) -> list[Slot]:
    """Decide what this run should try to book.

    `bookings` are earlier successes (state/booked.json): their targets are
    skipped and their dates count toward the retake gap. `release_cutoff`
    drops exams whose window opens after this run's race budget — they belong
    to a later release (e.g. the other site's) and get their own strike.
    """
    from .scout import in_target_window, location_rank, parse_exam_date

    strategy = cfg.date_strategy
    bookings = bookings or []

    def bookable(e: Exam) -> bool:
        if e.status not in _BOOKABLE or e.sold_out:
            return False
        return release_cutoff is None or e.opens_at is None or e.opens_at <= release_cutoff

    if strategy.target_dates:
        booked_targets = {b.get("target") for b in bookings}
        chosen_dates = [
            d for d in (parse_exam_date(b.get("date", "")) for b in bookings) if d
        ]
        tolerance = max(0, strategy.substitute_tolerance_days)
        gap = max(0, strategy.min_gap_days)
        used: list[Exam] = []
        slots: list[Slot] = []

        for target_raw, allowed in strategy.target_specs():
            if target_raw in booked_targets:
                console.print(f"[dim]target {target_raw!r} already booked — skipping[/dim]")
                continue
            wanted = parse_exam_date(target_raw)
            if wanted is None:
                console.print(f"[yellow]target_dates entry unparseable: {target_raw!r}[/yellow]")
                continue

            pool = [
                e for e in exams
                if bookable(e)
                and location_rank(e, allowed) is not None
                and parse_exam_date(e.date) is not None
                and e not in used
            ]

            def pref(e: Exam, allowed: list[str] = allowed) -> int:
                return location_rank(e, allowed)

            # Exact date: always honored (the user's explicit choice), at every
            # allowed site, most preferred site first.
            exact = sorted((e for e in pool if parse_exam_date(e.date) == wanted), key=pref)
            if exact:
                pick, cands = wanted, exact
            elif tolerance == 0:
                console.print(
                    f"[yellow]target {target_raw!r} not available; "
                    f"substitution disabled (tolerance=0)[/yellow]"
                )
                continue
            else:
                # Substitute: nearest date within tolerance that keeps the
                # retake gap from every date already chosen or booked.
                viable = sorted(
                    {
                        d for d in (parse_exam_date(e.date) for e in pool)
                        if abs((d - wanted).days) <= tolerance
                        and all(abs((d - c).days) >= gap for c in chosen_dates)
                    },
                    key=lambda d: (abs((d - wanted).days), d),
                )
                if not viable:
                    console.print(
                        f"[yellow]target {target_raw!r} unavailable and no substitute "
                        f"within {tolerance}d that keeps a {gap}-day gap — skipping[/yellow]"
                    )
                    continue
                pick = viable[0]
                cands = sorted((e for e in pool if parse_exam_date(e.date) == pick), key=pref)
                console.print(
                    f"[cyan]target {target_raw!r} unavailable → substituting {cands[0].date} "
                    f"({(pick - wanted).days:+d} days)[/cyan]"
                )

            slots.append(Slot(target_raw, cands))
            used.extend(cands)
            chosen_dates.append(pick)
        return slots

    # Other modes: min/max window + location filter, one exam per slot.
    allowed = strategy.locations
    pool = [
        e for e in exams
        if bookable(e)
        and in_target_window(e, strategy)
        and location_rank(e, allowed) is not None
    ]
    if strategy.mode.value == "earliest_with_fallback":
        # Earliest EXAM date first, then site preference. AF's listing sorts
        # dates as text ("December 11" before "December 2"), so page order
        # can't be trusted.
        pool.sort(
            key=lambda e: (
                parse_exam_date(e.date) or datetime.max.date(),
                location_rank(e, allowed),
                e.opens_at or datetime.max,
            )
        )
    return [Slot(e.date, [e]) for e in pool]

# --------------------------------------------------------------------------- #
# The race: wait for the Begin Registration link to appear on the target row.
# --------------------------------------------------------------------------- #


def _match_open_candidate(
    html: str, candidates: list[Exam], base: str
) -> tuple[Exam, str] | None:
    """First candidate, in preference order, whose listing row now carries an
    order link. Rows are matched on (exam date, site) — both sites list the
    same dates, so the date text alone is ambiguous — or on exam_id when the
    candidate already has one (`tcf strike --exam-id`)."""
    from .scout import parse_exam_date

    open_rows = [
        r for r in parse_exams_from_html(html, source_url="")
        if r.exam_id is not None and not r.sold_out
    ]
    by_key = {(parse_exam_date(r.date), r.location_key): r.exam_id for r in open_rows}
    open_ids = {r.exam_id for r in open_rows}
    for c in candidates:
        if c.exam_id is not None and c.exam_id in open_ids:
            exam_id = c.exam_id
        else:
            exam_id = by_key.get((parse_exam_date(c.date), c.location_key))
        if exam_id is not None:
            return c, f"{base}/af/exam-selector/order/?exam_id={exam_id}"
    return None


def _wait_for_window(
    page: Page,
    candidates: list[Exam],
    urls: dict[str, str],
    cfg: Config,
    runs: RunDirs,
) -> tuple[Exam, str] | None:
    """Race loop. Returns (candidate, order URL) for the most preferred
    candidate that opens, or None on timeout.

    Polls the PUBLIC listing page over plain HTTP — the browser is never
    reloaded. AF explicitly warns that "opening multiple tabs or repeatedly
    refreshing the page may cancel your reservation and prevent successful
    registration", so the logged-in browser session stays untouched until we
    have a URL to navigate to. HTTP GETs against the public listing carry no
    session cookies and can't endanger the login or any hold.
    """
    deadline = time.time() + cfg.strike.total_budget_seconds
    poll_s = max(cfg.strike.poll_interval_ms, 250) / 1000.0
    labels = " | ".join(c.label for c in candidates)
    runs.log(
        f"race loop start for {labels} — HTTP polling every {poll_s * 1000:.0f}ms "
        f"(budget={cfg.strike.total_budget_seconds}s, browser untouched)"
    )

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-CA,en;q=0.9",
    }
    with httpx.Client(headers=headers, follow_redirects=True, timeout=8) as client:
        errors = 0
        while time.time() < deadline:
            try:
                r = client.get(urls["tcf"])
                found = _match_open_candidate(r.text, candidates, urls["base"])
                errors = 0
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if errors in (1, 5, 20):  # log sparsely, don't spam
                    runs.log(f"  poll error #{errors}: {exc}")
                time.sleep(min(2.0, poll_s * 4))
                continue
            if found:
                runs.log(f"order URL discovered for {found[0].label}: {found[1]}")
                return found
            time.sleep(poll_s)

    # Last chance: the browser page has its own countdown JS that self-reloads
    # at window-open; check whatever it's showing now before giving up.
    found = _match_open_candidate(page.content(), candidates, urls["base"])
    if found:
        runs.log(f"order URL found via browser fallback for {found[0].label}: {found[1]}")
        return found

    runs.log(f"race loop expired without finding an order URL for {labels}")
    return None


def _grab_hold(
    page: Page,
    candidates: list[Exam],
    urls: dict[str, str],
    cfg: Config,
    runs: RunDirs,
    tag: str,
) -> tuple[Exam, str] | None:
    """Race for the first open candidate, open its order page and click Begin
    Registration (which starts the 10-minute hold). If the hold doesn't take —
    typically the seat sold out between the poll and the click — fall back to
    the next candidate site for the same date."""
    remaining = list(candidates)
    while remaining:
        found = _wait_for_window(page, remaining, urls, cfg, runs)
        if not found:
            return None
        exam, url = found
        page.goto(url, wait_until="domcontentloaded", timeout=10_000)
        runs.snap(page, f"{tag}a-order-{_slug(exam.label)}")

        begin = page.locator(SELECTORS.begin_registration_button)
        if begin.count() > 0:
            try:
                begin.first.click()
                page.wait_for_timeout(800)
            except Exception as exc:  # noqa: BLE001
                runs.log(f"[{tag}] Begin Registration click failed: {exc}")

        held = page.locator(SELECTORS.hold_countdown).count() > 0
        runs.log(f"[{tag}] {exam.label}: hold banner present = {held}")
        runs.snap(page, f"{tag}b-held-{_slug(exam.label)}")
        remaining = [c for c in remaining if c is not exam]
        if held or not remaining:
            return exam, url
        runs.log(f"[{tag}] no hold at {exam.label} — falling back to {remaining[0].label}")
    return None

# --------------------------------------------------------------------------- #
# Form fill (with interactive Telegram fallback for unknown fields)
# --------------------------------------------------------------------------- #


def _candidate_value_for(field_name: str, label: str, cfg: Config) -> str | None:
    """Return a sensible value from candidate config given a form field key/label."""
    cand = cfg.candidate.model_dump()
    # Direct key match first.
    name_l = (field_name or "").lower()
    label_l = (label or "").lower()
    # More-specific keywords first so e.g. "country_of_birth" wins over "country".
    candidates_by_keyword: list[tuple[Iterable[str], str]] = [
        (("first", "given"), "first_name"),
        (("last", "surname", "family"), "last_name"),
        (("email", "e-mail"), "email"),
        (("phone", "mobile", "tel"), "phone"),
        (("date_of_birth", "dob", "birthdate", "birth_date"), "date_of_birth"),
        (("gender", "sex"), "gender"),
        (("passport",), "passport_number"),
        (("id_number", "national_id", "identity_number", "identification_number"), "id_number"),
        (("candidate_number", "candidate_no", "previous_candidate"), "candidate_number"),
        (("mother_tongue", "native_language", "first_language", "langue_maternelle"), "mother_tongue"),
        (("country_of_birth", "birth_country", "pays_de_naissance"), "country_of_birth"),
        (("city_of_birth", "birth_city", "place_of_birth", "lieu_de_naissance", "ville_de_naissance"), "city_of_birth"),
        (("nationality", "citizenship", "nationalité"), "nationality"),
        (("address1", "address_line1", "street", "addressline1", "address_1"), "address_line1"),
        (("address2", "address_line2", "suite", "apt", "addressline2", "address_2"), "address_line2"),
        (("postal", "zip", "postcode"), "postal_code"),
        (("province", "state", "region"), "province"),
        (("country",), "country"),
        (("city", "town"), "city"),
    ]
    for keywords, key in candidates_by_keyword:
        if any(k in name_l or k in label_l for k in keywords):
            value = cand.get(key)
            if value:
                return str(value)
    # Fall through: also check extra fields the user has appended.
    for k, v in cand.items():
        if k == field_name and v:
            return str(v)
    return None


def _record_discovered_field(runs: RunDirs, field_info: dict) -> None:
    runs.discovered.append(field_info)
    runs.fields_path.write_text(json.dumps(runs.discovered, indent=2))


def _try_fill(page: Page, selector: str, value: str, runs: RunDirs, label: str) -> bool:
    """Fill a single field by selector. Logs and swallows errors so one missing
    field doesn't crash the whole strike — the form will surface errors at submit
    and the screenshot captures the state."""
    if not value:
        return False
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return False
        loc.fill(value)
        runs.log(f"  ✓ {label} ← {value!r}")
        return True
    except Exception as exc:  # noqa: BLE001
        runs.log(f"  ✗ {label} fill failed: {exc}")
        return False


def _try_check(page: Page, selector: str, runs: RunDirs, label: str) -> bool:
    """Check a checkbox/radio. Use force=True because Oncord forms style the
    native input as visually hidden and place a custom label on top — without
    force, Playwright fails with 'element is not visible'."""
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return False
        loc.check(force=True)
        runs.log(f"  ✓ {label} (checked)")
        return True
    except Exception as exc:  # noqa: BLE001
        runs.log(f"  ✗ {label} check failed: {exc}")
        return False


def _telegram_pick_then_combobox(
    page: Page,
    hidden_name: str,
    near_hidden_selector: str,
    tried: list[str],
    label: str,
    cfg: Config,
    runs: RunDirs,
) -> bool:
    """When all configured candidate strings fail, list whatever options the
    combobox currently shows, ask the user via Telegram, then try the reply."""
    from .notify import ask_for_field

    visible_options = page.evaluate(
        """name => {
            const cb = document.querySelector(`oncord-combobox[name="${name}"]`);
            if (!cb) return [];
            const out = [];
            cb.querySelectorAll('[role=option], [class*="option"], li, button, a, span, div')
              .forEach(el => {
                if (el.offsetParent === null) return;
                const t = (el.innerText || '').trim();
                if (t && t.length < 80 && !out.includes(t)) out.push(t);
              });
            return out.slice(0, 25);
        }""",
        hidden_name,
    ) or []
    runs.log(f"  {label} visible combobox options: {visible_options}")

    try:
        pick = ask_for_field(
            field_name=label,
            label=f"{label} — none of {tried} matched. Pick one:",
            options=visible_options or None,
            timeout_seconds=cfg.interactive_fallback.timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        runs.log(f"  ask_for_field failed: {exc}")
        return False
    if not pick:
        return False
    _type_into_oncord_combobox(page, hidden_name, pick)
    page.wait_for_timeout(500)
    clicked_text = _click_matching_option_in_combobox(page, hidden_name, pick)
    resolved = _autocomplete_hidden_value(page, near_hidden_selector)
    if clicked_text and resolved:
        runs.log(f"  ✓ {label} ← {pick!r} via Telegram (hidden={resolved!r})")
        return True
    return False


def _oncord_combobox_name(near_hidden_selector: str) -> str | None:
    """Parse the hidden field's name out of an `input[name='X']` selector so we
    can find the surrounding <oncord-combobox name='X'> Web Component."""
    m = re.search(r"name=['\"]([^'\"]+)['\"]", near_hidden_selector)
    return m.group(1) if m else None


def _open_oncord_combobox(page: Page, hidden_name: str) -> bool:
    """Click the <oncord-combobox> to open its popover. Returns True if the
    popover became visible (show='true')."""
    return page.evaluate(
        """name => {
            const cb = document.querySelector(`oncord-combobox[name="${name}"]`);
            if (!cb) return false;
            // Try the visible #search input first (clicking it usually opens the popover).
            const search = cb.querySelector('input#search');
            if (search) { search.click(); search.focus(); }
            // Also try the search-toggle button.
            const toggle = cb.querySelector('button#search-toggle');
            if (toggle) toggle.click();
            // Confirm popover opened.
            const attach = cb.querySelector('oncord-effect-attach#attach-options');
            return !attach || attach.getAttribute('show') === 'true' ||
                   cb.querySelector('input#mobile-search-input')?.offsetParent !== null;
        }""",
        hidden_name,
    )


def _type_into_oncord_combobox(page: Page, hidden_name: str, value: str) -> None:
    """Clear and type into the combobox's real searchable input (#mobile-search-input)
    inside the popover."""
    # Use evaluate to set focus then dispatch a real input — Playwright's
    # locator.fill() can struggle with non-standard custom elements.
    page.evaluate(
        """args => {
            const [name, val] = args;
            const cb = document.querySelector(`oncord-combobox[name="${name}"]`);
            if (!cb) return;
            const inp = cb.querySelector('input#mobile-search-input') ||
                        cb.querySelector('input#search');
            if (!inp) return;
            inp.focus();
            inp.value = '';
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            // Now type character by character to trigger any per-keystroke handlers.
            inp.value = val;
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(new KeyboardEvent('keyup', { key: val.slice(-1), bubbles: true }));
        }""",
        [hidden_name, value],
    )


def _click_matching_option_in_combobox(page: Page, hidden_name: str, value: str) -> str | None:
    """Find any visible clickable element inside the combobox that contains the
    value text and click it. Returns the clicked option's visible text on success."""
    return page.evaluate(
        """args => {
            const [name, val] = args;
            const cb = document.querySelector(`oncord-combobox[name="${name}"]`);
            if (!cb) return null;
            const v = val.toLowerCase();
            // Option markup varies — try many shapes. Skip the known toggle buttons.
            const skip = new Set(['search-toggle', 'dropdown-close', 'clear', 'search']);
            const candidates = cb.querySelectorAll(
                '[role=option], [class*="option"], li, button, [tabindex], a, span, div'
            );
            for (const c of candidates) {
                if (c.id && skip.has(c.id)) continue;
                if (c.offsetParent === null) continue;
                const text = (c.innerText || c.textContent || '').trim();
                if (!text) continue;
                if (text.toLowerCase().includes(v)) {
                    c.click();
                    return text.slice(0, 80);
                }
            }
            return null;
        }""",
        [hidden_name, value],
    )


def _focus_autocomplete_search(page: Page, near_hidden_selector: str) -> bool:
    """Legacy heuristic — kept as a fallback for non-Oncord-Combobox sites
    (e.g. AF San Diego). The primary path now uses _open_oncord_combobox."""
    return page.evaluate(
        """sel => {
            const hidden = document.querySelector(sel);
            if (!hidden) return false;

            function focusIt(el, strategy) {
                if (!el || el === hidden) return null;
                if (el.offsetParent === null) return null;
                el.focus();
                el.click();
                if (document.activeElement === el) return strategy;
                return null;
            }

            // Strategy 1 (the SD pattern): closest div/p/fieldset, sibling input.
            const root1 = hidden.closest('div,p,fieldset,form');
            if (root1) {
                for (const c of root1.querySelectorAll('input[type=search], input[type=text]')) {
                    const r = focusIt(c, 'closest-container/text-or-search');
                    if (r) return r;
                }
            }

            // Strategy 2: walk up to 8 ancestors, look at all input-like descendants.
            let node = hidden.parentElement;
            for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
                for (const c of node.querySelectorAll(
                    'input[type=search], input[type=text], input:not([type]), [role=combobox], [role=textbox]'
                )) {
                    const r = focusIt(c, `ancestor-walk-${i}/input-or-combobox`);
                    if (r) return r;
                }
            }

            // Strategy 3: label[for=hidden.id], then nearest input thereafter.
            if (hidden.id) {
                const lbl = document.querySelector(`label[for="${hidden.id}"]`);
                if (lbl) {
                    let nxt = lbl.nextElementSibling;
                    for (let i = 0; i < 5 && nxt; i++, nxt = nxt.nextElementSibling) {
                        if (nxt.matches('input, [role=combobox], [role=textbox]')) {
                            const r = focusIt(nxt, 'label-sibling');
                            if (r) return r;
                        }
                        const inside = nxt.querySelector('input, [role=combobox], [role=textbox]');
                        if (inside) {
                            const r = focusIt(inside, 'label-sibling-descendant');
                            if (r) return r;
                        }
                    }
                }
            }

            // Strategy 4: anything matching common autocomplete class names anywhere.
            const widgetSelector =
                '[class*="autocomplete" i] input, [class*="combobox" i] input, ' +
                '[class*="select2" i] input, [class*="chosen" i] input';
            for (const c of document.querySelectorAll(widgetSelector)) {
                if (!c.offsetParent) continue;
                // Prefer those nearest the hidden input by position.
                const r = focusIt(c, 'class-name-widget');
                if (r) return r;
            }

            // Strategy 5: hidden.id often follows a pattern like `contact_gael_X` whose
            // visible counterpart is `contact_gael_X_input` or `X_search`.
            if (hidden.id) {
                for (const suffix of ['_input', '_search', '_combobox', '_visible']) {
                    const el = document.getElementById(hidden.id + suffix);
                    const r = focusIt(el, `id-suffix-${suffix}`);
                    if (r) return r;
                }
                // Also try replacing 'hidden' with 'input' if name contains it.
                for (const variant of [
                    hidden.id.replace('_hidden', '_input'),
                    hidden.id.replace('_id', '_input'),
                    hidden.id.replace('_code', '_input'),
                ]) {
                    if (variant !== hidden.id) {
                        const el = document.getElementById(variant);
                        const r = focusIt(el, `id-variant-${variant}`);
                        if (r) return r;
                    }
                }
            }

            return false;
        }""",
        near_hidden_selector,
    )


def _autocomplete_hidden_value(page: Page, selector: str) -> str:
    try:
        return page.locator(selector).first.get_attribute("value") or ""
    except Exception:  # noqa: BLE001
        return ""


def _autocomplete_clear(page: Page) -> None:
    """Clear the currently-focused autocomplete search input."""
    try:
        page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(150)
    except Exception:  # noqa: BLE001
        pass


def _try_one_autocomplete_value(page: Page, value: str) -> bool:
    """Type a single value, click matching option, return True if a click landed."""
    page.keyboard.type(value, delay=20)
    page.wait_for_timeout(400)
    return page.evaluate(
        """val => {
            const v = val.toLowerCase();
            const opts = document.querySelectorAll(
                "li[role='option'], .autocomplete-suggestion, [class*='option']"
            );
            for (const o of opts) {
                if (o.offsetParent === null) continue;
                if ((o.innerText || '').toLowerCase().includes(v)) {
                    o.click();
                    return true;
                }
            }
            return false;
        }""",
        value,
    )


def _list_visible_options(page: Page) -> list[str]:
    return page.evaluate(
        """() => {
            const opts = document.querySelectorAll(
                "li[role='option'], .autocomplete-suggestion, [class*='option']"
            );
            const out = [];
            for (const o of opts) {
                if (o.offsetParent === null) continue;
                const t = (o.innerText || '').trim();
                if (t) out.push(t);
            }
            return out;
        }"""
    )


def _fill_autocomplete(
    page: Page,
    near_hidden_selector: str,
    values,
    runs: RunDirs,
    label: str,
    cfg: Config | None = None,
) -> bool:
    """Fill an autocomplete widget by trying each candidate string in order.
    Verifies success by reading the hidden input's value attribute. On total
    miss, prompts via Telegram for a human pick (and persists the answer).

    Two execution paths:
      1. <oncord-combobox> custom element (AF Canada): open the popover, type
         into #mobile-search-input, click matching option. This is the primary
         path now — confirmed via 2026-06-17 form.html capture.
      2. Generic legacy heuristic (AF San Diego + anything else): walk the
         DOM looking for an adjacent visible text/search input.
    """
    if not values:
        return False
    candidates = [values] if isinstance(values, str) else [v for v in values if v]
    if not candidates:
        return False

    # --- Primary path: <oncord-combobox> ---
    hidden_name = _oncord_combobox_name(near_hidden_selector)
    if hidden_name:
        cb_count = page.locator(f"oncord-combobox[name='{hidden_name}']").count()
        if cb_count > 0:
            try:
                _open_oncord_combobox(page, hidden_name)
                page.wait_for_timeout(300)
                for value in candidates:
                    _type_into_oncord_combobox(page, hidden_name, value)
                    page.wait_for_timeout(500)  # AJAX option fetch
                    clicked_text = _click_matching_option_in_combobox(
                        page, hidden_name, value
                    )
                    resolved = _autocomplete_hidden_value(page, near_hidden_selector)
                    if clicked_text and resolved:
                        runs.log(
                            f"  ✓ {label} ← {value!r} via combobox "
                            f"(clicked={clicked_text!r}, hidden={resolved!r})"
                        )
                        return True
                    runs.log(
                        f"  · {label}: {value!r} via combobox did not resolve "
                        f"(clicked={clicked_text!r}, hidden={resolved!r})"
                    )
                # Combobox approach exhausted candidates without success.
                runs.log(f"  ✗ {label}: oncord-combobox candidates all failed")
                if cfg and cfg.interactive_fallback.enabled:
                    return _telegram_pick_then_combobox(
                        page, hidden_name, near_hidden_selector,
                        candidates, label, cfg, runs,
                    )
                return False
            except Exception as exc:  # noqa: BLE001
                runs.log(f"  · {label}: oncord-combobox path crashed ({exc}); trying legacy")

    # --- Fallback: legacy heuristic (SD-style) ---
    try:
        focus_strategy = _focus_autocomplete_search(page, near_hidden_selector)
        if not focus_strategy:
            runs.log(f"  ✗ {label}: no focus strategy worked for {near_hidden_selector!r}")
            # Last-ditch fallback: set the hidden input's value directly via JS
            # and dispatch change events. May or may not satisfy form validation.
            for value in candidates:
                set_ok = page.evaluate(
                    """args => {
                        const [sel, val] = args;
                        const hidden = document.querySelector(sel);
                        if (!hidden) return false;
                        hidden.value = val;
                        hidden.dispatchEvent(new Event('input', { bubbles: true }));
                        hidden.dispatchEvent(new Event('change', { bubbles: true }));
                        return hidden.value === val;
                    }""",
                    [near_hidden_selector, value],
                )
                if set_ok:
                    runs.log(f"  ⚠ {label} ← {value!r} (set via JS direct, no widget interaction)")
                    return True
            return False

        runs.log(f"  · {label}: focus strategy = {focus_strategy}")
        for value in candidates:
            _autocomplete_clear(page)
            clicked = _try_one_autocomplete_value(page, value)
            resolved = _autocomplete_hidden_value(page, near_hidden_selector)
            if clicked and resolved:
                runs.log(f"  ✓ {label} ← {value!r} (hidden={resolved!r})")
                return True
            runs.log(f"  · {label}: {value!r} did not resolve (clicked={clicked}, hidden={resolved!r})")

        # Total miss — capture visible options and ask via Telegram if enabled.
        _autocomplete_clear(page)
        page.wait_for_timeout(300)
        opts = _list_visible_options(page)
        runs.log(f"  ✗ {label}: none of {candidates} matched. Visible options: {opts[:20]}")
        if cfg and cfg.interactive_fallback.enabled and opts:
            try:
                from .notify import ask_for_field

                pick = ask_for_field(
                    field_name=label,
                    label=f"{label} — none of {candidates} matched. Pick one:",
                    options=opts[:25],
                    timeout_seconds=cfg.interactive_fallback.timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                runs.log(f"  ask_for_field failed: {exc}")
                pick = None
            if pick:
                _autocomplete_clear(page)
                if _try_one_autocomplete_value(page, pick):
                    resolved = _autocomplete_hidden_value(page, near_hidden_selector)
                    if resolved:
                        runs.log(f"  ✓ {label} ← {pick!r} (via Telegram, hidden={resolved!r})")
                        return True
        return False
    except Exception as exc:  # noqa: BLE001
        runs.log(f"  ✗ {label} autocomplete crashed: {exc}")
        return False


def _fill_known_fields(page: Page, cfg: Config, runs: RunDirs) -> None:
    """Fill all the fields we know about by exact name=. Order matters less,
    but we do candidate info first, then identity, then preferences, then consent."""
    c = cfg.candidate
    runs.log("filling candidate info…")
    _try_fill(page, SELECTORS.field_first_name, c.first_name, runs, "first_name")
    _try_fill(page, SELECTORS.field_last_name, c.last_name, runs, "last_name")
    _try_fill(page, SELECTORS.field_email, str(c.email), runs, "email")
    # Skip password if logged-in (field absent); fill if present.
    _try_fill(page, SELECTORS.field_phone, c.phone, runs, "phone")
    _try_fill(page, SELECTORS.field_dob, _normalize_dob(c.date_of_birth), runs, "dob")
    _try_fill(page, SELECTORS.field_address, c.address_line1, runs, "address")
    _try_fill(page, SELECTORS.field_city, c.city, runs, "city")
    _try_fill(page, SELECTORS.field_state, c.province, runs, "province")
    _try_fill(page, SELECTORS.field_postcode, c.postal_code, runs, "postcode")

    gender = (c.gender or "").strip().lower()[:1]  # accept "M", "Male", "m", …
    if gender == "m":
        _try_check(page, SELECTORS.field_gender_male, runs, "gender=m")
    elif gender == "f":
        _try_check(page, SELECTORS.field_gender_female, runs, "gender=f")

    runs.log("filling autocomplete widgets…")
    _fill_autocomplete(page, SELECTORS.field_country_hidden, c.country, runs, "country", cfg)

    runs.log("filling TCF identity…")
    _try_fill(
        page,
        SELECTORS.field_passport,
        c.passport_number or c.id_number,
        runs,
        "passport_or_id",
    )
    _try_fill(page, SELECTORS.field_candidate_number, c.candidate_number, runs, "candidate_number")
    _try_fill(page, SELECTORS.field_city_of_birth, c.city_of_birth, runs, "city_of_birth")
    _fill_autocomplete(
        page, SELECTORS.field_mother_tongue_hidden, c.mother_tongue, runs, "mother_tongue", cfg
    )
    _fill_autocomplete(
        page, SELECTORS.field_nationality_hidden, c.nationality, runs, "nationality", cfg
    )
    _fill_autocomplete(
        page, SELECTORS.field_country_of_birth_hidden, c.country_of_birth, runs, "country_of_birth", cfg
    )
    _fill_autocomplete(
        page, SELECTORS.field_disability_hidden, c.has_disability, runs, "has_disability", cfg
    )

    runs.log("filling marketing / preferences…")
    for option in c.how_heard:
        _try_check(
            page,
            f"{SELECTORS.field_how_heard_prefix}[value={option!r}]",
            runs,
            f"how_heard={option}",
        )

    # Defensive: explicitly tick "Myself" on the buy_for radio group. Logged-in
    # users have this pre-selected — but if AF changes the default we'd
    # otherwise silently book for someone else. Triggers an AJAX re-render,
    # which is why we do it before consent boxes (so the form is settled).
    _try_check(page, SELECTORS.field_buy_for_myself, runs, "buy_for=myself")

    runs.log("ticking consent boxes…")
    if c.acknowledge_french_law:
        _try_check(page, SELECTORS.field_acknowledge_french_law, runs, "ack_french_law")
    if c.agree_to_terms:
        # AF Canada renders TWO agree_terms_product_* checkboxes; SD has one.
        # Check every match. force=True because the visible UI is a styled
        # label and the actual <input> is hidden.
        try:
            terms = page.locator(SELECTORS.field_agree_terms)
            n_terms = terms.count()
            for i in range(n_terms):
                try:
                    terms.nth(i).check(force=True)
                    runs.log(f"  ✓ agree_terms[{i}] (checked)")
                except Exception as exc:  # noqa: BLE001
                    runs.log(f"  ✗ agree_terms[{i}] failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            runs.log(f"  ✗ agree_terms enumeration failed: {exc}")
    _try_check(page, SELECTORS.payment_method_card_radio, runs, "payment_method=card")


def _discover_unknown_fields(page: Page, cfg: Config, runs: RunDirs) -> None:
    """Iterate over visible form inputs we haven't filled yet, prompt via Telegram."""
    from .notify import ask_for_field

    snapshot = page.evaluate(
        """() => {
            const out = [];
            document.querySelectorAll('form input, form select, form textarea').forEach((el, i) => {
                if (el.type === 'hidden') return;
                if (el.disabled) return;
                if (el.offsetParent === null) return;  // hidden
                // Skip already-filled
                if (el.type === 'checkbox' || el.type === 'radio') {
                    if (el.checked) return;
                } else if (el.value) {
                    return;
                }
                if (el.name && el.name.startsWith('_as')) return;
                let label = '';
                if (el.labels && el.labels[0]) label = el.labels[0].innerText;
                out.push({
                    index: i,
                    name: el.name || el.id || '',
                    type: el.type || el.tagName.toLowerCase(),
                    label: label.trim().slice(0, 120),
                    options: el.tagName === 'SELECT'
                        ? Array.from(el.options).slice(0, 30).map(o => o.innerText)
                        : null,
                });
            });
            return out;
        }"""
    )
    if not snapshot:
        return
    runs.log(f"{len(snapshot)} field(s) still blank after targeted fill")
    for info in snapshot:
        _record_discovered_field(runs, info)
        if not cfg.interactive_fallback.enabled:
            continue
        if not info["name"]:
            continue
        try:
            value = ask_for_field(
                field_name=info["name"],
                label=info["label"],
                options=info["options"],
                timeout_seconds=cfg.interactive_fallback.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            runs.log(f"  ask_for_field failed for {info['name']}: {exc}")
            continue
        if value is None:
            continue
        try:
            page.locator(f"[name={info['name']!r}]").first.fill(value)
        except Exception as exc:  # noqa: BLE001
            runs.log(f"  fill failed for {info['name']}: {exc}")


def _fill_form(
    page: Page,
    cfg: Config,
    runs: RunDirs,
    abort_before_payment: bool,
) -> bool:
    """Fill the entire single-page form. AF Canada/SD have no separate continue
    step — candidate, TCF identity, consent, and payment are on one page; only
    the card data needs an iframe."""
    runs.snap(page, "10-form-start")
    # Persist the form HTML + autocomplete-widget DOM so we can debug selector
    # mismatches offline (the postmortem after 2026-06-15 showed we had only
    # screenshots, not DOM — fixing that here).
    _dump_form_dom(page, runs)
    _fill_known_fields(page, cfg, runs)
    _discover_unknown_fields(page, cfg, runs)
    runs.snap(page, "11-form-filled")
    return True


def _dump_form_dom(page: Page, runs: RunDirs) -> None:
    """Save the page HTML + a JSON inventory of every autocomplete widget's
    DOM structure. Lets us fix selector heuristics from logs alone."""
    try:
        (runs.root / "form.html").write_text(page.content())
        runs.log(f"  form HTML saved to {runs.root / 'form.html'}")
    except Exception as exc:  # noqa: BLE001
        runs.log(f"  form HTML dump failed: {exc}")

    # Hidden-input names we expect to drive autocomplete widgets.
    hidden_names = [
        SELECTORS.field_country_hidden,
        SELECTORS.field_mother_tongue_hidden,
        SELECTORS.field_nationality_hidden,
        SELECTORS.field_country_of_birth_hidden,
        SELECTORS.field_disability_hidden,
    ]
    try:
        report = page.evaluate(
            """selectors => {
                const out = [];
                for (const sel of selectors) {
                    const hidden = document.querySelector(sel);
                    if (!hidden) { out.push({ selector: sel, found: false }); continue; }
                    // Walk up to 6 ancestors, recording each container's tag, class, id,
                    // and the names/types of all input descendants. That's enough to see
                    // the structure surrounding the widget.
                    const ancestors = [];
                    let node = hidden;
                    for (let i = 0; i < 6 && node; i++) {
                        const inputs = [];
                        if (node.querySelectorAll) {
                            node.querySelectorAll('input, button, [role=combobox]').forEach(inp => {
                                inputs.push({
                                    tag: inp.tagName.toLowerCase(),
                                    type: inp.type || '',
                                    name: inp.name || '',
                                    id: inp.id || '',
                                    role: inp.getAttribute('role') || '',
                                    placeholder: inp.placeholder || '',
                                    visible: inp.offsetParent !== null,
                                });
                            });
                        }
                        ancestors.push({
                            level: i,
                            tag: node.tagName ? node.tagName.toLowerCase() : '(none)',
                            id: node.id || '',
                            class: (node.className && node.className.toString) ? node.className.toString().slice(0, 80) : '',
                            inputs: inputs.slice(0, 12),
                        });
                        node = node.parentElement;
                    }
                    out.push({ selector: sel, found: true, ancestors });
                }
                return out;
            }""",
            hidden_names,
        )
        (runs.root / "autocomplete_dom.json").write_text(json.dumps(report, indent=2))
        runs.log(f"  autocomplete DOM dumped to {runs.root / 'autocomplete_dom.json'}")
    except Exception as exc:  # noqa: BLE001
        runs.log(f"  autocomplete DOM dump failed: {exc}")


def _stripe_frame(page: Page, title_substring: str):
    """Resolve a Stripe iframe by its title (each card field is its own iframe)."""
    return page.frame_locator(f"iframe[title*={title_substring!r}]").first


def _advance_to_payment(page: Page, runs: RunDirs) -> None:
    """Confirm we're on the card form. AF Canada and AF SD both serve the
    card form on `/af/exam-selector/order/?exam_id=N` itself — the card section
    only renders once `payment_method_id` = Card is ticked. AF Canada may also
    redirect through `/commerce/order/` → `/commerce/order/final/` in older
    flows, hence the Proceed-button hop.

    Retries with a short backoff because the card section sometimes renders a
    beat after the radio change."""
    for hop in range(3):
        url = page.url
        runs.log(f"  payment-hop {hop}: at {url}")
        # Wait up to 1.5s for either Stripe iframe or authnet card input
        # to appear — handles transient rendering delays.
        for retry in range(3):
            if (
                page.locator(f"iframe[title*={SELECTORS.stripe_card_iframe_title!r}]").count() > 0
                or page.locator(SELECTORS.authnet_card_number).count() > 0
            ):
                if retry > 0:
                    runs.log(f"    card form appeared after {retry * 500}ms wait")
                return
            page.wait_for_timeout(500)
        # Otherwise, if a Proceed-style button is visible, click it.
        proceed = page.locator(SELECTORS.cart_proceed_button).first
        if proceed.count() > 0:
            runs.log("  clicking Proceed → advancing to next checkout step")
            try:
                with page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                    proceed.click()
            except PWTimeoutError:
                runs.log("  navigation after Proceed timed out; continuing anyway")
            continue
        runs.log("  no card form and no Proceed button — stopping advance")
        return


def _normalize_dob(raw: str) -> str:
    """The AF form's DOB picker expects 'MMM DD YYYY' (e.g. 'Jun 15 2026').
    Users commonly write ISO ('1995-01-15') or slash ('15/01/1995') formats.
    Convert known patterns; if we don't recognise it, return as-is and let the
    form complain via the screenshot."""
    if not raw:
        return raw
    s = raw.strip()
    # Already in the target format?
    try:
        datetime.strptime(s, "%b %d %Y")
        return s
    except ValueError:
        pass
    # Try common alternatives.
    for fmt in (
        "%Y-%m-%d",     # ISO 1995-01-15
        "%Y %m %d",     # 1995 01 15 (unquoted YAML date drops the dashes)
        "%d-%m-%Y",     # 15-01-1995
        "%d/%m/%Y",     # 15/01/1995
        "%m/%d/%Y",     # 01/15/1995
        "%Y/%m/%d",     # 1995/01/15
        "%d %b %Y",     # 15 Jan 1995
        "%B %d, %Y",    # January 15, 1995
        "%d %B %Y",     # 15 January 1995
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%b %d %Y")
        except ValueError:
            continue
    return s


def _split_expiry(expiry: str) -> tuple[str, str]:
    """Split 'MM/YY' or 'MM/YYYY' (or 'MM YY', 'MM-YY') into (month, year).
    Year is normalized to 4 digits (assume 20YY for 2-digit input)."""
    parts = re.split(r"[/\s\-]+", expiry.strip())
    if len(parts) != 2:
        return expiry, ""
    mm, yy = parts[0].zfill(2), parts[1]
    if len(yy) == 2:
        yy = "20" + yy
    return mm, yy


def _fill_card_data(page: Page, secrets: Secrets, runs: RunDirs) -> bool:
    """Detect processor by DOM markers, then fill card data accordingly.
    Returns True if all card fields got filled, False otherwise."""
    has_stripe = page.locator(
        f"iframe[title*={SELECTORS.stripe_card_iframe_title!r}]"
    ).count() > 0
    has_authnet = page.locator(SELECTORS.authnet_card_number).count() > 0

    if has_stripe:
        runs.log("payment processor: Stripe Elements (iframe) — AF San Diego style")
        try:
            _try_fill(page, SELECTORS.field_card_holder, secrets.card_holder, runs, "card_holder")
            _stripe_frame(page, SELECTORS.stripe_card_iframe_title).locator(
                SELECTORS.stripe_card_input
            ).fill(secrets.card_number.get_secret_value())
            _stripe_frame(page, SELECTORS.stripe_expiry_iframe_title).locator(
                SELECTORS.stripe_expiry_input
            ).fill(secrets.card_expiry)
            _stripe_frame(page, SELECTORS.stripe_cvc_iframe_title).locator(
                SELECTORS.stripe_cvc_input
            ).fill(secrets.card_cvv.get_secret_value())
            return True
        except Exception as exc:  # noqa: BLE001
            runs.log(f"  Stripe fill failed: {exc}")
            return False

    if has_authnet:
        runs.log("payment processor: Authorize.Net (direct inputs) — AF Canada style")
        try:
            mm, yyyy = _split_expiry(secrets.card_expiry)
            holder_ok = _try_fill(
                page, SELECTORS.authnet_card_holder, secrets.card_holder, runs, "card_holder"
            )
            number_ok = _try_fill(
                page,
                SELECTORS.authnet_card_number,
                secrets.card_number.get_secret_value(),
                runs,
                "card_number",
            )
            mm_ok = _try_fill(page, SELECTORS.authnet_expiry_month, mm, runs, "expiry_month")
            yy_ok = _try_fill(page, SELECTORS.authnet_expiry_year, yyyy, runs, "expiry_year")
            cvv_ok = _try_fill(
                page, SELECTORS.authnet_cvv, secrets.card_cvv.get_secret_value(), runs, "cvv"
            )
            if all([holder_ok, number_ok, mm_ok, yy_ok, cvv_ok]):
                return True
            runs.log(
                f"  Authorize.Net fill incomplete: holder={holder_ok} num={number_ok} "
                f"mm={mm_ok} yy={yy_ok} cvv={cvv_ok}"
            )
            return False
        except Exception as exc:  # noqa: BLE001
            runs.log(f"  Authorize.Net fill failed: {exc}")
            return False

    runs.log("payment processor: UNKNOWN (no Stripe iframe and no creditcard_number input)")
    return False


def _click_payment_submit(page: Page, runs: RunDirs) -> bool:
    """Click the right submit button for whichever page we landed on."""
    # Prefer the SD-specific #stripeSubmit if it exists.
    stripe_btn = page.locator(SELECTORS.submit_process_button)
    if stripe_btn.count() > 0:
        runs.log("clicking #stripeSubmit (PROCESS)")
        stripe_btn.first.click()
        return True
    # Else look for a generic submit/complete button.
    generic = page.locator(SELECTORS.submit_complete_order_button).first
    if generic.count() > 0:
        runs.log("clicking generic complete-order button")
        generic.click()
        return True
    runs.log("no submit button found")
    return False


def _fill_payment(
    page: Page,
    cfg: Config,
    secrets: Secrets,
    runs: RunDirs,
    dry_run: bool,
) -> bool:
    """Drive checkout: advance through any cart hops, fill the card form by
    whatever processor is on the final page, then click submit."""
    runs.snap(page, "19-pre-payment")
    _advance_to_payment(page, runs)
    runs.snap(page, "20-payment-page")

    ok = _fill_card_data(page, secrets, runs)
    if not ok:
        runs.snap(page, "20b-payment-fill-incomplete")
        # Let the human finish manually inside the 10-min hold.
        try:
            from .notify import send_outcome

            send_outcome(
                success=False,
                details={
                    "reason": "Unknown payment processor — please finish manually in the held window",
                    "url": page.url,
                },
                screenshot_path=runs.screenshots / "20b-payment-fill-incomplete.png",
            )
        except Exception:  # noqa: BLE001
            pass
        # Also dump any unknown fields for next-time selectors.
        try:
            _discover_unknown_fields(page, cfg, runs)
        except Exception:  # noqa: BLE001
            pass
        return False

    runs.snap(page, "21-payment-filled")
    if dry_run:
        runs.log("dry_run=True → NOT clicking submit")
        return True

    if not _click_payment_submit(page, runs):
        return False

    try:
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
    except PWTimeoutError:
        runs.log("post-submit load timed out (3DS/hCaptcha challenge?)")

    if page.locator(SELECTORS.hcaptcha_challenge).count() > 0:
        runs.log("hCaptcha challenge detected — sending alert")
        runs.snap(page, "22-hcaptcha")
        try:
            from .notify import send_outcome

            send_outcome(
                success=False,
                details={"reason": "hCaptcha challenge", "url": page.url},
                screenshot_path=runs.screenshots / "22-hcaptcha.png",
            )
        except Exception:  # noqa: BLE001
            pass

    if page.locator(SELECTORS.recaptcha_challenge).count() > 0:
        runs.log("reCAPTCHA challenge detected — sending alert (AF Canada uses Google reCAPTCHA)")
        runs.snap(page, "22-recaptcha")
        try:
            from .notify import send_outcome

            send_outcome(
                success=False,
                details={"reason": "reCAPTCHA challenge", "url": page.url},
                screenshot_path=runs.screenshots / "22-recaptcha.png",
            )
        except Exception:  # noqa: BLE001
            pass

    if page.locator(SELECTORS.three_ds_iframe).count() > 0:
        runs.log("3-D Secure iframe detected — sending alert")
        runs.snap(page, "22-3ds")
        try:
            from .notify import send_outcome

            send_outcome(
                success=False,
                details={"reason": "3DS verification required", "url": page.url},
                screenshot_path=runs.screenshots / "22-3ds.png",
            )
        except Exception:  # noqa: BLE001
            pass

    runs.snap(page, "23-after-payment")
    confirmed = page.locator(SELECTORS.confirmation_indicator).count() > 0
    runs.log(f"confirmation indicator present: {confirmed}")
    return confirmed


# --------------------------------------------------------------------------- #
# Manual takeover
# --------------------------------------------------------------------------- #


def _read_field_summary(runs: RunDirs) -> tuple[list[str], list[str]]:
    """Parse the run log for ✓/✗ markers to summarise what filled vs. didn't."""
    filled: list[str] = []
    failed: list[str] = []
    try:
        for line in runs.log_path.read_text().splitlines()[-200:]:
            # Lines look like "  ✓ first_name ← 'Jane'" or "  ✗ country ..."
            if "  ✓ " in line:
                # Extract the field label between ✓ and ← / ( / end-of-line.
                rest = line.split("  ✓ ", 1)[1]
                label = re.split(r"[←(]", rest, maxsplit=1)[0].strip()
                if label:
                    filled.append(label)
            elif "  ✗ " in line:
                rest = line.split("  ✗ ", 1)[1]
                label = re.split(r"[:←(]", rest, maxsplit=1)[0].strip()
                if label:
                    failed.append(label)
    except Exception:  # noqa: BLE001
        pass
    # Dedupe while preserving order.
    seen: set[str] = set()
    filled = [f for f in filled if not (f in seen or seen.add(f))]
    seen = set()
    failed = [f for f in failed if not (f in seen or seen.add(f))]
    return filled, failed


def _hold_open_for_manual_takeover(page: Page, cfg: Config, runs: RunDirs) -> None:
    """Keep the browser open after a failure so the human can finish the booking
    in the same window. Sends a richly detailed Telegram alert with what's
    filled, what's not, and the hold expiry time — then sleeps until either
    the hold expires or the user closes the browser themselves."""
    hold_seconds = max(0, cfg.strike.keep_open_on_failure_seconds)
    if hold_seconds == 0:
        return
    url = ""
    try:
        url = page.url
    except Exception:  # noqa: BLE001
        pass
    # `%-I` (no leading zero) is GNU/macOS only — Windows crashes on it.
    # Use %I and strip the leading zero manually for portability.
    _expires_dt = datetime.now() + timedelta(seconds=hold_seconds)
    expires_at = _expires_dt.strftime("%I:%M:%S %p").lstrip("0")
    runs.log(
        f"keeping browser open for up to {hold_seconds}s for manual takeover at {url}"
    )

    filled, failed = _read_field_summary(runs)
    fields_filled = ", ".join(filled[:15]) or "(none)"
    fields_failed = ", ".join(failed[:15]) or "(none)"
    if len(filled) > 15:
        fields_filled += f" … +{len(filled) - 15} more"
    if len(failed) > 15:
        fields_failed += f" … +{len(failed) - 15} more"

    instruction = (
        "🚨 The bot reserved a slot but couldn't finish.\n\n"
        "What to do:\n"
        "1. Open the Chromium window on your Mac (Cmd-Tab, look for 'Chromium').\n"
        "2. Log in if prompted (saved card details in your Apple Keychain may help).\n"
        f"3. Fill these blank fields: {fields_failed}\n"
        "4. Verify the consent checkboxes are ticked.\n"
        "5. Click PROCESS / authorizeSubmit at the bottom.\n\n"
        f"⏳ Hold expires at ~{expires_at} ({hold_seconds // 60} min from now)."
    )

    try:
        from .notify import send_outcome

        send_outcome(
            success=False,
            details={
                "event": "🆘 MANUAL TAKEOVER NEEDED",
                "url": url,
                "hold_expires_at": expires_at,
                "fields_filled": fields_filled,
                "fields_missing": fields_failed,
                "instructions": instruction,
            },
            screenshot_path=None,
        )
    except Exception as exc:  # noqa: BLE001
        runs.log(f"takeover notify failed: {exc}")

    # Poll for either timeout or the user closing the page themselves.
    deadline = time.time() + hold_seconds
    while time.time() < deadline:
        try:
            # `page.url` raises if the page/context is closed by the user.
            _ = page.url
        except Exception:
            runs.log("page closed by user — exiting takeover wait")
            return
        time.sleep(2)
    runs.log("manual-takeover window expired; closing browser")


# --------------------------------------------------------------------------- #
# Hold-first multi-date flow
# --------------------------------------------------------------------------- #


def _slug(text: str) -> str:
    return text.replace(" @ ", "_").replace(" ", "_").replace(",", "")


def _complete_booking(
    page: Page,
    cfg: Config,
    secrets: Secrets,
    exam: Exam,
    order_url: str,
    runs: RunDirs,
    idx: int,
    dry_run: bool,
) -> bool:
    """Return to a previously-held order page and finish form + payment.
    Logs whether the hold survived the navigation away — key telemetry for
    validating the hold-first strategy."""
    page.goto(order_url, wait_until="domcontentloaded", timeout=10_000)
    still_held = page.locator(SELECTORS.hold_countdown).count() > 0
    runs.log(f"[complete {idx}] {exam.label}: hold still active on return = {still_held}")
    if not still_held:
        begin = page.locator(SELECTORS.begin_registration_button)
        if begin.count() > 0:
            runs.log(f"[complete {idx}] hold lapsed — re-clicking Begin Registration")
            try:
                begin.first.click()
                page.wait_for_timeout(800)
            except Exception as exc:  # noqa: BLE001
                runs.log(f"[complete {idx}] re-grab failed: {exc}")
                return False
    runs.snap(page, f"c{idx}a-return-{_slug(exam.label)}")

    if not _fill_form(page, cfg, runs, abort_before_payment=dry_run):
        return False
    if dry_run:
        runs.log(f"[complete {idx}] dry_run — stopping before payment for {exam.label}")
        return True
    return _fill_payment(page, cfg, secrets, runs, dry_run=False)


def _strike_hold_first(
    cfg: Config,
    secrets: Secrets,
    site: TargetSite,
    slots: list[Slot],
    dry_run: bool,
) -> tuple[list[str], list[str]]:
    """Multi-date strike: secure EVERY hold first (seconds apart), then loop
    back and complete forms + payments inside the 10-minute hold windows.
    One browser, one tab, sequential navigation throughout."""
    runs = RunDirs.create(f"multi-hold{'-dry' if dry_run else ''}")
    urls = _site_urls(cfg, site)
    booked: list[str] = []
    failed: list[str] = []
    runs.log(
        "hold-first strike: "
        f"{[[c.label for c in s.candidates] for s in slots]} (dry_run={dry_run})"
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=cfg.strike.headless)
        context: BrowserContext = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-CA",
            timezone_id="America/Vancouver",
        )
        page = context.new_page()
        all_done = False
        try:
            if not _login(page, urls, secrets, runs):
                runs.log("login indicator missing — proceeding anyway")

            page.goto(urls["tcf"], wait_until="domcontentloaded", timeout=10_000)
            runs.snap(page, "00-tcf-page")

            # Phase A — lock every seat. Each hold is ~4s; all targets are
            # secured within seconds of the window opening.
            secured: list[tuple[Slot, Exam, str]] = []
            for i, slot in enumerate(slots, start=1):
                got = _grab_hold(page, slot.candidates, urls, cfg, runs, f"h{i}")
                if got:
                    secured.append((slot, *got))
                else:
                    failed.append(slot.target)

            # Phase B — complete in the order holds were taken (earliest
            # hold expires first).
            for i, (slot, exam, url) in enumerate(secured, start=1):
                if _complete_booking(page, cfg, secrets, exam, url, runs, i, dry_run):
                    booked.append(exam.label)
                    if not dry_run:
                        record_booking(slot.target, exam)
                    runs.log(f"[complete {i}] ✓ {exam.label}")
                else:
                    failed.append(exam.label)
                    runs.log(f"[complete {i}] ✗ {exam.label}")

            all_done = bool(booked) and not failed
        except Exception as exc:  # noqa: BLE001
            runs.log(f"hold-first strike crashed: {exc}")
            runs.snap(page, "99-crash")
        finally:
            if not all_done and not dry_run:
                _hold_open_for_manual_takeover(page, cfg, runs)
            try:
                context.close()
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    return booked, failed


# --------------------------------------------------------------------------- #
# Top-level flows
# --------------------------------------------------------------------------- #


def _attempt(
    pw: Playwright,
    cfg: Config,
    secrets: Secrets,
    site: TargetSite,
    target: Exam | Slot,
    abort_before_payment: bool,
    label: str,
) -> Exam | None:
    """One browser session for one slot. Returns the exam that was booked
    (or, with abort_before_payment, whose form was filled), else None."""
    candidates = target.candidates if isinstance(target, Slot) else [target]
    runs = RunDirs.create(label)
    urls = _site_urls(cfg, site)
    runs.log(f"attempt {label}: candidates={[c.label for c in candidates]} site={site.value}")

    browser = pw.chromium.launch(headless=cfg.strike.headless)
    context: BrowserContext = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-CA",
        timezone_id="America/Vancouver",
    )
    page = context.new_page()
    success = False
    held = False
    try:
        try:
            logged_in = _login(page, urls, secrets, runs)
            if not logged_in:
                runs.log("login indicator missing — proceeding anyway as guest")

            runs.log(f"navigating to TCF page: {urls['tcf']}")
            page.goto(urls["tcf"], wait_until="domcontentloaded", timeout=10_000)
            runs.snap(page, "03-tcf-page")

            got = _grab_hold(page, candidates, urls, cfg, runs, "a")
            if not got:
                runs.snap(page, "04-race-expired")
                return None
            held = True
            exam, _url = got
            runs.snap(page, "06-form-opened")

            if not _fill_form(page, cfg, runs, abort_before_payment=abort_before_payment):
                return None
            if abort_before_payment:
                success = True
                return exam

            success = _fill_payment(page, cfg, secrets, runs, dry_run=False)
            return exam if success else None
        except Exception as exc:  # noqa: BLE001
            runs.log(f"strike crashed: {exc}")
            runs.snap(page, "99-crash")
            return None
    finally:
        # Only hold the browser open when there's a seat to take over — a
        # race that never reached an order page has nothing to rescue, and
        # waiting would delay the next fallback attempt.
        if held and not success and not abort_before_payment:
            _hold_open_for_manual_takeover(page, cfg, runs)
        try:
            context.close()
            browser.close()
        except Exception:  # noqa: BLE001
            pass


def strike_one(exam_id: int, dry_run: bool = False) -> None:
    """Strike a single known exam_id on AF Canada."""
    cfg = load_config()
    secrets = load_secrets()
    site = TargetSite.AF_CANADA
    # Synthesise a minimal exam — the race matches its row by exam_id.
    target = Exam(
        date=f"exam_id={exam_id}",
        exam_id=exam_id,
        opens_at=None,
        closes_at=None,
        price="unknown",
        status="unknown",
        source_url="",
        site=site.value,
    )
    with sync_playwright() as pw:
        _attempt(
            pw,
            cfg,
            secrets,
            site,
            target,
            abort_before_payment=dry_run,
            label=f"strike-{exam_id}{'-dry' if dry_run else ''}",
        )


def _notify_summary(event: str, booked: list[str], failed: list[str]) -> None:
    try:
        from .notify import send_outcome

        send_outcome(
            success=bool(booked) and not failed,
            details={
                "event": event,
                "booked": ", ".join(booked) or "(none)",
                "failed": ", ".join(failed) or "(none)",
            },
            screenshot_path=None,
        )
    except Exception:  # noqa: BLE001
        pass
    console.print(
        f"[bold]{event} — booked: {booked or 'none'}; failed: {failed or 'none'}[/bold]"
    )


def strike_earliest(dry_run: bool = False) -> bool:
    """Strike AF Canada exams.

    Default: earliest available, falling back chronologically, stop at first
    success. With `date_strategy.target_dates` set: book EVERY listed date —
    one browser at a time, never in parallel (AF warns that multiple tabs can
    cancel reservations). Earlier successes in state/booked.json are skipped."""
    cfg = load_config()
    secrets = load_secrets()
    site = TargetSite.AF_CANADA
    urls = _site_urls(cfg, site)
    book_all = bool(cfg.date_strategy.target_dates)

    bookings = [] if dry_run else load_bookings()
    if bookings and not book_all:
        console.print(
            f"[yellow]Already booked {[b.get('date') for b in bookings]} — earliest mode "
            f"books a single exam, so not striking again. Delete state/booked.json to "
            f"book another.[/yellow]"
        )
        return False

    # One-shot Scout via httpx — fast.
    from .scout import fetch_html

    html = fetch_html(urls["tcf"])
    exams = parse_exams_from_html(html, source_url=urls["tcf"], site=site.value)
    cutoff = datetime.now() + timedelta(
        seconds=max(0, cfg.strike.prewarm_seconds) + cfg.strike.total_budget_seconds
    )
    slots = _plan_slots(exams, cfg, bookings=bookings, release_cutoff=cutoff)
    if not slots:
        ds = cfg.date_strategy
        bounds = []
        if ds.target_dates:
            bounds.append(f"target_dates={[t for t, _ in ds.target_specs()]}")
        if ds.locations:
            bounds.append(f"locations={ds.locations}")
        if ds.min_exam_date:
            bounds.append(f"min={ds.min_exam_date}")
        if ds.max_exam_date:
            bounds.append(f"max={ds.max_exam_date}")
        bound_str = f" ({', '.join(bounds)})" if bounds else ""
        console.print(f"[red]No bookable exams found in this release{bound_str}.[/red]")
        return False

    # Multi-date: secure every hold before completing any booking, so later
    # dates aren't exposed while earlier ones are being paid for.
    if book_all and len(slots) > 1 and cfg.strike.secure_all_holds_first:
        booked, failed = _strike_hold_first(cfg, secrets, site, slots, dry_run)
        _notify_summary("Multi-date strike summary (hold-first)", booked, failed)
        return bool(booked)

    booked: list[str] = []
    failed: list[str] = []
    with sync_playwright() as pw:
        for i, slot in enumerate(slots):
            options = " | ".join(c.label for c in slot.candidates)
            console.print(f"[cyan]Attempting {options} ({i + 1}/{len(slots)})[/cyan]")
            if i > 0:
                # Brief breather between attempts so OS sockets and Playwright
                # resources fully drain. Postmortem 2026-06-15 saw 30s nav
                # timeouts on fallbacks, likely from resource saturation.
                time.sleep(2)
            won = _attempt(
                pw,
                cfg,
                secrets,
                site,
                slot,
                abort_before_payment=dry_run,
                label=f"earliest-{i}-{_slug(slot.candidates[0].label)}"
                f"{'-dry' if dry_run else ''}",
            )
            if won is not None:
                booked.append(won.label)
                if not dry_run:
                    record_booking(slot.target, won)
                console.print(f"[green]Booked {won.label}![/green]")
                if not book_all:
                    return True
            else:
                failed.append(slot.target)
                console.print(
                    f"[yellow]Failed {slot.target}"
                    f"{', continuing to next target' if book_all else ', trying next…'}[/yellow]"
                )

    if book_all:
        _notify_summary("Multi-date strike summary", booked, failed)
        return bool(booked)
    console.print("[red]All targets exhausted.[/red]")
    return False


def dry_run(site: str = "sandiego", exam_id: int | None = None) -> None:
    """End-to-end test. Stops before final payment."""
    cfg = load_config()
    secrets = load_secrets()
    target_site = TargetSite.AF_SANDIEGO if site == "sandiego" else TargetSite.AF_CANADA
    urls = _site_urls(cfg, target_site)

    from .scout import fetch_html

    html = fetch_html(urls["tcf"])
    exams = parse_exams_from_html(html, source_url=urls["tcf"], site=target_site.value)
    if exam_id is not None:
        target = next((e for e in exams if e.exam_id == exam_id), None)
        if target is None:
            console.print(f"[red]exam_id={exam_id} not found on {site}[/red]")
            return
    else:
        candidates = [e for slot in _plan_slots(exams, cfg) for e in slot.candidates]
        target = next((e for e in candidates if e.exam_id is not None), None)
        if target is None:
            console.print(
                f"[red]No exam with an open registration on {site}; "
                "specify --exam-id explicitly or refresh fixtures.[/red]"
            )
            return

    with sync_playwright() as pw:
        won = _attempt(
            pw,
            cfg,
            secrets,
            target_site,
            target,
            abort_before_payment=True,
            label=f"dry-{site}-{target.exam_id}",
        )
    ok = won is not None
    console.print(f"[{'green' if ok else 'red'}]Dry run {'succeeded' if ok else 'failed'}[/]")


def benchmark() -> None:
    """Measure page-load and DOM-poll latency from this network."""
    cfg = load_config()
    site = TargetSite.AF_CANADA
    urls = _site_urls(cfg, site)
    samples: list[float] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        for i in range(5):
            t0 = time.time()
            page.goto(urls["tcf"], wait_until="domcontentloaded")
            elapsed = time.time() - t0
            samples.append(elapsed)
            console.print(f"  iter {i + 1}: {elapsed * 1000:.0f} ms")
        browser.close()
    avg = sum(samples) / len(samples)
    console.print(f"[green]avg page-load = {avg * 1000:.0f} ms over {len(samples)} runs[/green]")

# Postmortem — TCF Strike, 2026-06-15

## TL;DR

The bot **won the race** (got an order URL within ~500 ms of the window
opening), then **crashed before it could navigate to checkout** because of a
regression I introduced when adding Authorize.Net support: a renamed selector
was left referenced in `_advance_to_payment`. The 10-minute candidate hold
covered for it — the bot kept the browser open for 9 minutes — but manual
takeover didn't land. The Sept 4 and Sept 9 fallbacks then both timed out
on the very first navigation, likely because two parallel browser instances
were saturating local resources.

**Net result**: no slot booked. **But all four root causes are now diagnosable
from the run logs, and the Sept 9 window re-opens 2026-06-17 at 12:00pm — 48 h
from now — giving us time to ship fixes and try again.**

---

## Timeline (all times America/Vancouver, 2026-06-15)

| Time | What happened |
|---|---|
| 11:57:03 | **Two** Strike instances fire in parallel (BUG 2) |
| 11:57:09 | Both fail login (5 s timeout) — generic selectors didn't match (BUG 3) |
| 11:57:09 | Both fall through to guest mode |
| 11:57:12 | Both start race loop on AF Canada TCF page |
| **12:00:00.547** | **Race won.** Order URL discovered: `exam_id=53` (Sept 2) |
| 12:00:01–05 | Begin Registration clicked, form opens, basic fields fill |
| 12:00:05 | All 5 autocomplete dropdowns fail to focus (BUG 4) |
| 12:00:06 | **CRASH**: `'AFSelectors' object has no attribute 'generic_card_number'` (BUG 1) |
| 12:00:06 | Browser stays open per `keep_open_on_failure_seconds: 540` |
| 12:09:09 | Manual-takeover window expires; Sept 4 fallback fires |
| 12:09:39 | Sept 4 strike times out 30 s on first `page.goto` (BUG 5) |
| 12:19:12 | Sept 9 fallback fires, same 30 s timeout |
| 12:29:15 | Final takeover window expires. All over. |

---

## What worked (don't lose sight of this)

- **Scout was accurate**: parsed the opens_at correctly, scheduler fired
  Strike at T-90 s as planned.
- **The race loop is fast**: order URL was discovered within ~500 ms of
  window-open. Whatever delay manual users had, we beat them.
- **The 10-minute hold mechanism is real and works** — even after the crash,
  AF kept the slot reserved for the candidate session.
- **`_dump_login_page` paid off**: `state/runs/.../login_page_inputs.json`
  shows the real login form structure, so we can ship a fix today.
- **The crash didn't lose the browser** — `_hold_open_for_manual_takeover`
  kept it open for the full 9 min. That's the safety net working.

---

## Root causes (ranked by impact)

### BUG 1 — Removed selector still referenced (CATASTROPHIC)

When I refactored to add Stripe + Authorize.Net dispatch, I deleted the
`generic_card_number` selector but **left `_advance_to_payment` referencing
it** as a heuristic for "are we already on a card form?"

```python
# booker.py _advance_to_payment
if (
    page.locator(f"iframe[title*={SELECTORS.stripe_card_iframe_title!r}]").count() > 0
    or page.locator(SELECTORS.generic_card_number).count() > 0   # ← AttributeError
):
    return
```

Exception was unhandled inside `_advance_to_payment`, propagated up,
crashed the strike right at the moment we had the slot.

**Fix**: use `SELECTORS.authnet_card_number` for the Authorize.Net detection
(which is what the original heuristic was meant to match).

### BUG 2 — Double execution (PARALLEL STRIKES)

Every log line in the Sept 2 attempt is duplicated 30–50 ms apart, including
the `attempt earliest-0` banner. Two `Strike` jobs fired at the same instant.

Most likely cause: **`tcf schedule` was started more than once** (e.g. a stale
tmux session plus a new one, or relaunch after the laptop woke). The
scheduler has no PID file / lock, so each instance independently scheduled
the same strike for the same opens_at, and APScheduler in each fired
independently.

**Effect**: two browser windows, two parallel HTTP races, both crashing the
same way, likely saturating the local network/CPU enough to cause the
Sept 4/Sept 9 fallback navigations to time out at 30 s (BUG 5).

**Fix**: PID-file lock in `tcf schedule` — refuse to start if another
scheduler is already running.

### BUG 3 — Login selectors didn't match AF Canada

`login_page_inputs.json` from the failed run shows the real names:

```json
[
  { "name": "contact_email",    "id": "contact_email",    "placeholder": "E-mail" },
  { "name": "contact_password", "id": "contact_password", "placeholder": "Password" },
  { "name": "s8-formssubmitbutton1", "text": "Login" }
]
```

We had patterns for `community_login_email` and a bunch of generic
fallbacks, but `contact_email` / `contact_password` weren't in the list.
Login timed out at 5 s, we proceeded as guest. Guest mode required a
`registrant_password`, which we didn't fill.

**Fix**: trivial — add `contact_email` and `contact_password` to
`selectors.py`.

### BUG 4 — Autocomplete focus heuristic fails on AF Canada

5/5 autocompletes failed with `visible search input not found`.

```python
const root = hidden.closest('div,p,fieldset,form') || document.body;
const candidates = root.querySelectorAll('input[type=search], input[type=text]');
```

On SD this worked. On AF Canada the visible search input is apparently
either further away in the DOM (not in the same closest container), or
uses a different `type` than `search` / `text`, or the hidden input has a
different ancestor structure entirely. Without re-inspecting the live form
we can only guess.

**Fix**: widen the search — walk further up the DOM, accept more input
types, or click by adjacent label. Best fix: inspect the live Canada form
in DevTools and update the heuristic with real data.

### BUG 5 — Cascading 30 s timeouts on fallback strikes

After the crash + browser-open-hold, the Sept 4 fallback `page.goto` on
`/community/login/` timed out at 30 s. So did Sept 9 nine minutes later.

Possible causes (most likely first):
- **Resource saturation**: 2× crashed-but-open Playwright Chromiums (from
  the double execution) + a fresh one = 3 Chromium processes fighting for
  CPU/network. Each adds memory + persistent connections.
- **AF rate-limiting** the residential IP after repeated rapid requests.
- **macOS App Nap or other power throttling** kicking in after 9 minutes
  of "idle" browser.

**Fix**: ensure `_hold_open_for_manual_takeover` closes any prior browser
before the next fallback. Add a stricter 10 s navigation timeout on
fallbacks (so we fail fast and try the next exam).

### BUG 6 — Manual takeover didn't land

The browser was open with a partially filled form for 9 minutes. Telegram
should have alerted (assuming it was configured). The slot was held. But
no booking happened.

Hard to know exactly why without asking — Telegram delivery, two confusing
browser windows, didn't know what to fill, etc. **The takeover UX is
incomplete**: we send a Telegram with the URL and a generic instruction,
but we don't say *what's missing* or *what to fill next*.

**Fix**: panic-mode Telegram with explicit instructions — "I have a slot
held under candidate session at <URL>. Please open the Chromium window and:
(a) log in if asked, (b) fill the highlighted fields, (c) click Process.
You have N minutes."

---

## What to fix, in order

### P0 — Must ship before 2026-06-17 12:00pm (Sept 9 window)

1. **BUG 1**: Replace `SELECTORS.generic_card_number` reference in
   `_advance_to_payment` with the Stripe + Authorize.Net detection that
   actually exists.
2. **BUG 3**: Add `contact_email` / `contact_password` to login selectors.
3. **BUG 4**: Inspect AF Canada autocomplete DOM (real form, DevTools)
   and rewrite `_focus_autocomplete_search`. Until inspected, widen the
   sibling search.

### P1 — Should ship before next window

4. **BUG 2**: PID-file lock in `tcf schedule` so we can't double-fire.
5. **BUG 5**: Drop nav timeout from 30 s → 10 s for fallback strikes;
   ensure previous browser is closed before the next attempt starts.
6. **BUG 6**: Richer takeover Telegram message with explicit per-field
   instructions and a direct link to the order URL.

### P2 — Nice-to-have

7. Scout-state staleness warning when `opens_at` is past for a "future"
   exam — caught us with stale exam status.
8. Sentry/structured-logging so we don't have to read 200-line text logs
   to triage. JSON-per-line would help.
9. A `tcf preflight` command that verifies login works, fields can be
   focused, etc., without actually grabbing a slot.

---

## Recovery plan for 2026-06-17 12:00pm (Sept 9 window)

The Sept 9 window opens in **48 hours**. Plan:

- **Today**: ship the three P0 fixes. Test against SD dry-run again. Then
  drive the AF Canada login + form (with something cheap in cart) via
  DevTools to verify the autocomplete DOM and update selectors.
- **Tomorrow**: full dry-run against AF SD with the real config. Confirm
  Telegram delivery, manual takeover UX, and that the bot DOES complete
  end-to-end with the abort-before-payment flag.
- **Wednesday 11:30am**: kill any stray `tcf schedule` processes
  (`pgrep -af "tcf schedule"`). Start a single fresh one in tmux.
  Confirm via `tcf status` that exactly one Strike is scheduled for 11:58:30.

If Sept 9 also fails, AF Canada typically releases more dates in the
following weeks. The bot will continue scouting.

---

## Accountability

I introduced BUG 1 in the refactor to support AF Canada's Authorize.Net
checkout and didn't run `tcf dry-run --site sandiego` after the change.
The test suite passed only because parser tests don't exercise the booker
code path. **The single most important preventive measure going forward**:
a fast `tcf preflight` command (P2) that exercises every code path against
SD without actually submitting, runnable in under 30 seconds. That's the
next thing to add.

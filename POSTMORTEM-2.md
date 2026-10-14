# Postmortem 2 — TCF Strike, 2026-06-17

## TL;DR

Bot ran today but failed for **four** new reasons:

1. **Wrong target picked.** Bot raced for Sept 2 (already-open window) instead of the Sept 9 release. With `earliest_with_fallback` and no `min_exam_date` set, that's the documented behavior — but operationally wrong for this strike.
2. **Double execution again.** Two scheduler instances ran in parallel (1 ms apart). The PID-file lock from Postmortem 1 only protects within one repo install — the user has two repo clones and started a scheduler in each.
3. **Autocomplete dropdowns still failed.** This time we *captured the DOM*: AF Canada uses a `<oncord-combobox>` Web Component with a readonly visible input and a hidden popover containing the real searchable input. My BUG 4 defensive heuristic focused the wrong element.
4. **Missing form submit step.** After filling the registration form, the bot tried to detect a payment form on the *same page* — but AF Canada's flow is register → submit → /commerce/order/final/. We never advanced because we never clicked the registration form's submit button.

Plus an incidental field-name mismatch (BUG 10): real form field is `contact_passeport_` (French spelling + trailing underscore), not `contact_passport_number_or_id_number`. Silently skipped.

**Net result**: same as 2026-06-15 — slot was held briefly, bot couldn't finish, user wasn't available for manual takeover.

---

## What we now know — and didn't last time

The `_dump_form_dom` helper from Postmortem 1 paid off. `state/runs/.../form.html` and `autocomplete_dom.json` give us:

- Exact custom-element tag: `<oncord-combobox>` (plus four others: `oncord-ajaxregion`, `oncord-datepicker`, `oncord-datepicker-agewarning`, `oncord-effect-attach`).
- Full DOM tree around each autocomplete.
- The widget structure: a custom element with three nested layers — a *display* input (`#search`, readonly), a popover (`<oncord-effect-attach usepopover="true" show="false">`), and the *real* searchable input (`#mobile-search-input`).
- Options are AJAX-loaded; they're not in the static HTML.

The `aria-label` attribute on each `<oncord-combobox>` carries the human-readable field name ("Mother Tongue", "Nationality", "Country of Birth", etc.) — useful for matching even when ID schemes shift.

---

## Timeline (2026-06-17, America/Vancouver)

| Time | What |
|---|---|
| 14:57:03 | Two scheduler instances fire the strike simultaneously (BUG 9). |
| 14:57:04 | Both navigate to /community/login/. |
| 14:57:10 | Both **login successfully** (the BUG 3 fix is solid — verified via dashboard). |
| 14:57:12 | Both start the race loop targeting **Sept 2** (BUG 11: wrong-target selection). |
| 15:00:00 | Race wins — order URL `exam_id=53` discovered. |
| 15:00:01 | Click Begin Registration → form opens. |
| 15:00:02 | `_dump_form_dom` saves `form.html` + `autocomplete_dom.json`. |
| 15:00:02–05 | Form fill: 4 plain text + 2 consent boxes fill OK; **all 5 autocompletes fail** (BUG 7). |
| 15:00:03 | `_advance_to_payment` reports "no card form and no Proceed button" — **never advances to /commerce/order/final/** (BUG 8). |
| 15:00:03 | `payment processor: UNKNOWN` → manual-takeover Telegram alert fired. |
| 15:09 / 15:30 | Manual-takeover windows expire (user wasn't available). |

---

## Bugs found this run

### BUG 7 — Autocomplete must drive `<oncord-combobox>`, not generic search inputs

Web Component structure (excerpt):

```html
<oncord-combobox name="contact_gael_language_id" aria-label="Mother Tongue">
  <input type="hidden" id="custom_field_86_languages" name="contact_gael_language_id" value="">
  <input id="search" type="text" readonly>                             <!-- display only -->
  <oncord-effect-attach usepopover="true" show="false">
    <input id="mobile-search-input" type="search" placeholder="Search...">  <!-- the real input -->
    <!-- options AJAX-loaded once popover opens -->
  </oncord-effect-attach>
</oncord-combobox>
```

**Failure mode**: my defensive heuristic's Strategy 1 (`closest container → text-or-search input`) focused the `readonly` `#search`. The `_try_one_autocomplete_value` then typed but produced 0 visible options, because the popover was still closed (`show="false"`).

**Fix shape**: locate `oncord-combobox[name='X']`, click it (or its `#search-toggle`) to open the popover, type into `#mobile-search-input`, wait for AJAX, click matching option.

### BUG 8 — Card section conditionally rendered behind a wrong-valued radio

**Initially diagnosed as a missing submit step / two-page flow — that was wrong.** A look at the rendered form (screenshot from the saved `form.html`) confirms AF Canada's order page is **single-page**: registration fields + payment method + card inputs + Process button all live on `/af/exam-selector/order/?exam_id=N`. No navigation to `/commerce/order/` or `/commerce/order/final/`.

**Actual root cause**: `_fill_known_fields` tries to tick the "Credit Card" radio via `SELECTORS.payment_method_card_radio = "input[name='payment_method_id'][value='2']"`. That value is AF San Diego's (Stripe Card). AF Canada uses `value='1'` for Credit Card. So the radio click silently no-op'd (selector matched nothing), the card section never rendered (it's gated by which payment method is selected), and `_advance_to_payment` correctly reported "no card form".

Earlier-than-expected check timing was a contributing factor: `_advance_to_payment` ran ~700 ms after Begin Registration, before any potential late-render of the card section. We were not retrying.

**Fix shipped**:
1. `payment_method_card_radio` now matches `[value='1'], [value='2']` so it works on both sites.
2. `_advance_to_payment` now retries 3× with 500 ms backoff before giving up — handles any rendering lag after the card radio is ticked.

Note: the previous `_advance_to_payment` had a Proceed-button hop for an imagined `/commerce/order/` → `/commerce/order/final/` flow. It's harmless (just doesn't match anything on the single-page form) and worth keeping as a fallback in case AF reorganises later.

### BUG 9 — PID lock doesn't catch two repo clones

Lock file path is `state/scheduler.pid` (relative to repo root). User has two clones (`tcf_scheduler_bot` and `tcf_scheduler_bot-1`), each with its own state dir. Each scheduler instance acquires its own lock.

**Fix shape**: move lock to a system-wide location (`~/.tcf-scheduler.lock`) so all installs share it. Use `os.open(...O_CREAT|O_EXCL...)` for atomic creation (closes the TOCTOU race even within a single install).

### BUG 10 — Passport selector mismatch

Real form field: `name="contact_passeport_"` (French "passeport", trailing underscore — probably AF Canada's bilingual platform reusing a French-named field even on the English form).

My selector: `input[name='contact_passport_number_or_id_number']` (AF San Diego's name, which doesn't exist on Canada).

Result: `_try_fill` silently no-ops (count() == 0 → return False without ✓/✗ log). Passport field never filled. May be a required field for the form to submit successfully.

**Fix shape**: change selector to `input[name='contact_passeport_'], input[name='contact_passport_number_or_id_number']` to support both sites.

### BUG 11 — Wrong target picked (config-only, not a code bug)

`earliest_with_fallback` correctly picked Sept 2 (the earliest *currently open* date). But the user wanted Sept 9. The fix exists in code already from yesterday: `min_exam_date: "2026-09-09"` in `config.yaml`.

Calling it out as a bug here because the runbook didn't surface the need clearly enough — when there are multiple open windows simultaneously, the user must explicitly tell the bot which dates to consider.

---

## Recovery for next strike

Sept 9 likely sold out today (12:00pm release, almost certainly gone by now). AF Canada usually releases more dates in following weeks. Plan:

1. **Today**: ship BUG 7, 8, 9, 10 fixes. Verify autocomplete works against the new oncord-combobox handler via a dry-run.
2. **Update config.yaml**: set `min_exam_date: "2026-XX-XX"` to whichever new release the user wants to target (won't be known until AF announces).
3. **Single scheduler**: only run `tcf schedule` in ONE clone. Delete the duplicate.
4. **User availability for manual takeover**: even with all fixes, the success-rate estimate from yesterday remains 65–75% with manual rescue, ~30% fully automated. User should be at their phone / laptop during the release moment.

---

## Additional observations from the rendered form

Things visible in the saved `form.html` (and the screenshot) that we should
account for in future iterations:

- **Autocomplete options are in French even on the English-language site.**
  Confirmed by the Mother Tongue hint text: *"For English, search 'anglais'"*.
  So `mother_tongue: English` won't match anything — config needs French
  primary, e.g. `mother_tongue: [Anglais, English]` or just `Anglais`.
  Same applies to nationality/country_of_birth — bilingual lists are not
  paranoia, they're required.

- **"Who is this exam registration for?" radio group.** Three options:
  Myself / Another Family Member / Relative or Employee. Pre-selected as
  "Myself" for logged-in users (visible in screenshot — red highlight on
  Myself with name underneath). If AF ever changes the default we'd miss
  this; worth adding to selectors as a defensive check.

- **Available Credit.** Accounts with credit on file see a pre-checked
  "Use Available Credit" checkbox that reduces the amount charged to the
  card. Worth not unchecking it accidentally. Current code doesn't touch it.

- **Add-on dropdown** (Exam only / probably "Exam + PREPmyFuture") —
  pre-selected to "Exam only". Current code doesn't touch it, which is
  the right default.

## What I'm reflecting on

Postmortem 1 said: "every preflight check we add should map to a real past failure, otherwise we're just adding noise." This run produced four new bugs none of which preflight could have caught — because preflight tests only `_login` and the module-import / selector-existence layers. Form-fill code paths require a real candidate hold to exercise.

Best next iteration: a "shadow dry-run" mode that hits a sister-platform form (AF SD's are public, doesn't burn a hold) and exercises the *entire* form-fill stack including `<oncord-combobox>` (if SD uses it). Add to preflight when the user has bandwidth.

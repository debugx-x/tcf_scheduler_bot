"""CSS / XPath selectors for the AF site, kept in one place.

Confirmed against the live AF San Diego order page (Oncord platform). Both AF
Canada and AF San Diego share the same form structure, so these selectors apply
to both. Keep selector logic out of the rest of the codebase.

Form flow surprise (confirmed in walkthrough):
- The order page is SINGLE-PAGE. Candidate info, TCF identity, two consent
  checkboxes, and payment all live on the same page. The submit button (`#stripeSubmit`,
  text "PROCESS") commits everything at once. There is no separate "continue
  to payment" step.
- Card number / expiry / CVC are inside three separate Stripe iframes
  (titles "Secure card number input frame", "Secure expiration date input
  frame", "Secure CVC input frame"). Use Playwright frame_locator().
- Stripe loads an invisible hCaptcha — may silently pass or may challenge.
- Dropdowns for Mother Tongue / Nationality / Country / Disability are
  autocomplete widgets (visible text input + search input + hidden ID).
  Filling them means: focus the text input, type a search string, click the
  matching option in the popup.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AFSelectors:
    # --- Public TCF page (the listing) ---
    exam_table_row: str = "tr.tableRow"
    exam_title: str = ".es-exam-title"
    exam_status: str = ".es-status"
    exam_book_link: str = "a[href*='/af/exam-selector/order/']"

    # --- Login page ---
    # Confirmed from 2026-06-15 strike's login_page_inputs.json:
    # AF Canada uses contact_email / contact_password (NOT community_login_*).
    # AF San Diego may differ; kept generic fallbacks at the end of each list.
    login_path: str = "/community/login/"
    login_username_input: str = (
        "input[name='contact_email'], "                # AF Canada (confirmed)
        "input[name='community_login_email'], "        # legacy / possible SD
        "input[name='login_email'], input[name='username'], "
        "input[name='email'], input[type='email']:visible"
    )
    login_password_input: str = (
        "input[name='contact_password'], "             # AF Canada (confirmed)
        "input[name='community_login_password'], "
        "input[name='login_password'], "
        "input[name='password'], input[type='password']:visible"
    )
    login_submit_button: str = (
        "#s8-formssubmitbutton1, "                     # AF Canada (confirmed)
        "button[type='submit']:has-text('Login'), "
        "button[type='submit']:has-text('Log in'), "
        "button[type='submit'], input[type='submit']"
    )
    login_success_indicator: str = "a[href*='/community/logout'], [class*='logged-in']"

    # Authenticated-only page. Hitting this after login is the most reliable
    # session-verification signal: the URL stays the same regardless of auth
    # state, but the page CONTENT differs — logged-in users see the dashboard,
    # logged-out users see an embedded login form whose password field is named
    # `login_contact_password` (note the `login_` prefix — different from the
    # main /community/login/ page's `contact_password`).
    auth_check_path: str = "/af/students/dashboard/"
    auth_check_login_prompt: str = "input[name='login_contact_password']"

    # --- Order / registration form ---
    order_path_prefix: str = "/af/exam-selector/order/"
    cart_path: str = "/commerce/order/"
    cart_final_path: str = "/commerce/order/final/"
    begin_registration_button: str = "button:has-text('Begin Registration')"
    cart_proceed_button: str = "button:has-text('Proceed'), a:has-text('Proceed')"
    hold_countdown: str = "text=Your spot is held for"

    # Candidate-info inputs (exact `name=` attrs from live form).
    field_first_name: str = "input[name='registrant_first_name']"
    field_last_name: str = "input[name='registrant_last_name']"
    field_email: str = "input[name='registrant_email']"
    field_password: str = "input[name='registrant_password']"
    field_gender_male: str = "input[name='contact_gender'][value='m']"
    field_gender_female: str = "input[name='contact_gender'][value='f']"
    field_phone: str = "input[name='registrant_phone']"
    field_dob: str = "input[name='registrant_dob']"  # format: "MMM DD YYYY", e.g. "Jun 15 2026"
    field_address: str = "textarea[name='registrant_address_address']"
    field_city: str = "input[name='registrant_address_suburb_city']"
    field_state: str = "input[name='registrant_address_state']"
    field_postcode: str = "input[name='registrant_address_postcode']"
    field_country_hidden: str = "input[name='registrant_address_country']"  # value is ISO3, e.g. "CAN"
    field_country_visible: str = "input[placeholder='Country']"

    # TCF identity
    field_acknowledge_french_law: str = (
        "input[name='contact_acknowledgment_of_french_legal_requirements[]'][value='yes']"
    )
    # AF Canada's bilingual form uses the French spelling with a trailing
    # underscore: `contact_passeport_`. AF San Diego uses the longer English name.
    # Support both so a single config works on both sites.
    field_passport: str = (
        "input[name='contact_passeport_'], "                  # AF Canada (confirmed)
        "input[name='contact_passport_number_or_id_number']"  # AF San Diego
    )
    field_candidate_number: str = "input[name='contact_gael_candidate_number']"
    field_city_of_birth: str = "input[name='contact_city_of_birth']"

    # Autocomplete widgets — visible input + hidden ID pairs.
    # For these, click the visible input, type, then click the matching option.
    field_mother_tongue_hidden: str = "input[name='contact_gael_language_id']"
    field_nationality_hidden: str = "input[name='contact_gael_nationality_id']"
    field_country_of_birth_hidden: str = "input[name='contact_gael_country_of_birth_code']"
    field_disability_hidden: str = "input[name='contact_has_disability']"
    # Autocomplete option popup (after typing into a search input).
    autocomplete_option: str = "li[role='option'], .autocomplete-suggestion, [class*='option']:visible"

    # Marketing / misc
    field_how_heard_prefix: str = "input[name='contact_how_did_you_hear_about_us[]']"

    # "Who is this exam registration for?" — pre-selected to Myself for
    # logged-in users, but defensive re-tick so we never silently book for
    # the wrong person if AF changes the default. AJAX-triggered onclick;
    # the form re-renders on change.
    field_buy_for_myself: str = "input#buy_for_myself, input[name='buy_for'][value='myself' i]"

    # Consent + payment
    # Exclude *_hidden siblings — `name^='agree_terms_product_'` would also
    # match `agree_terms_product_6_hidden[]`, the wrong element.
    field_agree_terms: str = "input[type='checkbox'][name^='agree_terms_product_']"
    # AF Canada uses value='1' for Card (Authorize.Net); AF San Diego uses
    # value='2' for Card (Stripe). Match either. We pick `.first` so the bot
    # ticks one even if both are somehow present.
    payment_method_card_radio: str = (
        "input[name='payment_method_id'][value='1'], "
        "input[name='payment_method_id'][value='2']"
    )

    # --- AF San Diego: Stripe Elements (iframe per card field) ---
    stripe_card_iframe_title: str = "Secure card number input frame"
    stripe_expiry_iframe_title: str = "Secure expiration date input frame"
    stripe_cvc_iframe_title: str = "Secure CVC input frame"
    stripe_card_input: str = "input[name='cardnumber'], input[autocomplete='cc-number']"
    stripe_expiry_input: str = "input[name='exp-date'], input[autocomplete='cc-exp']"
    stripe_cvc_input: str = "input[name='cvc'], input[autocomplete='cc-csc']"

    # --- AF Canada: Authorize.Net direct card entry on /commerce/order/final/ ---
    # (Confirmed via live DOM inspection. Card fields are plain inputs on the
    # parent page; expiry is split into separate month and year inputs.)
    authnet_card_number: str = "input[name='creditcard_number']"
    authnet_card_holder: str = "input[name='creditcard_holder']"
    authnet_expiry_month: str = "input[name='creditcard_expiry_month']"
    authnet_expiry_year: str = "input[name='creditcard_expiry_year']"
    authnet_cvv: str = "input[name='creditcard_cvv']"

    # Common between SD (Stripe) and CA (Authorize.Net) — both use the
    # same `creditcard_holder` name.
    field_card_holder: str = "input[name='creditcard_holder']"

    # Submit buttons — SD uses #stripeSubmit, Canada uses #authorizeSubmit.
    submit_process_button: str = "#stripeSubmit, #authorizeSubmit"
    submit_complete_order_button: str = (
        "button:has-text('Complete Order'), button:has-text('Pay'), "
        "button:has-text('Place Order'), button:has-text('Confirm'), "
        "button:has-text('Process'), button[type='submit']"
    )

    # --- reCAPTCHA detection (Canada checkout has invisible reCAPTCHA) ---
    recaptcha_iframe: str = "iframe[src*='google.com/recaptcha'], iframe[src*='recaptcha']"
    recaptcha_challenge: str = "iframe[title*='recaptcha challenge' i]"

    # --- 3-D Secure / hCaptcha detection ---
    three_ds_iframe: str = "iframe[src*='3dsecure'], iframe[src*='acs']"
    hcaptcha_challenge: str = "iframe[src*='hcaptcha'][title*='challenge']"

    # --- Confirmation ---
    confirmation_indicator: str = (
        "[class*='confirmation'], [class*='success'], h1:has-text('Thank'), h1:has-text('Confirmation')"
    )


SELECTORS = AFSelectors()

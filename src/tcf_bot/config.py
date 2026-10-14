"""Config + secrets loading. Pydantic validates config.yaml; .env feeds secrets."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, EmailStr, Field, SecretStr, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config.yaml"
ENV_PATH = REPO_ROOT / ".env"


class DateStrategyMode(str, Enum):
    EARLIEST_WITH_FALLBACK = "earliest_with_fallback"
    SPECIFIC_DATE = "specific_date"
    ORDERED_LIST = "ordered_list"


class TargetSite(str, Enum):
    AF_CANADA = "af_canada"
    AF_SANDIEGO = "af_sandiego"


class Candidate(BaseModel):
    # Personal
    first_name: str
    last_name: str
    email: EmailStr
    phone: str
    date_of_birth: str
    gender: str = ""  # "" / "m" / "f"

    # TCF-specific identity. Autocomplete-driven fields accept EITHER a single
    # string OR a list of fallback strings — the bot tries each in order until
    # one resolves. Useful when the bilingual AF site shows French labels
    # ("Inde" instead of "India").
    passport_number: str = ""
    id_number: str = ""
    candidate_number: str = ""
    mother_tongue: str | list[str] = ""
    nationality: str | list[str]
    country_of_birth: str | list[str] = ""
    city_of_birth: str = ""

    # Address
    address_line1: str
    address_line2: str = ""
    city: str
    province: str = ""
    postal_code: str
    country: str | list[str]

    # Form-specific preferences (defaults match the AF San Diego flow)
    has_disability: str | list[str] = "No"
    how_heard: list[str] = []
    registration_reason: str | list[str] = "School/studies in the country of origin"

    # Required consent boxes — must both be True or the form refuses to submit
    acknowledge_french_law: bool = True
    agree_to_terms: bool = True

    # Discovered fields appended at runtime are accepted as extras.
    model_config = {"extra": "allow"}


class TargetDate(BaseModel):
    """A target date with its own location preference, e.g.
    `{date: "December 2, 2026", locations: [new_westminster]}`."""

    date: str
    # Empty = use DateStrategy.locations.
    locations: list[str] = Field(default_factory=list)


class DateStrategy(BaseModel):
    mode: DateStrategyMode = DateStrategyMode.EARLIEST_WITH_FALLBACK
    exam_center: str = "Vancouver"  # deprecated and ignored — use `locations`
    specific_date: str | None = None
    ordered_list: list[str] = Field(default_factory=list)
    # Exam sites you're willing to sit at, most preferred first:
    # "vancouver" (6161 Cambie St.) and/or "new_westminster" (320 Columbia St.).
    # Empty = either site. When the same date is offered at several allowed
    # sites, the first listed wins, and the next is the fallback if the first
    # is sold out.
    locations: list[str] = Field(default_factory=list)
    # Book EVERY date in this list (not just the first that succeeds).
    # Accepts "October 2, 2026" or "2026-10-02", or a mapping with its own
    # `locations` to override the global preference for that date. When
    # non-empty this overrides `mode` — each date is attempted sequentially in
    # one session. Mind the 20-day retake rule between TCF sittings.
    target_dates: list[str | TargetDate] = Field(default_factory=list)
    # If an exact target date isn't available, substitute the NEAREST available
    # date within this many days of the target — but only if it still respects
    # `min_gap_days` from every other date already secured this run. Set to 0
    # to disable substitution (exact dates only).
    substitute_tolerance_days: int = 14
    # Minimum spacing enforced between two SUBSTITUTED bookings (the TCF retake
    # rule is 20 days). Explicitly-listed exact dates are always honored even if
    # they'd violate this — it only governs auto-substitutions.
    min_gap_days: int = 20

    @model_validator(mode="after")
    def _reject_unimplemented_modes(self) -> "DateStrategy":
        # These modes exist in the schema but nothing implements them; left
        # unchecked they'd fall through to "book any open exam".
        if self.mode in (DateStrategyMode.SPECIFIC_DATE, DateStrategyMode.ORDERED_LIST):
            raise ValueError(
                f"date_strategy.mode={self.mode.value!r} is not implemented. Use "
                f"target_dates (books every listed date) or earliest_with_fallback "
                f"with min_exam_date/max_exam_date."
            )
        return self

    def target_specs(self) -> list[tuple[str, list[str]]]:
        """Each target date paired with its effective location preference."""
        out: list[tuple[str, list[str]]] = []
        for t in self.target_dates:
            if isinstance(t, TargetDate):
                out.append((t.date, t.locations or self.locations))
            else:
                out.append((t, self.locations))
        return out
    # Filter exams by exam date (the date you'd actually sit the test, not the
    # registration window). Both bounds are optional and inclusive. ISO format
    # YYYY-MM-DD. Composes with any mode — e.g. mode=earliest_with_fallback +
    # min_exam_date=2026-09-01 means "earliest exam on/after Sept 1".
    min_exam_date: str = ""
    max_exam_date: str = ""


class Site(BaseModel):
    target: TargetSite = TargetSite.AF_CANADA
    af_canada_base_url: str = "https://www.alliancefrancaise.ca"
    af_canada_tcf_path: str = "/en/language/exams/tcf-canada/"
    af_sandiego_base_url: str = "https://www.afsandiego.org"
    af_sandiego_tcf_path: str = "/certifications/tcf/tcf-canada/"


class Scout(BaseModel):
    poll_interval_minutes: int = 15
    notify_when_opens_within_hours: int = 24


class Strike(BaseModel):
    prewarm_seconds: int = 90
    poll_lead_seconds: int = 2
    poll_interval_ms: int = 100
    three_ds_alert_prefix: str = "3DS verification required — open: "
    total_budget_seconds: int = 600
    headless: bool = True
    # When something fails mid-strike, keep the browser window open for up to
    # this many seconds so you can take over manually. Bounded by the 10-min
    # candidate hold, so 600s is the natural max.
    keep_open_on_failure_seconds: int = 540
    # Multi-date strategy: secure ALL candidate holds first (each Begin
    # Registration click locks a seat for 10 min), THEN go back and complete
    # forms + payment inside the hold windows. Locks every target within
    # seconds instead of exposing later dates while earlier ones are paid for.
    # One tab, sequential navigation — not the "multiple tabs" pattern AF
    # warns against. Set false to fall back to complete-each-before-next.
    secure_all_holds_first: bool = True


class Telegram(BaseModel):
    chat_id: str


class Email(BaseModel):
    to: EmailStr
    from_: str = Field(alias="from")


class Notifications(BaseModel):
    telegram: Telegram
    email: Email


class InteractiveFallback(BaseModel):
    enabled: bool = True
    timeout_seconds: int = 120


class Config(BaseModel):
    candidate: Candidate
    date_strategy: DateStrategy
    site: Site = Field(default_factory=Site)
    scout: Scout = Field(default_factory=Scout)
    strike: Strike = Field(default_factory=Strike)
    notifications: Notifications
    interactive_fallback: InteractiveFallback = Field(default_factory=InteractiveFallback)


class Secrets(BaseModel):
    """Loaded from .env. SecretStr keeps values out of repr/logs."""

    af_username: str
    af_password: SecretStr
    card_number: SecretStr
    card_cvv: SecretStr
    card_expiry: str
    card_holder: str
    telegram_bot_token: SecretStr
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: SecretStr


def load_config(path: Path = CONFIG_PATH) -> Config:
    with path.open() as f:
        data = yaml.safe_load(f)
    return Config.model_validate(data)


def load_secrets(env_path: Path = ENV_PATH) -> Secrets:
    load_dotenv(env_path, override=False)
    return Secrets(
        af_username=os.environ["AF_USERNAME"],
        af_password=os.environ["AF_PASSWORD"],
        card_number=os.environ["CARD_NUMBER"],
        card_cvv=os.environ["CARD_CVV"],
        card_expiry=os.environ["CARD_EXPIRY"],
        card_holder=os.environ["CARD_HOLDER"],
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        # Email is optional: blank or missing SMTP settings disable it.
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=int(os.environ.get("SMTP_PORT") or "587"),
        smtp_user=os.environ.get("SMTP_USER", ""),
        smtp_pass=os.environ.get("SMTP_PASS", ""),
    )


def save_config(cfg: Config, path: Path = CONFIG_PATH) -> None:
    """Used by the interactive fallback to persist learned field values."""
    data = cfg.model_dump(by_alias=True, mode="json")
    with path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)

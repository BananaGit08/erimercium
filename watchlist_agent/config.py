"""Configuration and scheduling helpers.

Every secret is read from the environment. Nothing sensitive is ever committed.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# The digest is scheduled for 4:30pm ET, just after the US market close.
TARGET_ET_HOUR = 16
TARGET_ET_MINUTE = 30

# GitHub Actions cron can fire late. Accept a run that starts anywhere in the
# hour following the target, which tolerates delay without ever letting the
# "wrong" DST cron entry through (the two entries are exactly 60 minutes apart
# in ET terms, and only one of them can land inside this window).
RUN_WINDOW_MINUTES = 60

DEFAULT_MOVE_THRESHOLD_PCT = 3.0

FINNHUB_BASE = "https://finnhub.io/api/v1"
COINBASE_BASE = "https://api.exchange.coinbase.com"

# Finnhub's free tier allows 60 requests/minute. Stay comfortably under it.
FINNHUB_MAX_RPM = 55

HTTP_TIMEOUT_SECONDS = 20
HTTP_MAX_RETRIES = 3


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required environment variable {name!r}. "
            "Set it as a GitHub Actions secret (or in your local .env)."
        )
    return value


def finnhub_api_key() -> str:
    return _require("FINNHUB_API_KEY")


def gmail_address() -> str:
    return _require("GMAIL_ADDRESS")


def gmail_app_password() -> str:
    # Google displays app passwords in groups of four; the spaces are cosmetic.
    return _require("GMAIL_APP_PASSWORD").replace(" ", "")


def recipient_address() -> str:
    return os.environ.get("DIGEST_RECIPIENT", "christian.na@icloud.com").strip()


def sec_user_agent() -> str:
    """SEC EDGAR requires a descriptive User-Agent with contact details."""
    return os.environ.get(
        "SEC_USER_AGENT", "erimercium-watchlist-agent christian@banananorth.com"
    )


def now_et() -> datetime:
    return datetime.now(ET)


def should_run_now(now: datetime | None = None) -> tuple[bool, str]:
    """Return whether the current ET time falls inside the send window.

    The workflow registers two cron entries -- one correct under EST, one under
    EDT -- so that exactly one of them lands at 4:30pm ET year round. This gate
    discards the other.
    """
    now = now or now_et()
    minutes_now = now.hour * 60 + now.minute
    target = TARGET_ET_HOUR * 60 + TARGET_ET_MINUTE
    if target <= minutes_now < target + RUN_WINDOW_MINUTES:
        return True, f"{now:%Y-%m-%d %H:%M %Z} is inside the 4:30pm ET send window"
    return False, (
        f"{now:%Y-%m-%d %H:%M %Z} is outside the 4:30pm ET send window "
        "(this is the off-season DST cron entry; skipping)"
    )

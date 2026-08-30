"""Configuration and scheduling helpers.

Every secret is read from the environment. Nothing sensitive is ever committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# The digest targets 4:30pm ET, just after the US close. GitHub cron is UTC-only
# and DST-unaware, so the workflow registers both offsets and exactly one is
# correct on any given day.
EDT_CRON = "30 20 * * 1-5"  # 20:30 UTC == 4:30pm EDT (March-November)
EST_CRON = "30 21 * * 1-5"  # 21:30 UTC == 4:30pm EST (November-March)

DEFAULT_MOVE_THRESHOLD_PCT = 3.0

# Defaults for per-ticker flagging; overridable from watchlist.json.
DEFAULT_Z_SCORE = 2.0
DEFAULT_MIN_ABS_PCT = 1.5
DEFAULT_ALWAYS_FLAG_ABS_PCT = 8.0
DEFAULT_MAX_MOVERS_SHOWN = 12


@dataclass(frozen=True)
class Thresholds:
    """When a price move counts as worth reporting. See watchlist.json."""

    z_score: float
    min_abs_pct: float
    always_flag_abs_pct: float
    fallback_pct: float
    max_shown: int

FINNHUB_BASE = "https://finnhub.io/api/v1"
COINBASE_BASE = "https://api.exchange.coinbase.com"
STOOQ_BASE = "https://stooq.com"

# Volatility window. ~60 trading days is about three months: long enough to be
# stable, short enough to track a name whose character has changed.
VOLATILITY_LOOKBACK_DAYS = 60
VOLATILITY_MIN_OBSERVATIONS = 30
VOLATILITY_MAX_WORKERS = 4

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


def expected_cron(now: datetime | None = None) -> str:
    """Which of the two cron entries is the correct one for today's ET offset."""
    now = now or now_et()
    return EDT_CRON if now.utcoffset() == timedelta(hours=-4) else EST_CRON


def _normalize_cron(cron: str) -> str:
    return " ".join(cron.split())


def should_run_for_schedule(
    cron: str, now: datetime | None = None
) -> tuple[bool, str]:
    """Decide whether a scheduled run should send the digest.

    This keys off *which cron entry fired*, not the wall-clock time the runner
    happened to start. GitHub routinely delays scheduled workflows -- observed
    delays of 80+ minutes on this repo -- so any wall-clock window is a coin
    flip on whether the digest goes out at all. The triggering cron expression
    is delay-proof: the off-season entry is discarded no matter how late either
    one actually starts, and the in-season entry always sends.
    """
    now = now or now_et()
    fired = _normalize_cron(cron)
    wanted = _normalize_cron(expected_cron(now))
    season = "EDT" if now.utcoffset() == timedelta(hours=-4) else "EST"
    if fired == wanted:
        return True, (
            f"cron {fired!r} is the {season} entry for {now:%Y-%m-%d}; sending "
            f"(runner started {now:%H:%M %Z})"
        )
    return False, (
        f"cron {fired!r} is the off-season entry ({season} is in effect, which "
        f"uses {wanted!r}); skipping"
    )

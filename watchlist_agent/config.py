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
YAHOO_BASE = "https://query1.finance.yahoo.com"
SEC_DATA_BASE = "https://data.sec.gov"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# SEC asks for no more than 10 requests/second and a descriptive User-Agent.
SEC_MAX_RPS = 8

# Yahoo serves JSON to plain HTTP clients; Stooq answers datacenter IPs with a
# JavaScript bot-check page regardless of headers, so it cannot be used here.
YAHOO_USER_AGENT = "Mozilla/5.0 (compatible; erimercium-watchlist-agent/1.0)"

# If most tickers come back without history the per-ticker rule has silently
# degraded to the flat fallback, which is worth saying out loud.
VOLATILITY_MIN_COVERAGE = 0.5

# Volatility window. ~60 trading days is about three months: long enough to be
# stable, short enough to track a name whose character has changed.
VOLATILITY_LOOKBACK_DAYS = 60
VOLATILITY_MIN_OBSERVATIONS = 30

# Scale factor turning a median absolute deviation into a standard-deviation
# equivalent for normally distributed data.
MAD_TO_SIGMA = 1.4826
VOLATILITY_MAX_WORKERS = 4

# How far back to look for news and filings, and how much of it to show.
NEWS_LOOKBACK_DAYS = 7
FILINGS_LOOKBACK_DAYS = 10
MAX_BULLETS_PER_TICKER = 4

# Deep-dive research scheduling.
# Reports go out this many days before a company reports earnings -- far enough
# ahead to act on, close enough that the analyst estimates are settled.
EARNINGS_LEAD_DAYS = 7
# How far ahead to pull the earnings calendar. Wider than the lead time so a
# date that moves closer is still seen before it passes.
EARNINGS_LOOKAHEAD_DAYS = 30
# Quarters of fundamentals to trend in a report.
FUNDAMENTALS_QUARTERS = 6

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

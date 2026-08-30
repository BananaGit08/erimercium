"""Upcoming earnings dates and the consensus expectations that go with them.

Finnhub's earnings calendar is free-tier and carries the EPS and revenue
estimates alongside the date, which is exactly what a pre-earnings report needs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from .config import (
    EARNINGS_LOOKAHEAD_DAYS,
    FINNHUB_BASE,
    HTTP_TIMEOUT_SECONDS,
    finnhub_api_key,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EarningsEvent:
    ticker: str
    date: date
    year: int
    quarter: int
    eps_estimate: float | None
    revenue_estimate: float | None
    hour: str  # "bmo", "amc" or "" -- before/after market

    @property
    def period(self) -> str:
        """Stable identity for a reporting period, e.g. '2026Q3'.

        Companies move their reporting dates routinely, so the date is not a
        safe key for "have we already covered this". The fiscal period is: it
        does not change when the date does.
        """
        return f"{self.year}Q{self.quarter}"

    @property
    def timing(self) -> str:
        return {"bmo": "before market open", "amc": "after market close"}.get(
            self.hour, ""
        )


def fetch_calendar(
    session: requests.Session, days_ahead: int = EARNINGS_LOOKAHEAD_DAYS
) -> dict[str, EarningsEvent]:
    """Upcoming earnings for the whole market, keyed by ticker.

    One request covers every ticker, so this is cheaper than asking per symbol
    and stays well inside the free tier.
    """
    today = date.today()
    try:
        resp = session.get(
            f"{FINNHUB_BASE}/calendar/earnings",
            params={
                "from": today.isoformat(),
                "to": (today + timedelta(days=days_ahead)).isoformat(),
                "token": finnhub_api_key(),
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.warning("earnings calendar request failed: %s", exc)
        return {}
    if not resp.ok:
        log.warning("earnings calendar HTTP %s", resp.status_code)
        return {}
    try:
        rows = resp.json().get("earningsCalendar", [])
    except (ValueError, AttributeError):
        log.warning("earnings calendar payload unusable")
        return {}

    calendar: dict[str, EarningsEvent] = {}
    for row in rows:
        symbol = (row.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            when = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            continue
        year, quarter = row.get("year"), row.get("quarter")
        if not year or not quarter:
            continue
        event = EarningsEvent(
            ticker=symbol,
            date=when,
            year=int(year),
            quarter=int(quarter),
            eps_estimate=row.get("epsEstimate"),
            revenue_estimate=row.get("revenueEstimate"),
            hour=(row.get("hour") or "").lower(),
        )
        # Keep the soonest entry if a symbol appears more than once.
        if symbol not in calendar or event.date < calendar[symbol].date:
            calendar[symbol] = event

    log.info("earnings calendar: %d symbols in the next %d days", len(calendar), days_ahead)
    return calendar


def for_watchlist(
    calendar: dict[str, EarningsEvent], tickers: list[str]
) -> dict[str, EarningsEvent]:
    return {t: calendar[t] for t in tickers if t in calendar}

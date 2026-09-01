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
    finnhub_api_key,
)
from .http import get_json

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


def fetch_recent(
    session: requests.Session, days_back: int
) -> dict[str, EarningsEvent]:
    """Companies that have already reported, keyed by ticker.

    The same endpoint read backwards. Take-aways are written after a call, so
    the queue is built from who has reported rather than who is about to; the
    newest entry wins here, where the soonest wins looking forward.
    """
    today = date.today()
    payload = get_json(
        session,
        f"{FINNHUB_BASE}/calendar/earnings",
        label="recent earnings calendar",
        params={
            "from": (today - timedelta(days=days_back)).isoformat(),
            "to": today.isoformat(),
            "token": finnhub_api_key(),
        },
    )
    if not isinstance(payload, dict):
        log.warning("recent earnings calendar unavailable — nothing will look due")
        return {}

    calendar: dict[str, EarningsEvent] = {}
    for event in _events(payload.get("earningsCalendar") or []):
        existing = calendar.get(event.ticker)
        if existing is None or event.date > existing.date:
            calendar[event.ticker] = event
    log.info(
        "%d symbols reported in the last %d days", len(calendar), days_back
    )
    return calendar


def _events(rows: list) -> list[EarningsEvent]:
    """Parse calendar rows, skipping anything without a usable date or period."""
    events: list[EarningsEvent] = []
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
        events.append(EarningsEvent(
            ticker=symbol,
            date=when,
            year=int(year),
            quarter=int(quarter),
            eps_estimate=row.get("epsEstimate"),
            revenue_estimate=row.get("revenueEstimate"),
            hour=(row.get("hour") or "").lower(),
        ))
    return events


def fetch_calendar(
    session: requests.Session, days_ahead: int = EARNINGS_LOOKAHEAD_DAYS
) -> dict[str, EarningsEvent]:
    """Upcoming earnings for the whole market, keyed by ticker.

    One request covers every ticker, so this is cheaper than asking per symbol
    and stays well inside the free tier.
    """
    today = date.today()
    # The whole day's scheduling rests on this one request: an empty calendar
    # means nothing looks due, so every pre-earnings report that should have
    # gone out is simply skipped without anything appearing to fail.
    payload = get_json(
        session,
        f"{FINNHUB_BASE}/calendar/earnings",
        label="earnings calendar",
        params={
            "from": today.isoformat(),
            "to": (today + timedelta(days=days_ahead)).isoformat(),
            "token": finnhub_api_key(),
        },
    )
    if not isinstance(payload, dict):
        log.warning("earnings calendar unavailable — nothing will look due today")
        return {}
    rows = payload.get("earningsCalendar") or []

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

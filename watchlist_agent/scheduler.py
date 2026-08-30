"""Deciding which tickers are due for a research report, and why."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from .config import EARNINGS_LEAD_DAYS
from .earnings import EarningsEvent
from .report_state import ReportState

log = logging.getLogger(__name__)

# 8-K item codes that justify redoing a full report. Deliberately narrower than
# the set worth mentioning in the daily digest: these are the events that change
# the picture of a company, not merely the news about it.
#
# 2.02 (results of operations) is excluded on purpose -- earnings are already
# covered by the pre-earnings cadence, and including it would fire a duplicate
# report every quarter for every holding.
REPORT_TRIGGER_8K_ITEMS = {
    "1.01": "entered a material definitive agreement",
    "1.02": "terminated a material definitive agreement",
    "1.03": "entered bankruptcy or receivership",
    "2.01": "completed an acquisition or disposition of assets",
    "2.06": "recorded a material impairment",
    "3.01": "received a delisting or listing-rule notice",
    "4.01": "changed its accountant",
    "4.02": "said previously issued financials cannot be relied upon",
    "5.01": "had a change in control",
    "5.02": "had a director or officer departure or appointment",
}


@dataclass(frozen=True)
class ReportRequest:
    ticker: str
    kind: str  # "baseline" | "earnings" | "event"
    reason: str
    period: str = ""
    accession: str = ""

    @property
    def dedupe_key(self) -> str:
        return self.period or self.accession or "baseline"


def due_for_baseline(
    tickers: list[str], state: ReportState, limit: int = 0
) -> list[ReportRequest]:
    """Tickers that have never had an in-depth report.

    `limit` spreads the initial hundred over several runs rather than spending
    the whole first month's budget in one burst -- and it means the first few
    can be read before the rest are generated.
    """
    pending = [
        ReportRequest(t, "baseline", "no in-depth report on file yet")
        for t in tickers
        if not state.has_baseline(t)
    ]
    return pending[:limit] if limit > 0 else pending


def due_for_earnings(
    calendar: dict[str, EarningsEvent],
    state: ReportState,
    today: date | None = None,
    lead_days: int = EARNINGS_LEAD_DAYS,
) -> list[ReportRequest]:
    """Tickers reporting within the lead window that have not been covered.

    The window is one-sided: anything from today up to `lead_days` out
    qualifies. That means a company whose date moves closer is still caught,
    and one whose date slips further out simply waits.
    """
    today = today or date.today()
    requests: list[ReportRequest] = []
    for ticker, event in sorted(calendar.items(), key=lambda kv: kv[1].date):
        days_out = (event.date - today).days
        if not 0 <= days_out <= lead_days:
            continue
        if state.has_earnings(ticker, event.period):
            continue
        timing = f" ({event.timing})" if event.timing else ""
        requests.append(
            ReportRequest(
                ticker,
                "earnings",
                f"reports {event.period} on {event.date:%b %d}{timing}, "
                f"{days_out} day{'s' if days_out != 1 else ''} out",
                period=event.period,
            )
        )
    return requests


def due_for_event(
    ticker: str, form: str, items: str, accession: str, state: ReportState
) -> ReportRequest | None:
    """A filing that changes the picture enough to justify a fresh report.

    Reading the 8-K item codes rather than judging the headline means the
    trigger is exact: a CEO departure is Item 5.02 whether or not anyone wrote
    about it, and companies are required to file it.
    """
    if (form or "").upper() != "8-K" or not accession:
        return None
    if state.has_event(ticker, accession):
        return None
    codes = [c.strip() for c in (items or "").split(",") if c.strip()]
    hits = [REPORT_TRIGGER_8K_ITEMS[c] for c in codes if c in REPORT_TRIGGER_8K_ITEMS]
    if not hits:
        return None
    return ReportRequest(
        ticker,
        "event",
        "8-K: " + "; ".join(dict.fromkeys(hits)),
        accession=accession,
    )

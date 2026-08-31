"""Quarterly financial trends from SEC XBRL company facts.

These are the numbers a pre-earnings report is built on: where revenue,
margins and leverage have been going over the last several quarters. They come
straight from what the company filed, so no estimate or vendor sits in between.

XBRL's difficulty is that companies tag the same idea differently, so each
metric lists candidate concepts and the first one that returns usable data
wins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import requests

from .config import FUNDAMENTALS_QUARTERS, HTTP_TIMEOUT_SECONDS, SEC_DATA_BASE
from .filings import throttle

log = logging.getLogger(__name__)

# Ordered by how commonly each tag is used for the concept.
CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ],
    # NKE, among others, does not report OperatingIncomeLoss. Fall through to
    # the pre-tax line, which is close enough to trend a margin on.
    "operating_income": [
        "OperatingIncomeLoss",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "gross_profit": ["GrossProfit"],
}
# Point-in-time balances rather than period flows, so they are read differently.
BALANCE_CONCEPTS: dict[str, list[str]] = {
    "total_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
        "DebtLongtermAndShorttermCombinedAmount",
        "LongTermDebtCurrent",
    ],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
}

QUARTER_MIN_DAYS, QUARTER_MAX_DAYS = 80, 100


@dataclass
class Fundamentals:
    ticker: str
    revenue: dict[str, float] = field(default_factory=dict)
    operating_income: dict[str, float] = field(default_factory=dict)
    net_income: dict[str, float] = field(default_factory=dict)
    gross_profit: dict[str, float] = field(default_factory=dict)
    total_debt: float | None = None
    equity: float | None = None
    cash: float | None = None
    missing: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def margin_series(self) -> tuple[str, dict[str, float]]:
        """The best available margin trend, and what it is called.

        Companies tag profit lines inconsistently, so rather than report
        nothing when OperatingIncomeLoss is absent, fall back through gross
        and net profit. Naming which one is in use matters -- a reader
        comparing "margin" across reports needs to know it is the same line.
        """
        for label, series in (
            ("operating margin", self.operating_income),
            ("gross margin", self.gross_profit),
            ("net margin", self.net_income),
        ):
            margins = {
                period: series[period] / self.revenue[period] * 100
                for period in self.revenue
                if period in series and self.revenue[period]
            }
            if margins:
                return label, margins
        return "margin", {}

    def revenue_growth_yoy(self) -> float | None:
        """Most recent quarter against the same quarter a year earlier."""
        periods = sorted(self.revenue, reverse=True)
        if len(periods) < 5:
            return None
        latest, year_ago = periods[0], periods[4]
        if not self.revenue[year_ago]:
            return None
        return (self.revenue[latest] - self.revenue[year_ago]) / abs(
            self.revenue[year_ago]
        ) * 100

    def debt_to_equity(self) -> float | None:
        if self.total_debt is None or not self.equity:
            return None
        return self.total_debt / self.equity


def _concept_url(cik: str, tag: str) -> str:
    return f"{SEC_DATA_BASE}/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"


def _fetch_concept(
    session: requests.Session, cik: str, tag: str, problems: list[str] | None = None
) -> list[dict] | None:
    """One XBRL concept, or None.

    A metric can go missing for two very different reasons: the company does
    not use that tag (a 404, entirely normal), or SEC refused the request. The
    second used to look exactly like the first, which is how a rate-limited run
    reported a company as having no net income.
    """
    throttle()  # SEC allows 10 requests/second across all endpoints.
    try:
        resp = session.get(_concept_url(cik, tag), timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.warning("SEC request failed for %s: %s", tag, exc)
        if problems is not None:
            problems.append(f"{tag}: {exc}")
        return None
    if resp.status_code == 404:
        return None  # This company simply does not report that concept.
    if not resp.ok:
        log.warning("SEC HTTP %s for %s (not a missing tag)", resp.status_code, tag)
        if problems is not None:
            problems.append(f"{tag}: HTTP {resp.status_code}")
        return None
    try:
        return resp.json().get("units", {}).get("USD", [])
    except (ValueError, AttributeError):
        log.warning("SEC payload unusable for %s", tag)
        if problems is not None:
            problems.append(f"{tag}: unparseable response")
        return None


def _quarterly(entries: list[dict], limit: int) -> dict[str, float]:
    """Most recent quarterly values, keyed by period end date.

    XBRL mixes quarterly, annual and year-to-date figures in one series, so
    durations outside a quarter are discarded rather than compared against
    each other.
    """
    quarters: dict[str, float] = {}
    for entry in entries:
        start, end, val = entry.get("start"), entry.get("end"), entry.get("val")
        if not start or not end or val is None:
            continue
        try:
            span = (date.fromisoformat(end) - date.fromisoformat(start)).days
        except ValueError:
            continue
        if not QUARTER_MIN_DAYS <= span <= QUARTER_MAX_DAYS:
            continue
        # Later filings restate earlier ones; the last write for a period wins.
        quarters[end] = float(val)
    return {k: quarters[k] for k in sorted(quarters, reverse=True)[:limit]}


def _latest_balance(entries: list[dict]) -> float | None:
    dated = [e for e in entries if e.get("end") and e.get("val") is not None]
    if not dated:
        return None
    return float(max(dated, key=lambda e: e["end"])["val"])


def fetch(
    session: requests.Session,
    ticker: str,
    cik: str,
    quarters: int = FUNDAMENTALS_QUARTERS,
) -> Fundamentals:
    """Quarterly trends and latest balances for one company."""
    result = Fundamentals(ticker=ticker)

    for metric, tags in CONCEPTS.items():
        for tag in tags:
            entries = _fetch_concept(session, cik, tag, result.problems)
            if entries:
                series = _quarterly(entries, quarters)
                if series:
                    setattr(result, metric, series)
                    break
        else:
            result.missing.append(metric)

    for metric, tags in BALANCE_CONCEPTS.items():
        for tag in tags:
            entries = _fetch_concept(session, cik, tag, result.problems)
            if entries:
                value = _latest_balance(entries)
                if value is not None:
                    setattr(result, metric, value)
                    break
        else:
            result.missing.append(metric)

    log.info(
        "%s fundamentals: %d revenue quarters%s%s",
        ticker,
        len(result.revenue),
        f", missing {', '.join(result.missing)}" if result.missing else "",
        f", {len(result.problems)} request problems" if result.problems else "",
    )
    if result.problems:
        log.warning("%s SEC request problems: %s", ticker, "; ".join(result.problems[:5]))
    return result

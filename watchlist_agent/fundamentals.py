"""Quarterly financial trends from SEC XBRL company facts.

These are the numbers a pre-earnings report is built on: where revenue,
margins and leverage have been going over the last several quarters. They come
straight from what the company filed, so no estimate or vendor sits in between.

Two XBRL difficulties are handled here. Companies tag the same idea
differently, so each metric lists candidate concepts. And SEC's per-concept
endpoint is unreliable: for PYPL it answers 200 with revenue that stops in
2020 and nothing at all for net income, debt or equity, while the same
company's filings are perfectly current. So facts come from `companyfacts`,
one request carrying every concept, rather than ~20 per-concept requests.

A series is also checked for recency. Reporting a 2020 revenue trend as
current is worse than reporting none, because nothing on the page says how old
it is.
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

# A quarterly series whose newest period predates this is not "current".
MAX_SERIES_AGE_DAYS = 250


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
    stale: list[str] = field(default_factory=list)
    tags_used: dict[str, str] = field(default_factory=dict)

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


def _fetch_facts(
    session: requests.Session, cik: str, problems: list[str]
) -> dict[str, list[dict]]:
    """Every us-gaap concept for a company, keyed by tag.

    One request rather than one per concept. `companyconcept` returns 200 with
    silently truncated or empty data for some filers; `companyfacts` is the
    complete dataset and cannot disagree with itself between concepts.
    """
    throttle()  # SEC allows 10 requests/second across all endpoints.
    url = f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT_SECONDS * 3)
    except requests.RequestException as exc:
        log.warning("companyfacts request failed for CIK %s: %s", cik, exc)
        problems.append(f"companyfacts: {exc}")
        return {}
    if not resp.ok:
        log.warning("companyfacts HTTP %s for CIK %s", resp.status_code, cik)
        problems.append(f"companyfacts: HTTP {resp.status_code}")
        return {}
    try:
        gaap = resp.json().get("facts", {}).get("us-gaap", {})
    except (ValueError, AttributeError):
        problems.append("companyfacts: unparseable response")
        return {}

    return {
        tag: body.get("units", {}).get("USD", [])
        for tag, body in gaap.items()
        if isinstance(body, dict)
    }


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


def _is_current(series: dict[str, float]) -> bool:
    if not series:
        return False
    try:
        newest = date.fromisoformat(max(series))
    except ValueError:
        return False
    return (date.today() - newest).days <= MAX_SERIES_AGE_DAYS


def fetch(
    session: requests.Session,
    ticker: str,
    cik: str,
    quarters: int = FUNDAMENTALS_QUARTERS,
) -> Fundamentals:
    """Quarterly trends and latest balances for one company."""
    result = Fundamentals(ticker=ticker)
    facts = _fetch_facts(session, cik, result.problems)
    if not facts:
        result.missing.extend(list(CONCEPTS) + list(BALANCE_CONCEPTS))
        return result

    for metric, tags in CONCEPTS.items():
        fallback: tuple[str, dict[str, float]] | None = None
        for tag in tags:
            series = _quarterly(facts.get(tag, []), quarters)
            if not series:
                continue
            if _is_current(series):
                setattr(result, metric, series)
                result.tags_used[metric] = tag
                break
            # Usable but old -- keep it only if nothing current turns up.
            fallback = fallback or (tag, series)
        else:
            if fallback:
                tag, series = fallback
                setattr(result, metric, series)
                result.tags_used[metric] = tag
                result.stale.append(f"{metric} (newest {max(series)}, tag {tag})")
            else:
                result.missing.append(metric)

    for metric, tags in BALANCE_CONCEPTS.items():
        for tag in tags:
            value = _latest_balance(facts.get(tag, []))
            if value is not None:
                setattr(result, metric, value)
                result.tags_used[metric] = tag
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
    if result.stale:
        log.warning("%s stale fundamentals: %s", ticker, "; ".join(result.stale))
    return result

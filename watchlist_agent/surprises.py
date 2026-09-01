"""How a company has done against consensus over recent quarters.

Calibration, not scorekeeping. A pre-earnings report is built on estimates, and
estimates are worth different amounts for different companies: some managements
guide low and clear the bar every time, some miss repeatedly, some swing either
way. Knowing which kind of name this is tells the reader how much weight the
rest of the report's expectations deserve.

**A tally on its own is close to useless, which is the trap this module exists
to avoid.** Companies guide conservatively and most large caps beat EPS
consensus most quarters, so "beat 4 of 4" is roughly the base rate rather than
a finding. What separates one company from another is the size of the
surprises, whether they are consistent or scattered, and which way they are
trending -- so those are what `characterise` reports, and the bare count never
appears without them.

One estimator note. The percentage surprise is unstable when the estimate is
near zero: a two-cent beat on a one-cent estimate is +200% and means nothing.
Below a floor the percentage is withheld and the difference is reported in
cents instead, which is what a reader would actually want to know.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from statistics import fmean

import requests

from .config import FINNHUB_BASE, finnhub_api_key
from .http import get_json

log = logging.getLogger(__name__)

# Below this, a percentage surprise says more about the denominator than the
# company. Roughly a nickel: small enough to keep real EPS lines, large enough
# to exclude the break-even names where percentages explode.
MIN_ESTIMATE_FOR_PCT = 0.05

QUARTERS = 4


@dataclass(frozen=True)
class Surprise:
    """One quarter's actual against what consensus expected."""

    period: str
    actual: float
    estimate: float

    @property
    def difference(self) -> float:
        return self.actual - self.estimate

    @property
    def pct(self) -> float | None:
        """Percentage surprise, or None when the estimate is too small to bear one."""
        if abs(self.estimate) < MIN_ESTIMATE_FOR_PCT:
            return None
        return self.difference / abs(self.estimate) * 100.0

    @property
    def verdict(self) -> str:
        if self.difference > 0:
            return "beat"
        if self.difference < 0:
            return "missed"
        return "in line"

    @property
    def summary(self) -> str:
        """The surprise as a reader would say it."""
        pct = self.pct
        if pct is None:
            return f"{self.verdict} by ${abs(self.difference):.2f}"
        if self.verdict == "in line":
            return "in line"
        return f"{self.verdict} by {abs(pct):.1f}%"


@dataclass(frozen=True)
class SurpriseHistory:
    ticker: str
    quarters: list[Surprise] = field(default_factory=list)
    note: str = ""

    @property
    def beats(self) -> int:
        return sum(1 for q in self.quarters if q.verdict == "beat")

    @property
    def misses(self) -> int:
        return sum(1 for q in self.quarters if q.verdict == "missed")

    @property
    def percentages(self) -> list[float]:
        return [q.pct for q in self.quarters if q.pct is not None]

    @property
    def spread(self) -> float | None:
        """How far apart the best and worst quarters are, in percentage points.

        The plainest measure of consistency: a reliable beater and a name that
        swings either way can share an average and differ entirely here.
        """
        pcts = self.percentages
        if len(pcts) < 2:
            return None
        return max(pcts) - min(pcts)

    def characterise(self) -> str:
        """One line naming the pattern -- never a bare count."""
        if not self.quarters:
            return self.note or "no surprise history available"

        n = len(self.quarters)
        # Oldest first reads as a story; the input arrives newest first.
        ordered = list(reversed(self.quarters))
        pcts = [q.pct for q in ordered]
        known = [p for p in pcts if p is not None]
        scope = f"{n} quarter{'s' if n != 1 else ''} on file"

        if self.beats == n:
            body = f"beat consensus in all {n}"
            if len(known) == n and n >= 3:
                if all(b < a for a, b in zip(known, known[1:])):
                    body += ", but by a narrowing margin"
                elif all(b > a for a, b in zip(known, known[1:])):
                    body += ", by a widening margin"
            if known:
                body += f" (averaging {fmean(known):+.1f}%)"
        elif self.misses == n:
            body = f"missed consensus in all {n}"
            if known:
                body += f" (averaging {fmean(known):+.1f}%)"
        else:
            body = f"{self.beats} beat{'s' if self.beats != 1 else ''} and " \
                   f"{self.misses} miss{'es' if self.misses != 1 else ''}"
            spread = self.spread
            if spread is not None and known:
                body += (
                    f", ranging from {min(known):+.1f}% to {max(known):+.1f}%"
                    if spread >= 1.0
                    else ", all within a point of consensus"
                )

        if n < QUARTERS:
            return f"{body} — only {scope}"
        return body


def _parse(ticker: str, rows: list) -> list[Surprise]:
    quarters: list[Surprise] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        actual, estimate = row.get("actual"), row.get("estimate")
        # A quarter with no published estimate is not a miss, it is an absence.
        # Counting it either way would misstate the record.
        if actual is None or estimate is None:
            continue
        try:
            actual, estimate = float(actual), float(estimate)
        except (TypeError, ValueError):
            continue
        year, quarter = row.get("year"), row.get("quarter")
        period = f"{year}Q{quarter}" if year and quarter else str(row.get("period", "?"))
        quarters.append(Surprise(period=period, actual=actual, estimate=estimate))
    return quarters[:QUARTERS]


def fetch(session: requests.Session, ticker: str) -> SurpriseHistory:
    """Recent quarters of EPS actual against estimate, newest first.

    Never raises: a report is worth sending without this, and the free tier may
    answer with the HTML paywall page it serves for premium endpoints -- which
    `get_json` reports as a note rather than as data.
    """
    status: dict = {}
    payload = get_json(
        session,
        f"{FINNHUB_BASE}/stock/earnings",
        label=f"finnhub stock/earnings {ticker}",
        status=status,
        params={"symbol": ticker, "token": finnhub_api_key()},
    )
    if not isinstance(payload, list):
        note = status.get("note", "no surprise history returned")
        log.info("%s surprise history unavailable: %s", ticker, note)
        return SurpriseHistory(ticker=ticker, note=note)

    quarters = _parse(ticker, payload)
    history = SurpriseHistory(ticker=ticker, quarters=quarters)
    log.info("%s surprise history: %s", ticker, history.characterise())
    return history

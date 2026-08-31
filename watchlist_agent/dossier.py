"""Everything known about one company, assembled for a research report.

Gathering is deliberately separate from writing. Every field here comes from a
public source that costs nothing, so a dossier can be built, inspected and
tested without involving a language model at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

from .config import RESEARCH_FILINGS_DAYS, RESEARCH_NEWS_DAYS
from .earnings import EarningsEvent
from .filings import company_name, fetch_filings, sec_session
from .fundamentals import Fundamentals, fetch as fetch_fundamentals
from .market_data import MarketData, fetch as fetch_market, peer_comparison
from .materiality import Bullet
from .news import fetch_news
from .prices import Quote

log = logging.getLogger(__name__)


@dataclass
class Dossier:
    ticker: str
    company: str = ""
    reason: str = ""
    kind: str = "baseline"
    quote: Quote | None = None
    sigma: float | None = None
    earnings: EarningsEvent | None = None
    fundamentals: Fundamentals | None = None
    market: MarketData | None = None
    peer_pe: dict[str, float] = field(default_factory=dict)
    news: list[Bullet] = field(default_factory=list)
    filings: list[Bullet] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return f"{self.company} ({self.ticker})" if self.company else self.ticker

    def note_gap(self, what: str) -> None:
        """Record something unavailable, so the report can say so explicitly."""
        self.gaps.append(what)


def build(
    ticker: str,
    kind: str = "baseline",
    reason: str = "",
    quote: Quote | None = None,
    sigma: float | None = None,
    earnings: EarningsEvent | None = None,
) -> Dossier:
    """Collect every free source for one ticker."""
    dossier = Dossier(
        ticker=ticker, kind=kind, reason=reason, quote=quote, sigma=sigma,
        earnings=earnings,
    )

    with requests.Session() as finnhub, sec_session() as sec:
        finnhub.headers["User-Agent"] = "erimercium-watchlist-agent"

        dossier.company = company_name(sec, ticker)
        if not dossier.company:
            dossier.note_gap(
                "not a US filer in EDGAR (foreign issuer, ETF or OTC) — "
                "no SEC filings or fundamentals"
            )

        dossier.market = fetch_market(finnhub, ticker)
        if not dossier.market.metrics:
            dossier.note_gap("no valuation metrics available")
        if not dossier.market.recommendations.months:
            dossier.note_gap(
                "no analyst ratings available"
                + (f" — {dossier.market.ratings_note}" if dossier.market.ratings_note else "")
            )
        # Price targets are a premium Finnhub endpoint, so sentiment is
        # rating-mix only. Say so rather than let a reader assume otherwise.
        dossier.note_gap("analyst price targets unavailable (premium data)")

        if dossier.market.peers:
            dossier.peer_pe = peer_comparison(finnhub, dossier.market.peers)

        dossier.news = fetch_news(
            finnhub, ticker, dossier.company, days=RESEARCH_NEWS_DAYS
        )
        if not dossier.news:
            dossier.note_gap("no material news in the recent window")

        if dossier.company:
            dossier.filings = fetch_filings(sec, ticker, days=RESEARCH_FILINGS_DAYS)
            from .filings import _load_cik_map  # noqa: PLC0415 - internal reuse

            entry = _load_cik_map(sec).get(ticker.upper())
            if entry:
                dossier.fundamentals = fetch_fundamentals(sec, ticker, entry[0])
                if not dossier.fundamentals.revenue:
                    dossier.note_gap("no quarterly revenue series in XBRL")

    log.info(
        "%s dossier: %d news, %d filings, %d peers, %d gaps",
        ticker, len(dossier.news), len(dossier.filings),
        len(dossier.market.peers) if dossier.market else 0, len(dossier.gaps),
    )
    return dossier


def to_prompt_context(dossier: Dossier) -> str:
    """Render a dossier as the factual material a report is written from."""
    lines = [f"COMPANY: {dossier.title}", f"REPORT TRIGGER: {dossier.reason or dossier.kind}"]

    if dossier.quote:
        lines.append(
            f"LATEST MOVE: {dossier.quote.change_pct:+.2f}% "
            f"(${dossier.quote.previous_close:,.2f} -> ${dossier.quote.current:,.2f})"
            + (f", typical daily move ±{dossier.sigma:.1f}%" if dossier.sigma else "")
        )
    if dossier.earnings:
        e = dossier.earnings
        lines += [
            "",
            f"UPCOMING EARNINGS: {e.period} on {e.date:%B %d, %Y} {e.timing}".rstrip(),
            f"  consensus EPS estimate: {e.eps_estimate if e.eps_estimate is not None else 'n/a'}",
            f"  consensus revenue estimate: "
            f"{f'${e.revenue_estimate:,.0f}' if e.revenue_estimate else 'n/a'}",
        ]

    if dossier.fundamentals:
        f = dossier.fundamentals
        lines += ["", "QUARTERLY FUNDAMENTALS (from SEC filings, newest first):"]
        margin_label, margins = f.margin_series()
        for period in sorted(f.revenue, reverse=True):
            margin = margins.get(period)
            lines.append(
                f"  {period}: revenue ${f.revenue[period]:,.0f}"
                + (f", {margin_label} {margin:.1f}%" if margin is not None else "")
            )
        growth = f.revenue_growth_yoy()
        if growth is not None:
            lines.append(f"  revenue growth YoY (latest quarter): {growth:+.1f}%")
        if f.total_debt is not None:
            lines.append(f"  long-term debt: ${f.total_debt:,.0f}")
        de = f.debt_to_equity()
        if de is not None:
            lines.append(f"  debt/equity: {de:.2f}")

    if dossier.market and dossier.market.metrics:
        lines += ["", "VALUATION AND MARGINS:"]
        for label, value in dossier.market.labelled_metrics().items():
            lines.append(f"  {label}: {value:,.2f}")

    if dossier.peer_pe:
        lines += ["", "PEER P/E (TTM):"]
        for peer, pe in sorted(dossier.peer_pe.items(), key=lambda kv: kv[1]):
            lines.append(f"  {peer}: {pe:.1f}")

    if dossier.market and dossier.market.recommendations.months:
        lines += ["", "ANALYST RATINGS (newest first):"]
        for month in dossier.market.recommendations.months:
            lines.append(
                f"  {month.get('period', '?')}: "
                f"{month.get('strongBuy', 0)} strong buy, {month.get('buy', 0)} buy, "
                f"{month.get('hold', 0)} hold, {month.get('sell', 0)} sell, "
                f"{month.get('strongSell', 0)} strong sell"
            )
        direction = dossier.market.recommendations.direction()
        if direction:
            lines.append(f"  direction: {direction}")

    if dossier.filings:
        lines += ["", "RECENT SEC FILINGS:"]
        lines += [f"  {b.text} [{b.url}]" for b in dossier.filings]

    if dossier.news:
        lines += ["", "RECENT NEWS:"]
        lines += [f"  {b.text} [{b.url}]" for b in dossier.news]

    if dossier.gaps:
        lines += ["", "KNOWN DATA GAPS (state these rather than working around them):"]
        lines += [f"  - {g}" for g in dossier.gaps]

    return "\n".join(lines)

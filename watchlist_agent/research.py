"""Gathering the news and filings behind a flagged move."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import requests

from .config import MAX_BULLETS_PER_TICKER
from .filings import company_name, fetch_filings, sec_session
from .materiality import Bullet, conflicting_move
from .news import fetch_news
from .watchlist import is_crypto

log = logging.getLogger(__name__)


def _for_ticker(ticker: str) -> tuple[str, list[Bullet]]:
    if is_crypto(ticker):
        # No company news feed and no SEC filings for a currency pair.
        return ticker, []

    bullets: list[Bullet] = []
    with requests.Session() as finnhub, sec_session() as sec:
        finnhub.headers["User-Agent"] = "erimercium-watchlist-agent"
        # The SEC ticker map supplies the registered company name, which is
        # what lets news be checked against the company rather than the symbol.
        company = company_name(sec, ticker)
        bullets.extend(fetch_news(finnhub, ticker, company))
        bullets.extend(fetch_filings(sec, ticker))

    bullets.sort(key=lambda b: b.sort_key, reverse=True)
    return ticker, bullets[:MAX_BULLETS_PER_TICKER]


def gather(tickers: list[str]) -> dict[str, list[Bullet]]:
    """Material news and filings per ticker, best first.

    Only flagged tickers are researched. Running this across the whole
    watchlist would mean 200 requests a day to answer a question nobody asked
    about the 90-odd names that did nothing.
    """
    if not tickers:
        return {}
    log.info("gathering news and filings for %d flagged tickers", len(tickers))
    results: dict[str, list[Bullet]] = {}
    # Two workers: EDGAR's rate limit is global and enforced by a shared lock.
    with ThreadPoolExecutor(max_workers=2) as pool:
        for ticker, bullets in pool.map(_for_ticker, tickers):
            if bullets:
                results[ticker] = bullets
    log.info(
        "found material items for %d of %d tickers", len(results), len(tickers)
    )
    return results


def flag_stale_moves(
    research: dict[str, list[Bullet]], movers: list
) -> dict[str, list[Bullet]]:
    """Mark headlines whose own price figure contradicts the move we measured.

    A wire story is frozen at the moment it was filed. On a day that keeps
    running, an 11am headline saying "Shares Rise 4.6%" sits under our
    close-to-close +13.85% and reads as the digest disagreeing with itself --
    which is how a reader took it, asking whether the data was stale. It was
    the headline that was stale, not the price.

    The headline is kept, because the news in it is still news. Only its
    number is annotated.
    """
    if not research:
        return research
    actual = {m.ticker: m.change_pct for m in movers}
    flagged: dict[str, list[Bullet]] = {}
    for ticker, bullets in research.items():
        if ticker not in actual:
            flagged[ticker] = bullets
            continue
        moved = actual[ticker]
        out = []
        for bullet in bullets:
            stated = (
                conflicting_move(bullet.text, moved)
                if bullet.kind == "news" else None
            )
            if stated is None:
                out.append(bullet)
                continue
            log.info(
                "%s: headline says %+.1f%% against our %+.1f%% — annotating",
                ticker, stated, moved,
            )
            out.append(replace(
                bullet,
                # Deliberately says nothing about when the headline was
                # written. It may be from earlier in the session or from an
                # older story the feed still carries; either way the only
                # claim we can stand behind is our own figure.
                note=f"{stated:+.1f}% is the publisher's figure, not ours — "
                     f"our close-to-close move for {ticker} is {moved:+.2f}%",
            ))
        flagged[ticker] = out
    return flagged

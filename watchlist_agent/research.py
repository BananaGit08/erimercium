"""Gathering the news and filings behind a flagged move."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import MAX_BULLETS_PER_TICKER
from .filings import fetch_filings, sec_session
from .materiality import Bullet
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
        bullets.extend(fetch_news(finnhub, ticker))
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

"""Price fetching.

Equities and US-listed ADRs come from Finnhub's /quote endpoint. Crypto pairs
(BTC-USD and friends) are not covered by that endpoint on the free tier, so
they are priced from Coinbase's public candles API, which needs no key.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from .config import (
    COINBASE_BASE,
    FINNHUB_BASE,
    FINNHUB_MAX_RPM,
    HTTP_MAX_RETRIES,
    HTTP_TIMEOUT_SECONDS,
    finnhub_api_key,
)
from .watchlist import is_crypto

log = logging.getLogger(__name__)


@dataclass
class Quote:
    ticker: str
    current: float
    previous_close: float
    source: str

    @property
    def change_pct(self) -> float:
        if not self.previous_close:
            return 0.0
        return (self.current - self.previous_close) / self.previous_close * 100.0

    @property
    def change_abs(self) -> float:
        return self.current - self.previous_close


@dataclass
class QuoteFailure:
    ticker: str
    reason: str


class _RateLimiter:
    """Simple spacing limiter to stay inside Finnhub's 60 req/min free tier."""

    def __init__(self, max_per_minute: int) -> None:
        self._interval = 60.0 / max_per_minute
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last = time.monotonic()


def _get_json(session: requests.Session, url: str, **kwargs) -> dict | list | None:
    """GET with retry on transient errors and on Finnhub's 429."""
    for attempt in range(HTTP_MAX_RETRIES):
        try:
            resp = session.get(url, timeout=HTTP_TIMEOUT_SECONDS, **kwargs)
        except requests.RequestException as exc:
            log.warning("request error for %s: %s", url, exc)
        else:
            if resp.status_code == 429:
                backoff = 2 ** (attempt + 1)
                log.warning("rate limited on %s; sleeping %ss", url, backoff)
                time.sleep(backoff)
                continue
            if resp.ok:
                try:
                    return resp.json()
                except ValueError:
                    log.warning("non-JSON response from %s", url)
                    return None
            if 400 <= resp.status_code < 500:
                # Client errors (bad symbol, bad key) will not resolve on retry.
                log.warning("HTTP %s for %s", resp.status_code, url)
                return None
            log.warning("HTTP %s for %s", resp.status_code, url)
        time.sleep(2 ** attempt)
    return None


def _finnhub_quote(session: requests.Session, ticker: str, token: str) -> Quote | QuoteFailure:
    data = _get_json(
        session, f"{FINNHUB_BASE}/quote", params={"symbol": ticker, "token": token}
    )
    if not isinstance(data, dict):
        return QuoteFailure(ticker, "no response from Finnhub")
    current = float(data.get("c") or 0.0)
    prev = float(data.get("pc") or 0.0)
    if current == 0.0 or prev == 0.0:
        # Finnhub returns zeros for symbols it cannot resolve, and for many
        # OTC/foreign tickers that are outside free-tier coverage.
        return QuoteFailure(ticker, "symbol not covered by Finnhub (returned no price)")
    return Quote(ticker, current, prev, "finnhub")


def _coinbase_quote(session: requests.Session, ticker: str) -> Quote | QuoteFailure:
    data = _get_json(
        session,
        f"{COINBASE_BASE}/products/{ticker}/candles",
        params={"granularity": 86400},
    )
    # Candles come back newest-first as [time, low, high, open, close, volume].
    if not isinstance(data, list) or len(data) < 2:
        return QuoteFailure(ticker, "no candle data from Coinbase")
    try:
        current = float(data[0][4])
        prev = float(data[1][4])
    except (IndexError, TypeError, ValueError):
        return QuoteFailure(ticker, "malformed candle data from Coinbase")
    if not current or not prev:
        return QuoteFailure(ticker, "zero price from Coinbase")
    return Quote(ticker, current, prev, "coinbase")


def fetch_quotes(tickers: list[str]) -> tuple[list[Quote], list[QuoteFailure]]:
    """Fetch a quote for every ticker, returning successes and failures apart.

    A ticker that cannot be priced never aborts the run -- it is reported in the
    digest so the watchlist can be corrected.
    """
    token = finnhub_api_key()
    limiter = _RateLimiter(FINNHUB_MAX_RPM)
    quotes: list[Quote] = []
    failures: list[QuoteFailure] = []

    with requests.Session() as session:
        session.headers["User-Agent"] = "erimercium-watchlist-agent"
        for ticker in tickers:
            if is_crypto(ticker):
                result = _coinbase_quote(session, ticker)
            else:
                limiter.wait()
                result = _finnhub_quote(session, ticker, token)
            if isinstance(result, Quote):
                quotes.append(result)
            else:
                failures.append(result)
                log.info("could not price %s: %s", result.ticker, result.reason)

    return quotes, failures


def significant_movers(quotes: list[Quote], threshold_pct: float) -> list[Quote]:
    """Quotes that moved more than threshold_pct in either direction, biggest first."""
    movers = [q for q in quotes if abs(q.change_pct) > threshold_pct]
    return sorted(movers, key=lambda q: abs(q.change_pct), reverse=True)

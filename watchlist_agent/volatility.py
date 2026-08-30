"""Per-ticker volatility, used to decide what counts as an unusual move.

A flat percentage bar is the wrong shape for a watchlist that mixes megacaps
with speculative names: AMZN moving 4% is a major event, RGTI moving 5% is a
Tuesday. So each ticker gets its own bar, derived from how much it normally
moves day to day.

Finnhub's free tier does not include historical candles (/stock/candle returns
403), so daily closes come from Yahoo's chart API for equities and Coinbase for
crypto. Both are free and keyless. Any ticker whose history cannot be fetched
falls back to the flat threshold rather than being dropped.

Stooq was tried first and rejected: it answers a plain client with 404 and a
browser user-agent with a JavaScript bot-check page, so it returns no usable
data from a datacenter IP however it is called.
"""

from __future__ import annotations

import logging
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import (
    COINBASE_BASE,
    VOLATILITY_MIN_COVERAGE,
    HTTP_TIMEOUT_SECONDS,
    VOLATILITY_LOOKBACK_DAYS,
    VOLATILITY_MAX_WORKERS,
    VOLATILITY_MIN_OBSERVATIONS,
    YAHOO_BASE,
    YAHOO_USER_AGENT,
)
from .watchlist import is_crypto

log = logging.getLogger(__name__)

_local = threading.local()


def _session() -> requests.Session:
    """One requests.Session per worker thread."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = YAHOO_USER_AGENT
        _local.session = session
    return session


def _daily_returns_pct(closes: list[float]) -> list[float]:
    """Percent change between consecutive closes, oldest first."""
    returns = []
    for prev, curr in zip(closes, closes[1:]):
        if prev:
            returns.append((curr - prev) / prev * 100.0)
    return returns


def _yahoo_closes(ticker: str) -> list[float] | None:
    """Daily closes from Yahoo's chart API, oldest first."""
    try:
        resp = _session().get(
            f"{YAHOO_BASE}/v8/finance/chart/{ticker}",
            params={"range": "6mo", "interval": "1d"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.debug("yahoo request failed for %s: %s", ticker, exc)
        return None
    if not resp.ok:
        log.debug("yahoo returned HTTP %s for %s", resp.status_code, ticker)
        return None
    try:
        result = resp.json()["chart"]["result"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        log.debug("yahoo payload unusable for %s", ticker)
        return None

    # Adjusted closes, always: the raw close series is not split-adjusted, so a
    # split shows up as a ~50% one-day "return" and wrecks the standard
    # deviation. MRNA came back at 23.6% daily sigma before this.
    raw = None
    try:
        raw = result["indicators"]["adjclose"][0]["adjclose"]
    except (KeyError, IndexError, TypeError):
        log.debug("no adjclose for %s; falling back to close", ticker)
        try:
            raw = result["indicators"]["quote"][0]["close"]
        except (KeyError, IndexError, TypeError):
            return None
    if not raw:
        return None

    # Yahoo emits null for halted or untraded sessions.
    closes = [float(c) for c in raw if c is not None]
    return closes or None


def _coinbase_closes(ticker: str) -> list[float] | None:
    """Daily closes from Coinbase, oldest first."""
    try:
        resp = _session().get(
            f"{COINBASE_BASE}/products/{ticker}/candles",
            params={"granularity": 86400},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.debug("coinbase request failed for %s: %s", ticker, exc)
        return None
    if not resp.ok:
        return None
    try:
        candles = resp.json()
    except ValueError:
        return None
    if not isinstance(candles, list):
        return None
    # Coinbase returns newest-first [time, low, high, open, close, volume].
    closes = []
    for candle in reversed(candles):
        try:
            closes.append(float(candle[4]))
        except (IndexError, TypeError, ValueError):
            continue
    return closes or None


def _sigma_for(ticker: str) -> tuple[str, float | None]:
    closes = _coinbase_closes(ticker) if is_crypto(ticker) else _yahoo_closes(ticker)
    if not closes:
        return ticker, None

    # Trailing window only -- volatility from two years ago says little about
    # what is normal for this stock today.
    window = closes[-(VOLATILITY_LOOKBACK_DAYS + 1) :]
    returns = _daily_returns_pct(window)
    if len(returns) < VOLATILITY_MIN_OBSERVATIONS:
        log.debug("%s has only %d usable returns; skipping", ticker, len(returns))
        return ticker, None

    sigma = statistics.stdev(returns)
    return ticker, sigma if sigma > 0 else None


def fetch_sigmas(tickers: list[str]) -> dict[str, float]:
    """Daily-return standard deviation (in percent) for each ticker.

    Tickers whose history is unavailable are simply absent from the result;
    callers fall back to the flat threshold for those.
    """
    sigmas: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=VOLATILITY_MAX_WORKERS) as pool:
        for ticker, sigma in pool.map(_sigma_for, tickers):
            if sigma is not None:
                sigmas[ticker] = sigma

    missing = [t for t in tickers if t not in sigmas]
    log.info(
        "computed volatility for %d/%d tickers%s",
        len(sigmas),
        len(tickers),
        f" (no history: {', '.join(missing)})" if missing else "",
    )

    coverage = len(sigmas) / len(tickers) if tickers else 1.0
    if coverage < VOLATILITY_MIN_COVERAGE:
        # A source going dark degrades every ticker to the flat fallback, which
        # looks exactly like a normal run unless it is called out. It has
        # happened once already, so say so rather than quietly carrying on.
        log.warning(
            "VOLATILITY COVERAGE %.0f%% (%d/%d) -- the per-ticker rule has "
            "degraded to the flat fallback for most tickers; the history "
            "source is probably failing",
            coverage * 100,
            len(sigmas),
            len(tickers),
        )
    return sigmas


def coverage_warning(sigmas: dict[str, float], tickers: list[str]) -> str | None:
    """A note for the digest when volatility coverage is too low to trust."""
    if not tickers:
        return None
    coverage = len(sigmas) / len(tickers)
    if coverage >= VOLATILITY_MIN_COVERAGE:
        return None
    return (
        f"Volatility history was only available for {len(sigmas)} of "
        f"{len(tickers)} tickers, so most moves below were flagged against the "
        "flat fallback bar rather than each ticker's own range. The price "
        "history source is likely failing."
    )

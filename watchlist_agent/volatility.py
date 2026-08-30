"""Per-ticker volatility, used to decide what counts as an unusual move.

A flat percentage bar is the wrong shape for a watchlist that mixes megacaps
with speculative names: AMZN moving 4% is a major event, RGTI moving 5% is a
Tuesday. So each ticker gets its own bar, derived from how much it normally
moves day to day.

Finnhub's free tier does not include historical candles (/stock/candle returns
403), so daily closes come from Stooq for equities and Coinbase for crypto.
Both are free and keyless. Any ticker whose history cannot be fetched falls
back to the flat threshold rather than being dropped.
"""

from __future__ import annotations

import csv
import io
import logging
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import (
    COINBASE_BASE,
    HTTP_TIMEOUT_SECONDS,
    STOOQ_BASE,
    VOLATILITY_LOOKBACK_DAYS,
    VOLATILITY_MAX_WORKERS,
    VOLATILITY_MIN_OBSERVATIONS,
)
from .watchlist import is_crypto

log = logging.getLogger(__name__)

_local = threading.local()


def _session() -> requests.Session:
    """One requests.Session per worker thread."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "erimercium-watchlist-agent"
        _local.session = session
    return session


def _daily_returns_pct(closes: list[float]) -> list[float]:
    """Percent change between consecutive closes, oldest first."""
    returns = []
    for prev, curr in zip(closes, closes[1:]):
        if prev:
            returns.append((curr - prev) / prev * 100.0)
    return returns


def _stooq_closes(ticker: str) -> list[float] | None:
    """Daily closes from Stooq, oldest first. US equities use the .us suffix."""
    symbol = f"{ticker.lower()}.us"
    try:
        resp = _session().get(
            f"{STOOQ_BASE}/q/d/l/",
            params={"s": symbol, "i": "d"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.debug("stooq request failed for %s: %s", ticker, exc)
        return None
    if not resp.ok or not resp.text.lstrip().startswith("Date"):
        # Stooq answers unknown symbols with a short non-CSV body.
        log.debug("stooq has no history for %s", ticker)
        return None

    closes = []
    for row in csv.DictReader(io.StringIO(resp.text)):
        try:
            closes.append(float(row["Close"]))
        except (KeyError, TypeError, ValueError):
            continue  # Stooq writes "N/D" for missing sessions.
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
    closes = _coinbase_closes(ticker) if is_crypto(ticker) else _stooq_closes(ticker)
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
    return sigmas

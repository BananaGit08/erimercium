"""Inspect one ticker's raw Yahoo history to explain an implausible sigma.

MRNA came back at 23.62% daily sigma both before and after switching to
adjusted closes, so the cause needs to be identified rather than guessed at.
"""

from __future__ import annotations

import statistics
import sys
from datetime import datetime, timezone

import requests

UA = "Mozilla/5.0 (compatible; erimercium-watchlist-agent/1.0)"


def inspect(ticker: str) -> None:
    resp = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"range": "6mo", "interval": "1d"},
        headers={"User-Agent": UA},
        timeout=20,
    )
    result = resp.json()["chart"]["result"][0]
    indicators = result["indicators"]

    print(f"\n=== {ticker} ===")
    print(f"  indicators keys: {sorted(indicators.keys())}")
    has_adj = "adjclose" in indicators
    print(f"  adjclose present: {has_adj}")

    closes = indicators["quote"][0]["close"]
    adj = indicators["adjclose"][0]["adjclose"] if has_adj else None
    stamps = result["timestamp"]

    if adj:
        diffs = [
            abs(a - c) for a, c in zip(adj, closes) if a is not None and c is not None
        ]
        print(f"  max |adjclose - close|: {max(diffs):.4f}" if diffs else "  (no pairs)")

    series = [x for x in (adj if adj else closes) if x is not None]
    print(f"  observations: {len(series)}  first={series[0]:.2f}  last={series[-1]:.2f}")

    # Biggest single-day moves, with dates, from the window actually used.
    window = series[-61:]
    win_stamps = stamps[-len(window):]
    moves = []
    for i in range(1, len(window)):
        if window[i - 1]:
            pct = (window[i] - window[i - 1]) / window[i - 1] * 100
            d = datetime.fromtimestamp(win_stamps[i], tz=timezone.utc).date()
            moves.append((abs(pct), pct, d, window[i - 1], window[i]))
    moves.sort(reverse=True)

    print("  largest daily moves in the 60-day window:")
    for _, pct, d, prev, curr in moves[:5]:
        print(f"    {d}  {pct:+9.2f}%   {prev:.2f} -> {curr:.2f}")

    rets = [m[1] for m in moves]
    sigma = statistics.stdev(rets)
    trimmed = sorted(rets)[2:-2]
    mad = statistics.median([abs(r - statistics.median(rets)) for r in rets])
    print(f"  stdev            : {sigma:.2f}%")
    print(f"  stdev (trim 2ea) : {statistics.stdev(trimmed):.2f}%")
    print(f"  1.4826 x MAD     : {1.4826 * mad:.2f}%")


for t in sys.argv[1:] or ["MRNA", "SNDK", "SKHY", "SPCX", "AMZN"]:
    try:
        inspect(t)
    except Exception as exc:  # noqa: BLE001 - diagnostic
        print(f"\n=== {t} ===\n  FAILED: {type(exc).__name__}: {exc}")

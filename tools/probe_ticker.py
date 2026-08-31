"""Inspect one ticker's raw Yahoo history to explain an implausible sigma.

MRNA came back at 23.62% daily sigma both before and after switching to
adjusted closes, so the cause needs to be identified rather than guessed at.
"""

from __future__ import annotations

import statistics
import sys
import time
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


if not (len(sys.argv) > 1 and sys.argv[1] == "--concepts"):
    for t in sys.argv[1:] or ["MRNA", "SNDK", "SKHY", "SPCX", "AMZN"]:
        try:
            inspect(t)
        except Exception as exc:  # noqa: BLE001 - diagnostic
            print(f"\n=== {t} ===\n  FAILED: {type(exc).__name__}: {exc}")


# --- SEC XBRL concept probe ------------------------------------------------
SEC_UA = "erimercium-watchlist-agent christian@banananorth.com"
CONCEPT_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
    "NetIncomeLoss", "ProfitLoss", "GrossProfit", "OperatingIncomeLoss",
    "LongTermDebtNoncurrent", "LongTermDebt", "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
]


def probe_concepts(ticker: str) -> None:
    """What SEC actually returns per concept: status, count, and date span."""
    tickers = requests.get(
        "https://www.sec.gov/files/company_tickers.json",
        headers={"User-Agent": SEC_UA}, timeout=20,
    ).json()
    cik = next(
        (str(r["cik_str"]).zfill(10) for r in tickers.values()
         if str(r.get("ticker", "")).upper() == ticker.upper()),
        None,
    )
    print(f"\n=== {ticker} SEC concepts (CIK {cik}) ===")
    if not cik:
        print("  no CIK found")
        return

    for tag in CONCEPT_TAGS:
        url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
        try:
            r = requests.get(url, headers={"User-Agent": SEC_UA}, timeout=20)
        except Exception as exc:  # noqa: BLE001
            print(f"  {tag:<58} EXCEPTION {exc}")
            continue
        if r.status_code != 200:
            print(f"  {tag:<58} HTTP {r.status_code}")
            continue
        units = r.json().get("units", {}).get("USD", [])
        quarterly = []
        for e in units:
            if e.get("start") and e.get("end"):
                from datetime import date as _d
                try:
                    span = (_d.fromisoformat(e["end"]) - _d.fromisoformat(e["start"])).days
                except ValueError:
                    continue
                if 80 <= span <= 100:
                    quarterly.append(e["end"])
        ends = sorted({e["end"] for e in units if e.get("end")})
        qends = sorted(set(quarterly))
        print(f"  {tag:<58} {len(units):>4} entries, "
              f"all {ends[0] if ends else '-'}..{ends[-1] if ends else '-'}, "
              f"quarterly {len(qends)} "
              f"{qends[-1] if qends else '(none)'}")
        time.sleep(0.15)


if len(sys.argv) > 1 and sys.argv[1] == "--concepts":
    import time
    for t in sys.argv[2:] or ["PYPL"]:
        probe_concepts(t)

"""Probe candidate free sources of daily price history.

Run from CI (this sandbox has no outbound network) to find out which sources
are reachable and what they return, before committing to one.
"""

from __future__ import annotations

import requests

UA = "Mozilla/5.0 (compatible; erimercium-watchlist-agent/1.0)"
TICKERS = ["AAPL", "NVDA", "PYPL"]


def show(label: str, resp: requests.Response | None, exc: Exception | None) -> None:
    if exc is not None:
        print(f"  {label:<38} EXCEPTION {type(exc).__name__}: {exc}")
        return
    assert resp is not None
    body = resp.text[:180].replace("\n", "\\n")
    print(f"  {label:<38} HTTP {resp.status_code}  len={len(resp.text)}")
    print(f"  {'':<38} body[:180]={body!r}")


def probe(label: str, url: str, params: dict | None = None, headers: dict | None = None) -> None:
    try:
        resp = requests.get(url, params=params, headers=headers or {}, timeout=20)
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        show(label, None, exc)
    else:
        show(label, resp, None)


print("=" * 78)
print("STOOQ")
print("=" * 78)
for t in TICKERS:
    probe(f"stooq default-UA {t}", "https://stooq.com/q/d/l/", {"s": f"{t.lower()}.us", "i": "d"})
    probe(f"stooq browser-UA {t}", "https://stooq.com/q/d/l/", {"s": f"{t.lower()}.us", "i": "d"}, {"User-Agent": UA})

print()
print("=" * 78)
print("YAHOO FINANCE CHART")
print("=" * 78)
for t in TICKERS:
    probe(
        f"yahoo chart {t}",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{t}",
        {"range": "3mo", "interval": "1d"},
        {"User-Agent": UA},
    )
    probe(
        f"yahoo chart q2 {t}",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{t}",
        {"range": "3mo", "interval": "1d"},
        {"User-Agent": UA},
    )

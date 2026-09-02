"""Probe candidate free sources of earnings call transcripts and surprise data.

Run from CI: the sandbox where this code is written has no outbound network, so
reachability can only be established here. Written before the feature that
would use it, deliberately -- Stooq looked like a working price-history source
and failed for all 99 tickers from CI, and the digest silently reverted to a
flat threshold for a week. tools/probe_history.py exists because of that.

Only free, publicly available sources are probed. Motley Fool, Seeking Alpha,
Insider Monkey and similar publishers are excluded on purpose: their
transcripts sit on pages anyone can read and remain copyrighted works whose
terms forbid automated collection.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

import requests

TIMEOUT = 25
TICKERS = ["AAPL", "ADBE", "AVGO"]

FINNHUB = "https://finnhub.io/api/v1"
ALPHA = "https://www.alphavantage.co/query"
FMP = "https://financialmodelingprep.com/api/v3"
SEC_DATA = "https://data.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"

SEC_UA = "erimercium-watchlist-agent christian@banananorth.com"


def looks_like_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<head>" in head


def verdict(resp: requests.Response) -> str:
    """What this response actually is, in one phrase."""
    if not resp.ok:
        return f"HTTP {resp.status_code}"
    if looks_like_html(resp.text):
        return "HTTP 200 but an HTML page -- the paywall shape"
    try:
        payload = resp.json()
    except ValueError:
        return f"HTTP 200, non-JSON ({len(resp.text)} chars)"
    if isinstance(payload, dict):
        for key in ("Information", "Note", "Error Message", "error"):
            if key in payload:
                return f"HTTP 200 but {key}: {str(payload[key])[:140]}"
        if not payload:
            return "HTTP 200, empty object"
    if isinstance(payload, list) and not payload:
        return "HTTP 200, empty list"
    return ""


def show(label: str, resp: requests.Response | None, exc: Exception | None = None) -> dict | list | None:
    if exc is not None:
        print(f"  {label:<44} EXCEPTION {type(exc).__name__}: {exc}")
        return None
    assert resp is not None
    problem = verdict(resp)
    if problem:
        print(f"  {label:<44} {problem}")
        return None
    payload = resp.json()
    size = len(payload) if isinstance(payload, (list, dict)) else 0
    print(f"  {label:<44} OK  json, {size} top-level entries, {len(resp.text)} chars")
    return payload


def get(label: str, url: str, params: dict | None = None, headers: dict | None = None):
    try:
        resp = requests.get(url, params=params, headers=headers or {}, timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        return show(label, None, exc)
    return show(label, resp)


def words(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


# --- 1 & 4. Finnhub -------------------------------------------------------


def probe_finnhub(ticker: str) -> None:
    token = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not token:
        print("  FINNHUB_API_KEY not set -- skipping Finnhub")
        return

    listing = get(
        "finnhub stock/transcripts/list",
        f"{FINNHUB}/stock/transcripts/list",
        {"symbol": ticker, "token": token},
    )
    transcript_id = ""
    if isinstance(listing, dict):
        entries = listing.get("transcripts") or []
        print(f"      -> {len(entries)} transcript(s) listed")
        if entries:
            transcript_id = str(entries[0].get("id", ""))
            print(f"      -> newest: {json.dumps(entries[0])[:200]}")

    if transcript_id:
        body = get(
            "finnhub stock/transcripts (full body)",
            f"{FINNHUB}/stock/transcripts",
            {"id": transcript_id, "token": token},
        )
        if isinstance(body, dict):
            speech = body.get("transcript") or []
            total = sum(words(" ".join(s.get("speech", []))) for s in speech if isinstance(s, dict))
            speakers = {s.get("name", "") for s in speech if isinstance(s, dict)}
            print(f"      -> {len(speech)} segments, ~{total} words, {len(speakers)} speakers")
            print("      -> Q&A separable: yes (per-speaker segments)" if len(speakers) > 3
                  else "      -> Q&A separable: unclear")

    # Part A needs this one, and it is cheap to settle here.
    surprises = get(
        "finnhub stock/earnings (Part A surprises)",
        f"{FINNHUB}/stock/earnings",
        {"symbol": ticker, "token": token},
    )
    if isinstance(surprises, list) and surprises:
        print(f"      -> {len(surprises)} quarters; newest: {json.dumps(surprises[0])[:200]}")


# --- 2. Alpha Vantage -----------------------------------------------------


# Alpha Vantage's free tier throttles at one request per second and answers a
# breach with a 200 and an advisory string rather than a 429 -- which reads
# exactly like a paywall unless the calls are spaced. The first run with a real
# key was thrown away to this.
ALPHA_PACE_SECONDS = 1.5


def probe_alpha(ticker: str) -> None:
    key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip() or "demo"
    note = "" if key != "demo" else "  (no key set -- using 'demo', which only serves fixed symbols)"
    print(f"  alpha vantage key: {'set' if key != 'demo' else 'demo'}{note}")
    time.sleep(ALPHA_PACE_SECONDS)
    payload = get(
        "alphavantage EARNINGS_CALL_TRANSCRIPT",
        ALPHA,
        {"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": ticker,
         "quarter": os.environ.get("ALPHA_QUARTER", "2026Q1"), "apikey": key},
    )
    if isinstance(payload, dict):
        speech = payload.get("transcript") or []
        total = sum(words(s.get("content", "")) for s in speech if isinstance(s, dict))
        print(f"      -> {len(speech)} segments, ~{total} words")
        if speech:
            print(f"      -> first segment keys: {sorted(speech[0])}")

    time.sleep(ALPHA_PACE_SECONDS)
    get("alphavantage EARNINGS (surprise history)", ALPHA,
        {"function": "EARNINGS", "symbol": ticker, "apikey": key})


# --- 3. Financial Modeling Prep -------------------------------------------


def probe_fmp(ticker: str) -> None:
    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        print("  FMP_API_KEY not set -- reporting reachability only, expect 401")
    payload = get(
        "fmp earning_call_transcript",
        f"{FMP}/earning_call_transcript/{ticker}",
        {"quarter": 1, "year": 2026, "apikey": key or "none"},
    )
    if isinstance(payload, list) and payload:
        content = payload[0].get("content", "") if isinstance(payload[0], dict) else ""
        print(f"      -> ~{words(content)} words")


# --- 5. SEC 8-K Item 2.02 exhibits ----------------------------------------


def probe_sec(ticker: str) -> None:
    """The floor: free, already integrated, and it always exists."""
    session = requests.Session()
    session.headers.update({"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})

    try:
        cik_map = session.get(SEC_TICKERS, timeout=TIMEOUT).json()
    except Exception as exc:  # noqa: BLE001
        print(f"  sec company_tickers.json                     EXCEPTION {exc}")
        return
    cik = next(
        (str(row["cik_str"]).zfill(10) for row in cik_map.values()
         if str(row.get("ticker", "")).upper() == ticker),
        "",
    )
    if not cik:
        print(f"  sec: no CIK for {ticker}")
        return

    try:
        subs = session.get(f"{SEC_DATA}/submissions/CIK{cik}.json", timeout=TIMEOUT).json()
    except Exception as exc:  # noqa: BLE001
        print(f"  sec submissions                              EXCEPTION {exc}")
        return

    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    items = recent.get("items", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])

    for i, form in enumerate(forms):
        if form != "8-K" or "2.02" not in (items[i] if i < len(items) else ""):
            continue
        accession = accessions[i].replace("-", "")
        print(f"  sec 8-K Item 2.02 on {dates[i]}  accession {accessions[i]}")
        index = session.get(
            f"{SEC_ARCHIVES}/{int(cik)}/{accession}/index.json", timeout=TIMEOUT
        )
        if not index.ok:
            print(f"      -> index.json HTTP {index.status_code}")
            return
        names = [
            item["name"] for item in index.json()["directory"]["item"]
            if item["name"].lower().endswith((".htm", ".html", ".txt"))
        ]
        exhibits = [n for n in names if "ex99" in n.lower().replace("-", "").replace("_", "")]
        print(f"      -> {len(names)} documents, {len(exhibits)} look like EX-99: {exhibits[:4]}")
        for name in (exhibits or names)[:1]:
            doc = session.get(f"{SEC_ARCHIVES}/{int(cik)}/{accession}/{name}", timeout=TIMEOUT)
            text = re.sub(r"<[^>]+>", " ", doc.text)
            body = re.sub(r"\s+", " ", text).strip()
            print(f"      -> {name}: HTTP {doc.status_code}, ~{words(body)} words")
            print(f"      -> opens: {body[:220]!r}")
            lowered = body.lower()
            kind = (
                "full transcript" if "question-and-answer" in lowered or "operator" in lowered
                else "prepared remarks" if "prepared remarks" in lowered
                else "press release"
            )
            print(f"      -> reads as: {kind}")
        return
    print("  sec: no 8-K Item 2.02 in the recent filings window")


def main() -> int:
    tickers = sys.argv[1:] or TICKERS
    for ticker in tickers:
        print(f"\n{'=' * 72}\n{ticker}\n{'=' * 72}")
        print("\n-- Finnhub (transcripts + Part A surprises) --")
        probe_finnhub(ticker)
        print("\n-- Alpha Vantage --")
        probe_alpha(ticker)
        print("\n-- Financial Modeling Prep --")
        probe_fmp(ticker)
        print("\n-- SEC 8-K Item 2.02 --")
        probe_sec(ticker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

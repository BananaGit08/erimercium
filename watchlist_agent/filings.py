"""Recent SEC filings for a ticker.

Uses EDGAR's submissions API rather than full-text search. Full-text search
answers "which filings contain this phrase", so it needs a search term and
cannot enumerate "every filing this company made recently" -- which is the
actual question here. The submissions feed lists each filing with its form
type and, for 8-Ks, its item codes, which is exactly what the materiality
check needs and is authoritative rather than inferred.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta

import requests

from .config import (
    FILINGS_LOOKBACK_DAYS,
    HTTP_TIMEOUT_SECONDS,
    SEC_ARCHIVES_BASE,
    SEC_DATA_BASE,
    SEC_MAX_RPS,
    SEC_TICKERS_URL,
    sec_user_agent,
)
from .materiality import Bullet, describe_filing

log = logging.getLogger(__name__)

_cik_lock = threading.Lock()
_cik_cache: dict[str, tuple[str, str]] | None = None
_last_request = 0.0


def _throttle() -> None:
    """SEC asks for at most 10 requests/second across all endpoints."""
    global _last_request
    interval = 1.0 / SEC_MAX_RPS
    elapsed = time.monotonic() - _last_request
    if elapsed < interval:
        time.sleep(interval - elapsed)
    _last_request = time.monotonic()


def sec_session() -> requests.Session:
    session = requests.Session()
    # EDGAR rejects requests without a descriptive User-Agent naming a contact.
    session.headers.update(
        {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
    )
    return session


def _load_cik_map(session: requests.Session) -> dict[str, tuple[str, str]]:
    """Ticker -> (zero-padded CIK, company name), fetched once per run."""
    global _cik_cache
    with _cik_lock:
        if _cik_cache is not None:
            return _cik_cache
        mapping: dict[str, tuple[str, str]] = {}
        try:
            _throttle()
            resp = session.get(SEC_TICKERS_URL, timeout=HTTP_TIMEOUT_SECONDS)
            if resp.ok:
                for row in resp.json().values():
                    ticker = str(row.get("ticker", "")).upper()
                    if ticker:
                        mapping[ticker] = (
                            str(row["cik_str"]).zfill(10),
                            str(row.get("title", "")),
                        )
        except (requests.RequestException, ValueError, KeyError, AttributeError) as exc:
            log.warning("could not load SEC ticker map: %s", exc)
        _cik_cache = mapping
        log.info("SEC ticker map: %d symbols", len(mapping))
        return mapping


def fetch_filings(session: requests.Session, ticker: str) -> list[Bullet]:
    """Material 10-K / 10-Q / 8-K filings for one ticker in the lookback window."""
    entry = _load_cik_map(session).get(ticker.upper())
    if not entry:
        # Foreign private issuers, ETFs and OTC symbols are frequently absent.
        log.info("%s: no CIK in EDGAR (foreign issuer, ETF or OTC)", ticker)
        return []
    cik, _ = entry

    try:
        _throttle()
        resp = session.get(
            f"{SEC_DATA_BASE}/submissions/CIK{cik}.json", timeout=HTTP_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        log.warning("EDGAR request failed for %s: %s", ticker, exc)
        return []
    if not resp.ok:
        log.warning("EDGAR HTTP %s for %s", resp.status_code, ticker)
        return []
    try:
        recent = resp.json()["filings"]["recent"]
    except (ValueError, KeyError, TypeError):
        log.warning("EDGAR payload unusable for %s", ticker)
        return []

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    items = recent.get("items", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    cutoff = date.today() - timedelta(days=FILINGS_LOOKBACK_DAYS)

    bullets: list[Bullet] = []
    for i, form in enumerate(forms):
        try:
            filed = datetime.strptime(dates[i], "%Y-%m-%d").date()
        except (IndexError, ValueError):
            continue
        if filed < cutoff:
            # The feed is newest-first, so nothing later can be in range.
            break
        description = describe_filing(form, items[i] if i < len(items) else "")
        if not description:
            continue

        url = ""
        if i < len(accessions) and i < len(docs) and docs[i]:
            plain = accessions[i].replace("-", "")
            url = f"{SEC_ARCHIVES_BASE}/{int(cik)}/{plain}/{docs[i]}"

        bullets.append(
            Bullet(
                text=f"{form} filed {filed:%b %d} — {description}",
                url=url,
                # Filings are ranked above news of equal score by Bullet.sort_key;
                # an 8-K with named items outranks a bare one.
                score=6 if form in ("10-K", "10-Q") else 5,
                kind="filing",
            )
        )

    if bullets:
        log.info("%s: %d material filings", ticker, len(bullets))
    return bullets


def company_name(session: requests.Session, ticker: str) -> str:
    """Registered company name for a ticker, or empty if EDGAR does not list it."""
    entry = _load_cik_map(session).get(ticker.upper())
    return entry[1] if entry else ""

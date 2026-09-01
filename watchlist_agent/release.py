"""The earnings press release, from the 8-K the company files with the SEC.

This is the source of record for every reported figure. The reader described
the division himself: *"the press release is the official statement which they
read on the earnings call, but then after the CEO/CFO finishes reading the
press release, they do a live Q&A which then adds important color."* So this
module and transcripts.py are not alternatives -- they supply different things,
and a take-aways report uses both.

Why the numbers come from here rather than from the transcript: the release is
filed by the company within minutes of the results, in its own words, with the
tables attached. Auto-generated transcripts mangle numerals and names, and a
wrong EPS figure in a summary is worse than no summary. Confirmed by probe --
Apple's Q3 exhibit came back at ~2,050 words and Adobe's at ~3,810, both
plainly the release.

Item 2.02 is "Results of Operations and Financial Condition". Filing it is
mandatory when results are announced, so unlike a transcript it always exists
for a US filer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape

import requests

from .config import HTTP_TIMEOUT_SECONDS, SEC_ARCHIVES_BASE, SEC_DATA_BASE
from .filings import _load_cik_map, throttle

log = logging.getLogger(__name__)

RESULTS_ITEM = "2.02"

# A release runs a couple of thousand words. Anything much shorter is a cover
# page or a bare exhibit index rather than the statement itself.
MIN_RELEASE_WORDS = 200


@dataclass(frozen=True)
class EarningsRelease:
    ticker: str
    filed: date
    accession: str
    url: str
    text: str

    @property
    def words(self) -> int:
        return len(re.findall(r"\w+", self.text))


def to_text(markup: str) -> str:
    """Readable text from an EDGAR exhibit, which is HTML more often than not."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</t[dh]\s*>", "  ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    # Collapse runs of spaces but keep paragraph breaks: the financial tables
    # in a release are only readable if their line structure survives.
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _looks_like_exhibit(name: str) -> bool:
    flattened = name.lower().replace("-", "").replace("_", "")
    return "ex99" in flattened and name.lower().endswith((".htm", ".html", ".txt"))


def fetch(
    session: requests.Session, ticker: str, since: date | None = None
) -> EarningsRelease | None:
    """The most recent Item 2.02 release for this ticker, if there is one.

    ``since`` bounds how far back to look, so a stale release from last quarter
    cannot be attached to this quarter's report.
    """
    entry = _load_cik_map(session).get(ticker.upper())
    if not entry:
        log.info("%s: no CIK in EDGAR — no earnings release to fetch", ticker)
        return None
    cik, _ = entry
    cutoff = since or (date.today() - timedelta(days=14))

    try:
        throttle()
        resp = session.get(
            f"{SEC_DATA_BASE}/submissions/CIK{cik}.json", timeout=HTTP_TIMEOUT_SECONDS
        )
        recent = resp.json()["filings"]["recent"]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.warning("%s: EDGAR submissions unusable: %s", ticker, exc)
        return None

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    items = recent.get("items", [])
    accessions = recent.get("accessionNumber", [])

    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        if RESULTS_ITEM not in (items[i] if i < len(items) else ""):
            continue
        try:
            filed = datetime.strptime(dates[i], "%Y-%m-%d").date()
        except (IndexError, ValueError):
            continue
        if filed < cutoff:
            # Newest-first, so nothing further down can be in range either.
            break

        accession = accessions[i]
        plain = accession.replace("-", "")
        base = f"{SEC_ARCHIVES_BASE}/{int(cik)}/{plain}"
        try:
            throttle()
            index = session.get(f"{base}/index.json", timeout=HTTP_TIMEOUT_SECONDS)
            names = [item["name"] for item in index.json()["directory"]["item"]]
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            log.warning("%s: filing index unusable for %s: %s", ticker, accession, exc)
            return None

        # EX-99.1 is where the release lives by convention; fall back to the
        # largest readable document rather than giving up on a filer who names
        # their exhibits differently.
        candidates = [n for n in names if _looks_like_exhibit(n)] or [
            n for n in names if n.lower().endswith((".htm", ".html"))
        ]
        for name in candidates[:2]:
            try:
                throttle()
                doc = session.get(f"{base}/{name}", timeout=HTTP_TIMEOUT_SECONDS)
            except requests.RequestException as exc:
                log.warning("%s: exhibit %s failed: %s", ticker, name, exc)
                continue
            if not doc.ok:
                continue
            text = to_text(doc.text)
            if len(re.findall(r"\w+", text)) < MIN_RELEASE_WORDS:
                continue
            release = EarningsRelease(
                ticker=ticker, filed=filed, accession=accession,
                url=f"{base}/{name}", text=text,
            )
            log.info(
                "%s earnings release: %s filed %s, %d words",
                ticker, accession, filed, release.words,
            )
            return release
        log.info("%s: 8-K %s had no readable release exhibit", ticker, accession)
        return None

    log.info("%s: no Item 2.02 filing since %s", ticker, cutoff)
    return None

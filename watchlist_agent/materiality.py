"""Deciding which news and filings are worth reading.

This is the keyword-based filter. It is deliberately conservative about what it
throws away: the cost of dropping a real story is much higher than the cost of
letting one extra headline through, so the noise list targets formats that are
never material (listicles, "here's why the stock is moving" reaction pieces,
generic market wraps) rather than trying to judge substance.

Filings are not keyword-filtered at all. Form type and 8-K item code say
exactly what a filing is, which is far more reliable than reading its title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Headline shapes that are never news about the company itself.
NOISE_PATTERNS = [
    r"\b\d+\s+(?:stocks?|reasons?|things?|ways?|picks?)\b",
    r"\bstocks? to (?:watch|buy|sell)\b",
    r"\b(?:should|why) you (?:buy|sell|own)\b",
    r"\bhere'?s why\b",
    r"\bwhy .{1,40}\b(?:stock|shares?)\b.{0,20}\b(?:is|are|was|were)\b",
    r"\b(?:stock|shares?)\b.{0,30}\b(?:moving|surging|plunging|soaring|sinking|tumbling|rallying)\b.{0,15}\btoday\b",
    r"\bwhat you need to know\b",
    r"\b(?:jim )?cramer\b",
    r"\bmotley fool\b",
    r"\bzacks\b",
    r"\bmarket (?:wrap|recap|close|open|movers)\b",
    r"\b(?:pre|post)[- ]?market (?:movers|gainers|losers)\b",
    r"\btrending (?:stocks?|tickers?)\b",
    r"\b(?:gainers?|losers?) (?:of|for) the (?:day|week)\b",
    r"\bis .+ a (?:buy|sell|good stock)\b",
    r"\b(?:52[- ]week|all[- ]time) (?:high|low)\b",
    r"\bunusual options activity\b",
    r"\bshares? (?:rise|fall|jump|slip|climb|drop)\b.*\bafter\b.*\banalyst\b",
    r"\bhow to (?:trade|play)\b",
    r"\bearnings preview\b",
    r"\bwhat analysts are saying\b",
]

# Subjects that are worth knowing about, roughly ordered by how much they move
# the underlying business rather than the narrative around it.
MATERIAL_KEYWORDS = {
    # Corporate actions and structure
    "acquisition": 5, "acquires": 5, "merger": 5, "to acquire": 5,
    "takeover": 5, "spin-off": 4, "spinoff": 4, "divest": 4,
    "bankruptcy": 5, "chapter 11": 5, "restructuring": 4,
    # Leadership
    "ceo": 4, "chief executive": 4, "cfo": 4, "chief financial": 4,
    "steps down": 5, "resigns": 5, "resignation": 5, "ousted": 5,
    "appoints": 3, "names new": 3, "succeeds": 3,
    # Results and outlook
    "guidance": 4, "outlook": 3, "forecast": 3, "raises": 3, "cuts": 3,
    "beats": 3, "misses": 3, "warns": 4, "profit warning": 5,
    "quarterly results": 3, "earnings": 3, "revenue": 3,
    # Legal and regulatory
    "lawsuit": 4, "sues": 4, "settlement": 4, "investigation": 5,
    "subpoena": 5, "sec charges": 5, "doj": 4, "antitrust": 4,
    "fda approval": 5, "fda rejects": 5, "recall": 4, "clinical trial": 4,
    "data breach": 5, "cyberattack": 4, "hack": 4,
    # Operations and capital
    "layoffs": 4, "job cuts": 4, "plant": 2, "contract": 3, "deal": 3,
    "partnership": 3, "buyback": 3, "dividend": 3, "offering": 3,
    "short seller": 5, "short report": 5, "delisting": 5,
}

# 8-K item codes that actually say something. Everything else is noise: Reg FD
# disclosures, routine shareholder votes, and the like.
MATERIAL_8K_ITEMS = {
    "1.01": "entered a material definitive agreement",
    "1.02": "terminated a material definitive agreement",
    "1.03": "entered bankruptcy or receivership",
    "2.01": "completed an acquisition or disposition of assets",
    "2.02": "reported results of operations",
    "2.03": "took on a material financial obligation",
    "2.04": "triggered acceleration of a financial obligation",
    "2.05": "committed to exit or disposal costs",
    "2.06": "recorded a material impairment",
    "3.01": "received a delisting or listing-rule notice",
    "4.01": "changed its accountant",
    "4.02": "said previously issued financials cannot be relied upon",
    "5.01": "had a change in control",
    "5.02": "had a director or officer departure or appointment",
    "5.03": "amended its charter or bylaws",
}

FORM_DESCRIPTIONS = {
    "10-K": "annual report",
    "10-Q": "quarterly report",
    "8-K": "material event",
}

_noise_re = re.compile("|".join(NOISE_PATTERNS), re.IGNORECASE)

# Words that do not distinguish one company from another.
_NAME_SUFFIXES = {
    "inc", "inc.", "corp", "corp.", "corporation", "co", "co.", "company",
    "ltd", "ltd.", "limited", "plc", "holdings", "holding", "group",
    "technologies", "technology", "tech", "international", "industries",
    "systems", "solutions", "enterprises", "partners", "the", "n.v.", "s.a.",
    "ag", "nv", "sa", "class", "common", "stock", "shares", "&",
}


def company_keyword(name: str) -> str:
    """The distinctive part of a company name, for matching against headlines."""
    for token in (name or "").replace(",", " ").split():
        cleaned = token.strip().lower()
        if cleaned and cleaned not in _NAME_SUFFIXES and len(cleaned) >= 3:
            # "Amazon.com" -> "amazon"; keep the leading segment only.
            return cleaned.split(".")[0]
    return ""


def is_about_company(headline: str, ticker: str, company_name: str = "") -> bool:
    """Whether a headline is actually about this company.

    Finnhub's company-news feed returns anything that *mentions* the symbol,
    which includes wire roundups covering a dozen tickers and macro commentary.
    Those score highly on materiality keywords while being about someone else
    entirely -- a real run attributed a Rivian CFO resignation to AMZN and a
    QFIN earnings miss to IREN. Requiring the company to appear in the headline
    is what makes the materiality score mean anything.
    """
    if not headline:
        return False

    # Ticker match is case-sensitive and length-limited on purpose: short
    # symbols like F, Q, ON, BE and GE are ordinary English words, and a
    # case-insensitive match on them would accept nearly every headline.
    if len(ticker) >= 3 and re.search(rf"\b{re.escape(ticker)}\b", headline):
        return True

    keyword = company_keyword(company_name)
    return bool(keyword) and keyword in headline.lower()


@dataclass
class Bullet:
    """One line worth putting in the digest."""

    text: str
    url: str
    score: int
    kind: str  # "filing" or "news"

    @property
    def sort_key(self) -> tuple[int, int]:
        # Filings outrank news at equal score: a filing is the company speaking.
        return (1 if self.kind == "filing" else 0, self.score)


def is_noise(headline: str) -> bool:
    return bool(_noise_re.search(headline or ""))


def score_headline(headline: str) -> int:
    """How material a headline looks. Zero means nothing recognisable."""
    text = (headline or "").lower()
    return sum(weight for term, weight in MATERIAL_KEYWORDS.items() if term in text)


def describe_filing(form: str, items: str = "") -> str | None:
    """Plain-language description of a filing, or None if it is routine.

    10-K and 10-Q are always worth listing. An 8-K is only as interesting as
    its item codes, which is why they are checked rather than the title.
    """
    form = (form or "").upper().strip()
    if form in ("10-K", "10-Q"):
        return FORM_DESCRIPTIONS[form]
    if form != "8-K":
        return None

    codes = [c.strip() for c in (items or "").split(",") if c.strip()]
    described = [MATERIAL_8K_ITEMS[c] for c in codes if c in MATERIAL_8K_ITEMS]
    if described:
        return "; ".join(dict.fromkeys(described))
    if not codes:
        # No item codes supplied: report it rather than guess it is routine.
        return "material event (items not specified)"
    return None

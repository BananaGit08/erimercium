"""Company news from Finnhub, filtered down to what is worth reading."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import requests

from .config import (
    FINNHUB_BASE,
    HTTP_TIMEOUT_SECONDS,
    NEWS_LOOKBACK_DAYS,
    finnhub_api_key,
)
from .materiality import Bullet, is_about_company, is_noise, is_subject, score_headline

log = logging.getLogger(__name__)


def fetch_news(
    session: requests.Session,
    ticker: str,
    company: str = "",
    days: int = NEWS_LOOKBACK_DAYS,
    min_score: int = 1,
) -> list[Bullet]:
    """Recent company news for one ticker, noise removed and scored.

    `days` and `min_score` are widened for research reports: a digest wants
    only what interrupts your day, but a report benefits from context that
    would not clear that bar on its own.
    """
    today = date.today()
    try:
        resp = session.get(
            f"{FINNHUB_BASE}/company-news",
            params={
                "symbol": ticker,
                "from": (today - timedelta(days=days)).isoformat(),
                "to": today.isoformat(),
                "token": finnhub_api_key(),
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.warning("news request failed for %s: %s", ticker, exc)
        return []
    if not resp.ok:
        log.warning("news HTTP %s for %s", resp.status_code, ticker)
        return []
    try:
        articles = resp.json()
    except ValueError:
        return []
    if not isinstance(articles, list):
        return []

    bullets: list[Bullet] = []
    seen: set[str] = set()
    for article in articles:
        headline = (article.get("headline") or "").strip()
        if not headline or is_noise(headline):
            continue
        if not is_about_company(headline, ticker, company):
            # Mentions the symbol but is about someone else -- a wire roundup
            # or macro piece. These score highly and are entirely misleading.
            continue
        # Wire stories are syndicated verbatim across outlets; keep the first.
        key = headline.lower()[:70]
        if key in seen:
            continue
        score = score_headline(headline)
        if score < min_score:
            continue
        if not is_subject(headline, ticker, company):
            # Named as a participant in someone else's story. Keep it, but let
            # anything the company actually did outrank it.
            score = max(1, score // 3)
        seen.add(key)
        source = (article.get("source") or "").strip()
        text = f"{headline} ({source})" if source else headline
        bullets.append(
            Bullet(text=text, url=article.get("url") or "", score=score, kind="news")
        )

    log.info(
        "%s: %d material headlines from %d articles", ticker, len(bullets), len(articles)
    )
    return bullets

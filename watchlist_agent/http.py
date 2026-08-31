"""Shared HTTP fetching with retry on transient failures.

The first scheduled research run wrote three of its four reports on degraded
data: Finnhub answered 503 in bursts, and AAPL's baseline went out with zero
news and zero valuation metrics because a single failed request was treated as
a final answer. Quotes survived the same storm, because prices.py has retried
since Stage 1 -- the other three Finnhub callers simply never got the same
treatment.

A 503 is transient by definition. A 403 (a premium endpoint on a free key) and
a 404 are not, and retrying those only wastes the run's time, so only 429 and
5xx are retried.
"""

from __future__ import annotations

import logging
import time

import requests

from .config import HTTP_MAX_RETRIES, HTTP_TIMEOUT_SECONDS

log = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def get_json(
    session: requests.Session,
    url: str,
    *,
    label: str = "",
    status: dict | None = None,
    **kwargs,
) -> dict | list | None:
    """GET and parse JSON, retrying transient failures.

    ``status`` collects why a call came back empty. A bare None cannot
    distinguish "the company has no news" from "the request was refused", and
    that ambiguity has produced a wrong report at least twice, so the reason
    travels with the gap rather than living only in a log line.
    """
    label = label or url
    last = ""

    for attempt in range(HTTP_MAX_RETRIES):
        try:
            resp = session.get(url, timeout=HTTP_TIMEOUT_SECONDS, **kwargs)
        except requests.RequestException as exc:
            last = f"request failed: {exc}"
            log.warning("%s %s", label, last)
        else:
            if resp.ok:
                try:
                    return resp.json()
                except ValueError:
                    # A 200 whose body is not JSON -- Finnhub serves its
                    # paywall page this way. Not retryable, and worth naming:
                    # this path once produced "no analyst ratings" for two of
                    # the most heavily covered stocks on the market.
                    body = resp.text[:160].replace("\n", " ")
                    last = f"200 but non-JSON body: {body!r}"
                    log.warning("%s %s", label, last)
                    if status is not None:
                        status["note"] = last
                    return None

            last = f"HTTP {resp.status_code} {resp.text[:120]}".strip()
            if resp.status_code not in RETRYABLE_STATUSES:
                # 403 is a premium endpoint on a free key, 404 a bad symbol.
                # Neither improves on a second attempt.
                log.warning("%s %s", label, last)
                if status is not None:
                    status["note"] = last
                return None
            log.warning("%s %s (attempt %d/%d)",
                        label, last, attempt + 1, HTTP_MAX_RETRIES)

        if attempt + 1 < HTTP_MAX_RETRIES:
            time.sleep(2 ** attempt)

    if status is not None:
        status["note"] = f"{last} (gave up after {HTTP_MAX_RETRIES} attempts)"
    return None

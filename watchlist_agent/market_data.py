"""Valuation, peers and analyst sentiment from Finnhub's free endpoints."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests

from .config import FINNHUB_BASE, HTTP_TIMEOUT_SECONDS, finnhub_api_key

log = logging.getLogger(__name__)

# Metrics worth putting in front of a reader, and what to call them.
METRIC_LABELS = {
    "peTTM": "P/E (TTM)",
    "psTTM": "P/S (TTM)",
    "pbQuarterly": "P/B",
    "epsGrowthTTMYoy": "EPS growth YoY %",
    "revenueGrowthTTMYoy": "Revenue growth YoY %",
    "grossMarginTTM": "Gross margin %",
    "operatingMarginTTM": "Operating margin %",
    "netProfitMarginTTM": "Net margin %",
    "totalDebtToEquityQuarterly": "Debt/equity",
    "currentRatioQuarterly": "Current ratio",
    "52WeekHigh": "52-week high",
    "52WeekLow": "52-week low",
    "beta": "Beta",
}


@dataclass
class Recommendations:
    """Analyst ratings over recent months, newest first."""

    months: list[dict] = field(default_factory=list)

    @property
    def latest(self) -> dict | None:
        return self.months[0] if self.months else None

    def direction(self) -> str | None:
        """Whether sentiment is improving or deteriorating.

        Compares a bull-minus-bear score across the two most recent months.
        Price-target movement would be the better signal, but it is a premium
        endpoint, so the rating mix is what is available.
        """
        if len(self.months) < 2:
            return None

        def score(m: dict) -> int:
            return (
                2 * int(m.get("strongBuy") or 0)
                + int(m.get("buy") or 0)
                - int(m.get("sell") or 0)
                - 2 * int(m.get("strongSell") or 0)
            )

        now, prior = score(self.months[0]), score(self.months[1])
        if now == prior:
            return "unchanged"
        return "improving" if now > prior else "deteriorating"

    def summary(self) -> str | None:
        latest = self.latest
        if not latest:
            return None
        parts = [
            f"{latest.get(k) or 0} {label}"
            for k, label in (
                ("strongBuy", "strong buy"),
                ("buy", "buy"),
                ("hold", "hold"),
                ("sell", "sell"),
                ("strongSell", "strong sell"),
            )
            if latest.get(k)
        ]
        text = ", ".join(parts) if parts else "no ratings"
        drift = self.direction()
        return f"{text} ({drift})" if drift else text


@dataclass
class MarketData:
    ticker: str
    metrics: dict[str, float] = field(default_factory=dict)
    peers: list[str] = field(default_factory=list)
    recommendations: Recommendations = field(default_factory=Recommendations)
    ratings_note: str = ""  # why ratings are missing, when they are

    def labelled_metrics(self) -> dict[str, float]:
        return {
            label: self.metrics[key]
            for key, label in METRIC_LABELS.items()
            if self.metrics.get(key) is not None
        }


def _get(session: requests.Session, path: str, status: dict | None = None, **params):
    params["token"] = finnhub_api_key()
    try:
        resp = session.get(
            f"{FINNHUB_BASE}/{path}", params=params, timeout=HTTP_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        log.warning("finnhub %s failed: %s", path, exc)
        if status is not None:
            status["note"] = f"request failed: {exc}"
        return None
    if not resp.ok:
        # 403 here means the endpoint is premium on this key.
        log.warning("finnhub %s HTTP %s: %s", path, resp.status_code, resp.text[:160])
        if status is not None:
            status["note"] = f"HTTP {resp.status_code} {resp.text[:120]}"
        return None
    try:
        return resp.json()
    except ValueError:
        # A 200 whose body is not JSON. This is the path that silently
        # produced "no analyst ratings" for PYPL and NKE, so record what
        # actually came back rather than returning a bare None.
        body = resp.text[:160].replace("\n", " ")
        log.warning("finnhub %s returned non-JSON: %r", path, body)
        if status is not None:
            status["note"] = f"200 but non-JSON body: {body!r}"
        return None


def fetch(session: requests.Session, ticker: str) -> MarketData:
    data = MarketData(ticker=ticker)

    basic = _get(session, "stock/metric", symbol=ticker, metric="all")
    if isinstance(basic, dict):
        raw = basic.get("metric") or {}
        data.metrics = {k: v for k, v in raw.items() if isinstance(v, (int, float))}

    peers = _get(session, "stock/peers", symbol=ticker)
    if isinstance(peers, list):
        # Finnhub includes the subject company in its own peer list.
        data.peers = [p for p in peers if p and p.upper() != ticker.upper()][:6]

    status: dict = {}
    trends = _get(session, "stock/recommendation-trends", status=status, symbol=ticker)
    if not (isinstance(trends, list) and trends):
        data.ratings_note = status.get("note") or (
            f"endpoint returned {type(trends).__name__} {str(trends)[:120]}"
        )
        log.warning("%s ratings unavailable: %s", ticker, data.ratings_note)
    if isinstance(trends, list) and trends:
        data.recommendations = Recommendations(
            months=sorted(trends, key=lambda m: m.get("period", ""), reverse=True)[:4]
        )

    log.info(
        "%s market data: %d metrics, %d peers, %d rating months",
        ticker,
        len(data.metrics),
        len(data.peers),
        len(data.recommendations.months),
    )
    return data


def peer_comparison(
    session: requests.Session, peers: list[str], metric: str = "peTTM"
) -> dict[str, float]:
    """One metric across peer companies, for valuation context."""
    values: dict[str, float] = {}
    for peer in peers:
        payload = _get(session, "stock/metric", symbol=peer, metric="all")
        if isinstance(payload, dict):
            value = (payload.get("metric") or {}).get(metric)
            if isinstance(value, (int, float)):
                values[peer] = float(value)
    return values

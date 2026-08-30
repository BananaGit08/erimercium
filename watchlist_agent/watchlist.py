"""Reading and mutating watchlist.json."""

from __future__ import annotations

import json
from pathlib import Path

from .config import (
    DEFAULT_ALWAYS_FLAG_ABS_PCT,
    DEFAULT_MAX_MOVERS_SHOWN,
    DEFAULT_MIN_ABS_PCT,
    DEFAULT_MOVE_THRESHOLD_PCT,
    DEFAULT_Z_SCORE,
    Thresholds,
)

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"


class Watchlist:
    def __init__(self, path: Path = WATCHLIST_PATH) -> None:
        self.path = path
        self._doc = json.loads(path.read_text())

    @property
    def tickers(self) -> list[str]:
        return list(self._doc.get("tickers", []))

    @property
    def thresholds(self) -> Thresholds:
        cfg = self._doc.get("thresholds", {})
        return Thresholds(
            z_score=float(cfg.get("z_score", DEFAULT_Z_SCORE)),
            min_abs_pct=float(cfg.get("min_abs_pct", DEFAULT_MIN_ABS_PCT)),
            always_flag_abs_pct=float(
                cfg.get("always_flag_abs_pct", DEFAULT_ALWAYS_FLAG_ABS_PCT)
            ),
            fallback_pct=float(cfg.get("fallback_pct", DEFAULT_MOVE_THRESHOLD_PCT)),
            max_shown=int(cfg.get("max_shown", DEFAULT_MAX_MOVERS_SHOWN)),
        )

    def add(self, ticker: str) -> bool:
        """Add a ticker. Returns True if it was actually added."""
        ticker = ticker.strip().upper()
        if not ticker or ticker in self._doc["tickers"]:
            return False
        self._doc["tickers"] = sorted(set(self._doc["tickers"]) | {ticker})
        self.save()
        return True

    def remove(self, ticker: str) -> bool:
        """Remove a ticker. Returns True if it was actually removed."""
        ticker = ticker.strip().upper()
        if ticker not in self._doc.get("tickers", []):
            return False
        self._doc["tickers"] = [t for t in self._doc["tickers"] if t != ticker]
        self.save()
        return True

    def save(self) -> None:
        self.path.write_text(json.dumps(self._doc, indent=2) + "\n")


def is_crypto(ticker: str) -> bool:
    """Crypto pairs are written in Coinbase product-id form, e.g. BTC-USD."""
    return ticker.upper().endswith("-USD")

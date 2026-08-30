"""What research has already been sent.

Committed back to the repository after each run so the schedule survives across
GitHub Actions runs, which share no storage.

Every entry is keyed by something that does not move:

  * baseline  -- one per ticker, ever
  * earnings  -- keyed by fiscal period ("2026Q3"), not by date. Companies
                 reschedule earnings routinely; keying on the date would send a
                 second report for the same quarter whenever one moved.
  * events    -- keyed by SEC accession number, which is unique per filing.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "reports_sent.json"


class ReportState:
    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        if path.exists():
            self._doc = json.loads(path.read_text())
        else:
            self._doc = {
                "_comment": (
                    "Research reports already sent. Earnings reports are keyed by "
                    "fiscal period so a rescheduled date does not cause a repeat; "
                    "event reports are keyed by SEC accession number."
                ),
                "baseline": {},
                "earnings": {},
                "events": {},
            }
        self._dirty = False

    # --- baseline ---------------------------------------------------------
    def has_baseline(self, ticker: str) -> bool:
        return ticker in self._doc.setdefault("baseline", {})

    def record_baseline(self, ticker: str, when: date | None = None) -> None:
        self._doc.setdefault("baseline", {})[ticker] = (when or date.today()).isoformat()
        self._dirty = True

    # --- earnings ---------------------------------------------------------
    def has_earnings(self, ticker: str, period: str) -> bool:
        return period in self._doc.setdefault("earnings", {}).get(ticker, {})

    def record_earnings(self, ticker: str, period: str, when: date | None = None) -> None:
        self._doc.setdefault("earnings", {}).setdefault(ticker, {})[period] = (
            when or date.today()
        ).isoformat()
        self._dirty = True

    # --- events -----------------------------------------------------------
    def has_event(self, ticker: str, accession: str) -> bool:
        return accession in self._doc.setdefault("events", {}).get(ticker, {})

    def record_event(self, ticker: str, accession: str, when: date | None = None) -> None:
        self._doc.setdefault("events", {}).setdefault(ticker, {})[accession] = (
            when or date.today()
        ).isoformat()
        self._dirty = True

    # --- persistence ------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self._dirty

    def save(self) -> bool:
        """Write the file if anything changed. Returns whether it was written."""
        if not self._dirty:
            return False
        self.path.write_text(json.dumps(self._doc, indent=2, sort_keys=True) + "\n")
        self._dirty = False
        return True

    def counts(self) -> dict[str, int]:
        return {
            "baseline": len(self._doc.get("baseline", {})),
            "earnings": sum(len(v) for v in self._doc.get("earnings", {}).values()),
            "events": sum(len(v) for v in self._doc.get("events", {}).values()),
        }

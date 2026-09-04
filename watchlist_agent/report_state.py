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
                "calls": {},
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

    def record_earnings(
        self,
        ticker: str,
        period: str,
        when: date | None = None,
        expectation: dict | None = None,
    ) -> None:
        """Record a pre-earnings report, and what it expected.

        The expectation is kept because the post-call report opens on the gap
        between what was expected and what landed, and the report prose is
        emailed and discarded. A handful of structured fields is enough for
        that comparison and far cheaper than archiving the document.
        """
        sent = (when or date.today()).isoformat()
        entry: str | dict = sent
        if expectation:
            entry = {"sent": sent, **{k: v for k, v in expectation.items() if v is not None}}
        self._doc.setdefault("earnings", {}).setdefault(ticker, {})[period] = entry
        self._dirty = True

    def expectation(self, ticker: str, period: str) -> dict:
        """What the pre-earnings report expected, or {} if it was not recorded.

        Entries written before expectations were kept are plain date strings,
        so a missing prior is normal rather than a fault. The post-call report
        says so and carries on -- withholding take-aways because the earlier
        half is missing would punish the reader for our history.
        """
        entry = self._doc.setdefault("earnings", {}).get(ticker, {}).get(period)
        return {k: v for k, v in entry.items() if k != "sent"} if isinstance(entry, dict) else {}

    # --- earnings calls ---------------------------------------------------
    def has_call(self, ticker: str, period: str) -> bool:
        return period in self._doc.setdefault("calls", {}).get(ticker, {})

    def call_source(self, ticker: str, period: str) -> str:
        """What the recorded take-aways were written from, or "" if none.

        Entries predating the source being recorded read as "", which routes
        them to the same place a missing record does: covered, nothing owed.
        """
        entry = self._doc.setdefault("calls", {}).get(ticker, {}).get(period)
        return entry.get("source", "") if isinstance(entry, dict) else ""

    def awaits_transcript(self, ticker: str, period: str) -> bool:
        """Whether a transcript would still add something to what was sent.

        A press release is published within minutes of a company reporting and
        a transcript takes hours, so the first poll after a call always finds
        the release alone. Sending from it and calling the period finished is
        how every take-away came to be release-only: the four-day window meant
        to wait for the transcript never opened, because the door shut on day
        one. A release-sourced record is therefore a partial answer, not a
        closed one -- the queue keeps it until a transcript arrives or the
        window closes on it.
        """
        return self.call_source(ticker, period) == "release"

    def record_call(
        self, ticker: str, period: str, source: str, when: date | None = None
    ) -> None:
        """Record a take-aways report, or a period given up on.

        ``source`` is "transcript", "release" or "missed". Recording the misses
        matters as much as the sends: without it a company whose transcript
        never appears stays in the due queue forever, and the poll spends its
        daily budget rediscovering that every three hours.
        """
        self._doc.setdefault("calls", {}).setdefault(ticker, {})[period] = {
            "at": (when or date.today()).isoformat(),
            "source": source,
        }
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
            "calls": sum(len(v) for v in self._doc.get("calls", {}).values()),
        }

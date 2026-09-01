"""Sending take-aways after each earnings call.

Polls for what a company said once it has reported. The pre-earnings report
covers expectations; this covers what actually happened, and the two are a
matched pair by design -- the reader asked for both, stacked, because "the
pre-earnings is all about expectations and predictions by analysts, the actual
earnings is the reality".

Two things bound the polling. Transcripts appear hours to a day after a call,
so a company is checked for a few days and then given up on rather than left in
the queue forever. And Alpha Vantage's free tier is roughly 25 requests a day
with a one-per-second ceiling, so only companies actually due are polled --
never the watchlist -- and a rate-limit answer stops the run rather than
spending the rest of the budget rediscovering it.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from .call_takeaways import CallMaterial, available, synthesize
from .earnings import EarningsEvent, fetch_recent, for_watchlist
from .email_report import render_call_html, render_call_text, send_email
from .filings import company_name, sec_session
from .release import fetch as fetch_release
from .report_state import ReportState
from .surprises import fetch as fetch_surprises
from .synthesis import CreditsExhausted
from .transcripts import RateLimited, fetch as fetch_transcript
from .watchlist import Watchlist

log = logging.getLogger(__name__)

# How long to keep looking for a transcript after the call. Most appear within
# a day; past four days one is not coming, and a company left in the queue
# spends the daily request budget every three hours proving it.
WINDOW_DAYS = 4

# Earnings cluster hard -- eight holdings reported inside one week in the first
# live batch -- and each report is a model call over a long transcript.
MAX_PER_RUN = 4


@dataclass(frozen=True)
class Due:
    event: EarningsEvent
    reason: str

    @property
    def ticker(self) -> str:
        return self.event.ticker

    @property
    def period(self) -> str:
        return self.event.period


def due_for_calls(
    calendar: dict[str, EarningsEvent],
    state: ReportState,
    today: date | None = None,
    window: int = WINDOW_DAYS,
) -> list[Due]:
    """Companies that have reported and have no take-aways yet.

    Ordered oldest-first: a company whose window is about to close is the one
    at risk of never being covered, so it goes first when the run is capped.
    """
    today = today or date.today()
    pending: list[Due] = []
    for event in calendar.values():
        if event.date > today:
            continue
        age = (today - event.date).days
        if age > window:
            continue
        if state.has_call(event.ticker, event.period):
            continue
        pending.append(Due(
            event=event,
            reason=f"reported {event.date:%b %d}" + (f", {age}d ago" if age else ", today"),
        ))
    pending.sort(key=lambda d: d.event.date)
    return pending


def close_expired(
    calendar: dict[str, EarningsEvent], state: ReportState, today: date | None = None,
    window: int = WINDOW_DAYS,
) -> int:
    """Give up on periods whose window has closed, so the queue cannot grow.

    Recorded as a miss rather than deleted: the next poll needs to know this
    was considered and abandoned, not that it was never seen.
    """
    today = today or date.today()
    closed = 0
    for event in calendar.values():
        if state.has_call(event.ticker, event.period):
            continue
        if event.date <= today - timedelta(days=window + 1):
            state.record_call(event.ticker, event.period, "missed")
            log.info("%s %s: window closed with no transcript or release",
                     event.ticker, event.period)
            closed += 1
    return closed


def gather(due: Due, state: ReportState) -> CallMaterial:
    """Everything the report is written from. No model, no sending."""
    event = due.event
    material = CallMaterial(
        ticker=event.ticker,
        company="",
        period=event.period,
        expectation=state.expectation(event.ticker, event.period),
    )

    with requests.Session() as finnhub, sec_session() as sec:
        finnhub.headers["User-Agent"] = "erimercium-watchlist-agent"
        material.company = company_name(sec, event.ticker)
        # The release is the source of record for every figure, so it is
        # fetched first and the report is not sent without it.
        material.release = fetch_release(
            sec, event.ticker, since=event.date - timedelta(days=1)
        )
        material.surprises = fetch_surprises(finnhub, event.ticker)
        material.transcript = fetch_transcript(finnhub, event.ticker, event.period)

    return material


def _send(material: CallMaterial, state: ReportState, dry_run: bool) -> bool:
    result = synthesize(material)
    subject = (
        f"Earnings call: {material.title} — {material.period}"
        + ("" if material.transcript else " (from the release)")
    )

    if dry_run:
        log.info("[dry run] would send %r", subject)
        print(render_call_text(result, material))
        return False

    send_email(
        subject,
        render_call_text(result, material),
        render_call_html(result, material),
    )
    # Record only after a successful send, so a delivery failure is retried.
    state.record_call(material.ticker, material.period, material.source)
    return True


def run_due(dry_run: bool = False, max_reports: int = MAX_PER_RUN) -> int:
    watchlist = Watchlist()
    state = ReportState()

    with requests.Session() as session:
        session.headers["User-Agent"] = "erimercium-watchlist-agent"
        calendar = for_watchlist(
            fetch_recent(session, days_back=WINDOW_DAYS + 2), watchlist.tickers
        )

    pending = due_for_calls(calendar, state)
    if not pending:
        log.info("no calls awaiting take-aways")
        if close_expired(calendar, state):
            state.save()
        return 0

    if not available():
        log.error("no ANTHROPIC_API_KEY — %d call(s) awaiting take-aways", len(pending))
        return 1

    if len(pending) > max_reports:
        log.warning(
            "%d calls due but capping this run at %d; the rest stay due",
            len(pending), max_reports,
        )
        pending = pending[:max_reports]

    log.info("%d call(s) this run: %s", len(pending),
             ", ".join(f"{d.ticker} {d.period}" for d in pending))

    sent = 0
    for due in pending:
        log.info("--- %s %s: %s", due.ticker, due.period, due.reason)
        try:
            material = gather(due, state)
        except RateLimited as exc:
            # The transcript budget is spent. Stop rather than burn the rest of
            # the day proving it; the remaining companies are still in window.
            log.warning("transcript source rate-limited, stopping this run: %s", exc)
            break

        if not material.release and not material.transcript:
            log.info("%s %s: nothing published yet, will look again", due.ticker, due.period)
            continue

        try:
            if _send(material, state, dry_run):
                sent += 1
                state.save()
        except CreditsExhausted as exc:
            log.error("out of Anthropic credit — %s not written: %s", due.ticker, exc)
            break

    if close_expired(calendar, state):
        state.save()
    log.info("sent %d take-aways; state now %s", sent, state.counts())
    return 0


def show_due() -> int:
    watchlist = Watchlist()
    state = ReportState()
    with requests.Session() as session:
        session.headers["User-Agent"] = "erimercium-watchlist-agent"
        calendar = for_watchlist(
            fetch_recent(session, days_back=WINDOW_DAYS + 2), watchlist.tickers
        )
    pending = due_for_calls(calendar, state)
    if not pending:
        print("No calls awaiting take-aways.")
        return 0
    for due in pending:
        print(f"  {due.ticker:<8} {due.period}  {due.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Take-aways from earnings calls.")
    parser.add_argument("--run", action="store_true", help="Generate and send what is due.")
    parser.add_argument("--due", action="store_true", help="Show what is due, generating nothing.")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of emailing.")
    parser.add_argument("--max-reports", type=int, default=MAX_PER_RUN)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.run or args.dry_run:
        return run_due(dry_run=args.dry_run, max_reports=args.max_reports)
    return show_due()


if __name__ == "__main__":
    raise SystemExit(main())

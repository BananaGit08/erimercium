"""Sending take-aways after each earnings call.

Polls for what a company said once it has reported. The pre-earnings report
covers expectations; this covers what actually happened, and the two are a
matched pair by design -- the reader asked for both, stacked, because "the
pre-earnings is all about expectations and predictions by analysts, the actual
earnings is the reality".

The two sources arrive at different times, and that governs the whole design.
The 8-K press release is on EDGAR minutes after a company reports; a transcript
takes hours. So the first poll after a call finds the release alone, and take-
aways go out from it that day. The period stays open afterwards: if a
transcript appears inside the window, a second report follows covering what the
release could not carry -- above all the Q&A, which is the half the reader
asked for by name. Treating the release as the final word is how every
take-away in the first live week came to be release-only.

Two things bound the polling. A transcript takes days rather than hours -- Palo
Alto Networks reported on a Tuesday and its call was published somewhere in the
following four days -- so a company is checked for a week and then given up on
rather than left in the queue forever. And Alpha Vantage's free tier is roughly
25 requests a day
with a one-per-second ceiling, so only companies actually due are polled --
never the watchlist. A spent budget stops the run asking for transcripts, but
does not stop the run: a report the release can carry needs nothing from that
source, and used to be lost with it.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from .call_takeaways import (
    CallMaterial,
    available,
    resolve_consensus,
    synthesize,
)
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

# How long to keep looking for a transcript after the call.
#
# Four days was a guess, and measurement contradicted it. Alpha Vantage had
# nothing for Palo Alto Networks the day after it reported (Sep 1), and had the
# full call four days later -- so a transcript does arrive, but not within a
# day. Because this workflow polls on weekdays only, a four-day window opened
# on a Tuesday-to-Friday report closes over the weekend without a single poll
# in the interval where the transcript actually appears. PANW is exactly that
# case: its transcript was published, and the queue would have given up on it
# before looking again.
#
# Seven days clears a weekend with margin. The cost is bounded: only companies
# already covered from the release stay in the queue, each costs one request
# per poll, and a spent budget now degrades rather than dropping reports.
WINDOW_DAYS = 7

# Earnings cluster hard -- eight holdings reported inside one week in the first
# live batch -- and each report is a model call over a long transcript.
MAX_PER_RUN = 4


@dataclass(frozen=True)
class Due:
    event: EarningsEvent
    reason: str
    # Take-aways already went out from the press release and only a transcript
    # would improve on them. Nothing is sent unless one turns up.
    upgrade: bool = False

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
    """Companies that have reported and are owed take-aways or a better set.

    Two kinds of work. A company with nothing sent yet is owed a report. One
    whose report was written from the press release is owed the transcript, if
    a transcript appears before the window closes -- the Q&A is the half the
    reader asked for by name, and it does not exist yet when the release does.

    Never-covered companies come first: a company with no email at all has
    more at stake than one whose email could be improved. Within each kind the
    oldest goes first, being nearest to losing its chance entirely.
    """
    today = today or date.today()
    pending: list[Due] = []
    for event in calendar.values():
        if event.date > today:
            continue
        age = (today - event.date).days
        if age > window:
            continue
        upgrade = state.awaits_transcript(event.ticker, event.period)
        if state.has_call(event.ticker, event.period) and not upgrade:
            continue
        when = f"reported {event.date:%b %d}" + (f", {age}d ago" if age else ", today")
        pending.append(Due(
            event=event,
            reason=f"{when}; release take-aways sent, waiting on the transcript"
                   if upgrade else when,
            upgrade=upgrade,
        ))
    pending.sort(key=lambda d: (d.upgrade, d.event.date))
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


def gather(due: Due, state: ReportState, want_transcript: bool = True) -> CallMaterial:
    """Everything the report is written from. No model, no sending."""
    event = due.event
    material = CallMaterial(
        ticker=event.ticker,
        company="",
        period=event.period,
        expectation=state.expectation(event.ticker, event.period),
        follows_release=due.upgrade,
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
        # The transcript enriches the report; the release is what it is built
        # from. A spent transcript budget used to abort the whole run, so a
        # company whose release was sitting in hand got no email at all -- an
        # optional source taking down the source of record.
        if want_transcript:
            try:
                material.transcript = fetch_transcript(
                    finnhub, event.ticker, event.period
                )
            except RateLimited as exc:
                log.warning("%s transcript source rate-limited: %s", event.ticker, exc)
                material.transcript_unavailable = True
        else:
            material.transcript_unavailable = True

    # The calendar entry for this quarter carries the consensus estimates, and
    # the surprise feed carries them again once it catches up. Both were being
    # fetched and neither reached the report, so every take-away said no
    # comparison was possible while the numbers sat in memory.
    reported = next(
        (q for q in (material.surprises.quarters if material.surprises else [])
         if q.period == event.period),
        None,
    )
    material.consensus = resolve_consensus(
        event_eps=event.eps_estimate,
        event_revenue=event.revenue_estimate,
        expectation=material.expectation,
        reported_estimate=reported.estimate if reported else None,
    )
    log.info(
        "%s %s consensus: EPS %s, revenue %s",
        event.ticker, event.period,
        material.consensus.eps if material.consensus.eps is not None else "n/a",
        material.consensus.revenue if material.consensus.revenue is not None else "n/a",
    )

    return material


def _send(material: CallMaterial, state: ReportState, dry_run: bool) -> bool:
    result = synthesize(material)
    if material.follows_release:
        tail = " (from the call transcript)"
    elif material.transcript:
        tail = ""
    else:
        tail = " (from the release)"
    subject = f"Earnings call: {material.title} — {material.period}{tail}"

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

    log.info(
        "%d call(s) this run: %s", len(pending),
        ", ".join(
            f"{d.ticker} {d.period}" + (" (transcript upgrade)" if d.upgrade else "")
            for d in pending
        ),
    )

    sent = 0
    # Once the transcript budget is spent every further request returns the
    # same answer, so stop asking -- but keep going, because a release-based
    # report needs nothing from that source.
    transcript_budget = True
    for due in pending:
        log.info("--- %s %s: %s", due.ticker, due.period, due.reason)
        material = gather(due, state, want_transcript=transcript_budget)
        if material.transcript_unavailable and transcript_budget:
            transcript_budget = False

        if due.upgrade:
            # Release take-aways are already with the reader. Only a transcript
            # justifies a second email; without one there is nothing to add.
            if not material.transcript:
                log.info("%s %s: still no transcript, nothing to add",
                         due.ticker, due.period)
                continue
        elif not material.release and not material.transcript:
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
    if not transcript_budget:
        log.warning(
            "the transcript budget ran out during this run; any release-based "
            "take-aways still went out and upgrades stay due"
        )
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

"""Entry point for research reports."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import requests

from .config import ConfigError, finnhub_api_key
from .dossier import build, to_prompt_context
from .email_report import (
    render_research_html,
    render_research_text,
    research_subject,
    send_email,
)
from .earnings import fetch_calendar, for_watchlist
from .report_state import ReportState
from .scheduler import ReportRequest, due_for_baseline, due_for_earnings
from .synthesis import SynthesisUnavailable, available, synthesize
from .watchlist import Watchlist

log = logging.getLogger("watchlist_agent.deep_dive")


def show_due(baseline_limit: int) -> int:
    """Print what the scheduler would generate today, without generating it."""
    watchlist = Watchlist()
    state = ReportState()
    finnhub_api_key()  # fail fast with a clear message if unset

    with requests.Session() as session:
        session.headers["User-Agent"] = "erimercium-watchlist-agent"
        calendar = for_watchlist(fetch_calendar(session), watchlist.tickers)

    earnings_due = due_for_earnings(calendar, state, date.today())
    baseline_due = due_for_baseline(watchlist.tickers, state, baseline_limit)

    print(f"\nstate so far: {state.counts()}")
    print(f"watchlist: {len(watchlist.tickers)} tickers")
    print(f"earnings calendar: {len(calendar)} of them report within 30 days\n")

    print(f"DUE NOW — earnings ({len(earnings_due)}):")
    for r in earnings_due:
        print(f"  {r.ticker:<8} {r.reason}")
    if not earnings_due:
        print("  (none within the lead window)")

    print(f"\nDUE NOW — baseline ({len(baseline_due)} of "
          f"{len([t for t in watchlist.tickers if not state.has_baseline(t)])} outstanding):")
    for r in baseline_due:
        print(f"  {r.ticker:<8} {r.reason}")

    upcoming = sorted(calendar.values(), key=lambda e: e.date)[:12]
    print("\nNEXT EARNINGS DATES:")
    for e in upcoming:
        print(f"  {e.ticker:<8} {e.date:%b %d}  {e.period}  "
              f"EPS est {e.eps_estimate if e.eps_estimate is not None else 'n/a'}")
    return 0


def _generate_and_send(request: ReportRequest, state: ReportState, dry_run: bool) -> bool:
    """One report: gather, write, send, record. Returns whether it was sent."""
    try:
        # Gathering is inside the guard too: an unreachable data source is at
        # least as likely as a synthesis failure, and one bad ticker must not
        # take down the rest of the batch.
        dossier = build(request.ticker, kind=request.kind, reason=request.reason)
        report = synthesize(dossier)
    except Exception as exc:  # noqa: BLE001 - one failure must not stop the batch
        log.error("%s report failed: %s: %s", request.ticker, type(exc).__name__, exc)
        return False

    subject = research_subject(report, dossier)
    if dry_run:
        log.info("[dry run] would send %r", subject)
        return False

    send_email(
        subject,
        render_research_text(report, dossier),
        render_research_html(report, dossier),
    )

    # Record only after a successful send, so a delivery failure is retried
    # rather than silently marked done.
    if request.kind == "baseline":
        state.record_baseline(request.ticker)
    elif request.kind == "earnings":
        state.record_earnings(request.ticker, request.period)
    elif request.kind == "event":
        state.record_event(request.ticker, request.accession)
    return True


def run_due(baseline_limit: int, dry_run: bool, max_reports: int = 6) -> int:
    """Generate and send every report that is due today."""
    watchlist = Watchlist()
    state = ReportState()

    with requests.Session() as session:
        session.headers["User-Agent"] = "erimercium-watchlist-agent"
        calendar = for_watchlist(fetch_calendar(session), watchlist.tickers)

    # Earnings first: those are time-sensitive, baselines are not.
    pending = due_for_earnings(calendar, state, date.today())
    pending += due_for_baseline(watchlist.tickers, state, baseline_limit)

    if not pending:
        log.info("nothing due today")
        return 0

    if len(pending) > max_reports:
        # Earnings cluster: 8 of the watchlist report within a week of each
        # other, and at ~5 minutes each a full queue outlives any sensible job
        # timeout. Take the front of the queue; the rest are still due
        # tomorrow, and earnings are ordered soonest-first.
        log.warning(
            "%d reports due but capping this run at %d; the remainder stay due",
            len(pending), max_reports,
        )
        pending = pending[:max_reports]

    log.info("%d reports this run: %s", len(pending),
             ", ".join(f"{r.ticker}({r.kind})" for r in pending))

    sent = 0
    for request in pending:
        log.info("--- %s (%s): %s", request.ticker, request.kind, request.reason)
        if _generate_and_send(request, state, dry_run):
            sent += 1
            # Persist after each send so an interrupted batch does not repeat
            # work already delivered.
            state.save()

    log.info("sent %d of %d due reports; state now %s", sent, len(pending), state.counts())
    return 0


def one_report(ticker: str, dossier_only: bool) -> int:
    ticker = ticker.upper()
    dossier = build(ticker, kind="manual", reason="requested directly")

    if dossier_only or not available():
        if not dossier_only:
            print(
                "\n[ANTHROPIC_API_KEY not set — printing the gathered dossier "
                "instead of a written report]\n"
            )
        print(to_prompt_context(dossier))
        return 0

    try:
        report = synthesize(dossier)
    except SynthesisUnavailable as exc:
        log.error("%s", exc)
        return 1

    print(f"\n{'=' * 70}\n{dossier.title} — {report.grade or 'ungraded'}\n{'=' * 70}\n")
    for label in ("Summary", "Leadership", "Financial health", "Valuation",
                  "Analyst sentiment", "Catalysts and risks"):
        if label in report.sections:
            print(f"{label.upper()}\n{report.sections[label]}\n")
    if report.grade:
        print(f"GRADE: {report.grade}\n{report.grade_reason}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deep-dive research reports.")
    parser.add_argument("--ticker", help="Build a report for one ticker.")
    parser.add_argument(
        "--dossier-only",
        action="store_true",
        help="Print the gathered data without writing a report (needs no API key).",
    )
    parser.add_argument(
        "--due",
        action="store_true",
        help="Show what the scheduler would generate today, without generating it.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Generate and email every report that is due today.",
    )
    parser.add_argument(
        "--max-reports",
        type=int,
        default=6,
        help="Hard cap on reports generated in one run (default 6).",
    )
    parser.add_argument(
        "--baseline-limit",
        type=int,
        default=5,
        help="How many first-time reports to generate per run (default 5).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        if args.run:
            return run_due(args.baseline_limit, args.dossier_only, args.max_reports)
        if args.due:
            return show_due(args.baseline_limit)
        if args.ticker:
            return one_report(args.ticker, args.dossier_only)
    except ConfigError as exc:
        log.error("%s", exc)
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

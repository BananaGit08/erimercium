"""Entry point for research reports."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import requests

from .config import ConfigError, finnhub_api_key
from .dossier import build, to_prompt_context
from .earnings import fetch_calendar, for_watchlist
from .report_state import ReportState
from .scheduler import due_for_baseline, due_for_earnings
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
        help="Show what the scheduler would generate today.",
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

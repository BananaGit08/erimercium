"""Entry point for the daily digest job."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, now_et, should_run_for_schedule
from .email_report import render_html, render_text, send_email, subject_line
from .movers import select_movers, split_for_email
from .prices import fetch_quotes
from .volatility import fetch_sigmas
from .watchlist import Watchlist

log = logging.getLogger("watchlist_agent")


def build_digest(dry_run: bool = False) -> int:
    watchlist = Watchlist()
    tickers = watchlist.tickers
    thresholds = watchlist.thresholds
    log.info("pricing %d tickers", len(tickers))

    quotes, failures = fetch_quotes(tickers)
    log.info("priced %d/%d tickers", len(quotes), len(tickers))

    sigmas = fetch_sigmas([q.ticker for q in quotes])
    movers = select_movers(quotes, sigmas, thresholds)
    shown, overflow = split_for_email(movers, thresholds.max_shown)
    when = now_et()

    log.info("%d moves flagged as unusual", len(movers))
    for m in movers:
        log.info("  %s %+.2f%% — %s", m.ticker, m.change_pct, m.reason)

    subject = subject_line(shown, when)
    text_body = render_text(shown, overflow, len(tickers), failures, when)
    html_body = render_html(shown, overflow, len(tickers), failures, when)

    if dry_run:
        print(text_body)
        print("\n[dry run] email not sent")
        return 0

    send_email(subject, text_body, html_body)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send the daily watchlist digest.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest to stdout instead of emailing it.",
    )
    parser.add_argument(
        "--schedule",
        default="",
        help=(
            "The cron expression that triggered this run (github.event.schedule). "
            "Used to discard the off-season DST cron entry."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Send regardless of which cron entry fired.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    if not args.force and args.schedule:
        allowed, reason = should_run_for_schedule(args.schedule)
        log.info("%s", reason)
        if not allowed:
            return 0

    try:
        return build_digest(dry_run=args.dry_run)
    except ConfigError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

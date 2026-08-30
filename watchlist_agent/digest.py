"""Entry point for the daily digest job."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, now_et, should_run_for_schedule
from .email_report import render_html, render_text, send_email, subject_line
from .prices import fetch_quotes, significant_movers
from .watchlist import Watchlist

log = logging.getLogger("watchlist_agent")


def build_digest(dry_run: bool = False) -> int:
    watchlist = Watchlist()
    tickers = watchlist.tickers
    threshold = watchlist.move_threshold_pct
    log.info("pricing %d tickers (threshold %.1f%%)", len(tickers), threshold)

    quotes, failures = fetch_quotes(tickers)
    movers = significant_movers(quotes, threshold)
    when = now_et()

    log.info(
        "priced %d/%d tickers, %d movers above %.1f%%",
        len(quotes),
        len(tickers),
        len(movers),
        threshold,
    )
    for q in movers:
        log.info("  %s %+.2f%%", q.ticker, q.change_pct)

    subject = subject_line(movers, when)
    text_body = render_text(movers, threshold, len(tickers), failures, when)
    html_body = render_html(movers, threshold, len(tickers), failures, when)

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

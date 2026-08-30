"""Entry point for the daily digest job."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, now_et, should_run_for_schedule
from .email_report import render_html, render_text, send_email, subject_line
from .movers import select_movers, split_for_email
from .prices import fetch_quotes
from .volatility import coverage_warning, fetch_sigmas
from .watchlist import Watchlist

log = logging.getLogger("watchlist_agent")


def explain() -> int:
    """Dump the flagging inputs for every ticker, so thresholds can be tuned."""
    watchlist = Watchlist()
    thresholds = watchlist.thresholds
    quotes, failures = fetch_quotes(watchlist.tickers)
    sigmas = fetch_sigmas([q.ticker for q in quotes])
    flagged = {m.ticker for m in select_movers(quotes, sigmas, thresholds)}

    rows = []
    for q in quotes:
        sigma = sigmas.get(q.ticker)
        z = abs(q.change_pct) / sigma if sigma else None
        rows.append((q.ticker, q.change_pct, sigma, z, q.ticker in flagged))
    rows.sort(key=lambda r: (r[3] is None, -(r[3] or 0)))

    print(f"\nthresholds: {thresholds}\n")
    print(f"{'TICKER':<8}{'MOVE':>9}{'SIGMA':>9}{'Z':>7}   VERDICT")
    print("-" * 50)
    for ticker, pct, sigma, z, hit in rows:
        sig = f"{sigma:.2f}%" if sigma else "    --"
        zs = f"{z:.2f}" if z else "  --"
        print(f"{ticker:<8}{pct:>+8.2f}%{sig:>9}{zs:>7}   {'FLAGGED' if hit else ''}")

    zs = [r[3] for r in rows if r[3] is not None]
    if zs:
        zs_sorted = sorted(zs, reverse=True)
        print(f"\n{len(flagged)} flagged of {len(quotes)} priced")
        print("highest z-scores: " + ", ".join(f"{z:.2f}" for z in zs_sorted[:10]))
        for bar in (1.5, 1.75, 2.0, 2.5):
            print(f"  would flag at z>={bar}: {sum(1 for z in zs if z >= bar)}")
    for f in failures:
        print(f"unpriced: {f.ticker} ({f.reason})")
    return 0


def build_digest(dry_run: bool = False) -> int:
    watchlist = Watchlist()
    tickers = watchlist.tickers
    thresholds = watchlist.thresholds
    log.info("pricing %d tickers", len(tickers))

    quotes, failures = fetch_quotes(tickers)
    log.info("priced %d/%d tickers", len(quotes), len(tickers))

    priced = [q.ticker for q in quotes]
    sigmas = fetch_sigmas(priced)
    warning = coverage_warning(sigmas, priced)
    movers = select_movers(quotes, sigmas, thresholds)
    shown, overflow = split_for_email(movers, thresholds.max_shown)
    when = now_et()

    log.info("%d moves flagged as unusual", len(movers))
    for m in movers:
        log.info("  %s %+.2f%% — %s", m.ticker, m.change_pct, m.reason)

    subject = subject_line(shown, when)
    text_body = render_text(shown, overflow, len(tickers), failures, when, warning)
    html_body = render_html(shown, overflow, len(tickers), failures, when, warning)

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
        "--explain",
        action="store_true",
        help=(
            "Print every priced ticker with its volatility, z-score and verdict, "
            "then exit. For tuning the thresholds against real data."
        ),
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
        if args.explain:
            return explain()
        return build_digest(dry_run=args.dry_run)
    except ConfigError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Digest formatting and delivery over Gmail SMTP."""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from html import escape

from .config import gmail_address, gmail_app_password, recipient_address
from .prices import Quote, QuoteFailure

log = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_SSL_PORT = 465

DISCLAIMER = (
    "This digest is a synthesis of public information for research purposes, "
    "not financial advice."
)


def _arrow(pct: float) -> str:
    return "▲" if pct >= 0 else "▼"


def subject_line(movers: list[Quote], when: datetime) -> str:
    date = f"{when:%b %d}"
    if not movers:
        return f"Watchlist digest — {date} — no significant moves"
    lead = movers[0]
    if len(movers) == 1:
        return f"Watchlist digest — {date} — {lead.ticker} {lead.change_pct:+.1f}%"
    return (
        f"Watchlist digest — {date} — {len(movers)} movers, "
        f"led by {lead.ticker} {lead.change_pct:+.1f}%"
    )


def render_text(
    movers: list[Quote],
    threshold_pct: float,
    watched_count: int,
    failures: list[QuoteFailure],
    when: datetime,
) -> str:
    lines = [
        f"WATCHLIST DIGEST — {when:%A, %B %d, %Y} (as of {when:%-I:%M %p %Z})",
        f"{watched_count} tickers watched — flagging moves over {threshold_pct:g}%",
        "",
        "=" * 60,
        f"MOVERS (>{threshold_pct:g}%)",
        "=" * 60,
        "",
    ]

    if movers:
        for q in movers:
            lines.append(
                f"  {_arrow(q.change_pct)} {q.ticker:<10} {q.change_pct:+7.2f}%   "
                f"${q.previous_close:,.2f} -> ${q.current:,.2f}"
            )
    else:
        lines.append(f"  No watchlist stock moved more than {threshold_pct:g}% today.")

    lines += [
        "",
        "=" * 60,
        "NEWS & FILINGS",
        "=" * 60,
        "",
        "  [Stage 2] Material news headlines and new 10-Q / 10-K / 8-K filings,",
        "  filtered for materiality, will appear here per ticker.",
        "",
        "=" * 60,
        "DEEP DIVE RESEARCH",
        "=" * 60,
        "",
        "  [Stage 3] Full research reports with a letter grade will be attached",
        "  here automatically for each stock that moved more than the threshold.",
        "",
    ]

    if failures:
        lines += ["=" * 60, "COULD NOT PRICE", "=" * 60, ""]
        for f in failures:
            lines.append(f"  {f.ticker:<10} {f.reason}")
        lines.append("")

    lines += ["-" * 60, DISCLAIMER]
    return "\n".join(lines)


def render_html(
    movers: list[Quote],
    threshold_pct: float,
    watched_count: int,
    failures: list[QuoteFailure],
    when: datetime,
) -> str:
    up, down, muted = "#0f7b3f", "#b3261e", "#6b7280"

    if movers:
        rows = []
        for q in movers:
            color = up if q.change_pct >= 0 else down
            rows.append(
                f'<tr>'
                f'<td style="padding:8px 14px 8px 0;font-weight:600;font-size:15px;">{escape(q.ticker)}</td>'
                f'<td style="padding:8px 14px 8px 0;color:{color};font-weight:600;font-size:15px;white-space:nowrap;">'
                f'{_arrow(q.change_pct)} {q.change_pct:+.2f}%</td>'
                f'<td style="padding:8px 0;color:{muted};font-size:14px;white-space:nowrap;">'
                f'${q.previous_close:,.2f} &rarr; ${q.current:,.2f}</td>'
                f'</tr>'
            )
        movers_block = (
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;width:100%;">' + "".join(rows) + "</table>"
        )
    else:
        movers_block = (
            f'<p style="margin:0;color:{muted};font-size:15px;">'
            f"No watchlist stock moved more than {threshold_pct:g}% today.</p>"
        )

    def section(title: str, body: str) -> str:
        return (
            f'<h2 style="margin:32px 0 12px;font-size:12px;letter-spacing:.09em;'
            f'text-transform:uppercase;color:{muted};font-weight:700;'
            f'border-bottom:1px solid #e5e7eb;padding-bottom:7px;">{escape(title)}</h2>'
            f"{body}"
        )

    placeholder = (
        f'<p style="margin:0;color:{muted};font-size:14px;font-style:italic;">{{}}</p>'
    )

    failures_block = ""
    if failures:
        items = "".join(
            f'<li style="margin-bottom:4px;"><strong>{escape(f.ticker)}</strong> '
            f"&mdash; {escape(f.reason)}</li>"
            for f in failures
        )
        failures_block = section(
            "Could not price",
            f'<ul style="margin:0;padding-left:20px;color:{muted};font-size:14px;">{items}</ul>',
        )

    return f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
            max-width:640px;margin:0 auto;padding:28px 24px;color:#111827;">
  <h1 style="margin:0 0 4px;font-size:20px;font-weight:700;">Watchlist digest</h1>
  <p style="margin:0 0 4px;color:{muted};font-size:14px;">
    {when:%A, %B %d, %Y} &middot; as of {when:%-I:%M %p %Z}
  </p>
  <p style="margin:0;color:{muted};font-size:13px;">
    {watched_count} tickers watched &middot; flagging moves over {threshold_pct:g}%
  </p>

  {section(f"Movers (>{threshold_pct:g}%)", movers_block)}
  {section("News &amp; filings", placeholder.format(
      "Stage 2 will list material news headlines and new 10-Q / 10-K / 8-K "
      "filings here, grouped by ticker and filtered for materiality."))}
  {section("Deep dive research", placeholder.format(
      "Stage 3 will add a full research report with a letter grade here for "
      "each stock that moved more than the threshold."))}
  {failures_block}

  <p style="margin:32px 0 0;padding-top:14px;border-top:1px solid #e5e7eb;
            color:{muted};font-size:12px;line-height:1.5;">{escape(DISCLAIMER)}</p>
</div>"""


def send_email(subject: str, text_body: str, html_body: str) -> None:
    sender = gmail_address()
    recipient = recipient_address()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_SSL_PORT) as smtp:
        smtp.login(sender, gmail_app_password())
        smtp.send_message(message)

    log.info("sent %r to %s", subject, recipient)

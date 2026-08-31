"""Digest formatting and delivery over Gmail SMTP."""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from html import escape

from .config import gmail_address, gmail_app_password, recipient_address
from .materiality import Bullet
from .movers import Mover
from .prices import QuoteFailure

log = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_SSL_PORT = 465

DISCLAIMER = (
    "This digest is a synthesis of public information for research purposes, "
    "not financial advice."
)


def _bullet_rows(bullets: list[Bullet], muted: str) -> str:
    """Material news and filings shown beneath the move that prompted them."""
    if not bullets:
        return ""
    items = []
    for b in bullets:
        label = escape(b.text)
        if b.url:
            label = (
                f'<a href="{escape(b.url, quote=True)}" '
                f'style="color:#1a4fa0;text-decoration:none;">{label}</a>'
            )
        items.append(f'<li style="margin:0 0 3px;">{label}</li>')
    return (
        f'<tr><td></td><td colspan="2" style="padding:0 0 10px;">'
        f'<ul style="margin:0;padding-left:18px;color:#374151;font-size:13px;'
        f'line-height:1.45;">{"".join(items)}</ul></td></tr>'
    )


def _arrow(pct: float) -> str:
    return "▲" if pct >= 0 else "▼"


def subject_line(movers: list[Mover], when: datetime) -> str:
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
    shown: list[Mover],
    overflow: list[Mover],
    watched_count: int,
    failures: list[QuoteFailure],
    when: datetime,
    warning: str | None = None,
    research: dict[str, list[Bullet]] | None = None,
) -> str:
    research = research or {}
    lines = [
        f"WATCHLIST DIGEST — {when:%A, %B %d, %Y} (as of {when:%-I:%M %p %Z})",
        f"{watched_count} tickers watched — flagging moves unusual for each ticker",
        "",
        "=" * 68,
        "UNUSUAL MOVES",
        "=" * 68,
        "",
    ]
    if warning:
        lines += [f"  ⚠ {warning}", ""]

    if shown:
        for m in shown:
            lines.append(
                f"  {_arrow(m.change_pct)} {m.ticker:<10} {m.change_pct:+7.2f}%   "
                f"${m.quote.previous_close:,.2f} -> ${m.quote.current:,.2f}"
            )
            lines.append(f"      {m.reason}")
            for b in research.get(m.ticker, []):
                lines.append(f"        - {b.text}")
                if b.url:
                    lines.append(f"          {b.url}")
        if overflow:
            lines += [
                "",
                f"  + {len(overflow)} more flagged: "
                + ", ".join(f"{m.ticker} {m.change_pct:+.1f}%" for m in overflow),
            ]
        if not any(research.get(m.ticker) for m in shown):
            lines += ["", "  No material news or filings found for these movers."]
    else:
        lines.append("  Nothing moved unusually for its own range today.")

    lines += [
        "",
        "=" * 68,
        "DEEP DIVE RESEARCH",
        "=" * 68,
        "",
        "  [Stage 3] Research reports with a letter grade, on request via chat.",
        "",
    ]

    if failures:
        lines += ["=" * 68, "COULD NOT PRICE", "=" * 68, ""]
        for f in failures:
            lines.append(f"  {f.ticker:<10} {f.reason}")
        lines.append("")

    lines += ["-" * 68, DISCLAIMER]
    return "\n".join(lines)


def render_html(
    shown: list[Mover],
    overflow: list[Mover],
    watched_count: int,
    failures: list[QuoteFailure],
    when: datetime,
    warning: str | None = None,
    research: dict[str, list[Bullet]] | None = None,
) -> str:
    research = research or {}
    up, down, muted = "#0f7b3f", "#b3261e", "#6b7280"

    if shown:
        rows = []
        for m in shown:
            color = up if m.change_pct >= 0 else down
            rows.append(
                f'<tr>'
                f'<td style="padding:10px 14px 2px 0;font-weight:600;font-size:15px;'
                f'vertical-align:top;">{escape(m.ticker)}</td>'
                f'<td style="padding:10px 14px 2px 0;color:{color};font-weight:600;'
                f'font-size:15px;white-space:nowrap;vertical-align:top;">'
                f'{_arrow(m.change_pct)} {m.change_pct:+.2f}%</td>'
                f'<td style="padding:10px 0 2px;color:{muted};font-size:14px;'
                f'white-space:nowrap;vertical-align:top;">'
                f'${m.quote.previous_close:,.2f} &rarr; ${m.quote.current:,.2f}</td>'
                f'</tr>'
                f'<tr><td></td><td colspan="2" style="padding:0 0 4px;color:{muted};'
                f'font-size:12.5px;">{escape(m.reason)}</td></tr>'
                + _bullet_rows(research.get(m.ticker, []), muted)
            )
        movers_block = (
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;width:100%;">' + "".join(rows) + "</table>"
        )
        if overflow:
            tail = ", ".join(
                f"{escape(m.ticker)} {m.change_pct:+.1f}%" for m in overflow
            )
            movers_block += (
                f'<p style="margin:14px 0 0;color:{muted};font-size:13px;">'
                f"<strong>+ {len(overflow)} more flagged:</strong> {tail}</p>"
            )
    else:
        movers_block = (
            f'<p style="margin:0;color:{muted};font-size:15px;">'
            "Nothing moved unusually for its own range today.</p>"
        )

    def section(title: str, body: str) -> str:
        return (
            f'<h2 style="margin:32px 0 12px;font-size:12px;letter-spacing:.09em;'
            f'text-transform:uppercase;color:{muted};font-weight:700;'
            f'border-bottom:1px solid #e5e7eb;padding-bottom:7px;">{title}</h2>'
            f"{body}"
        )

    placeholder = (
        f'<p style="margin:0;color:{muted};font-size:14px;font-style:italic;">{{}}</p>'
    )

    warning_block = ""
    if warning:
        warning_block = (
            '<p style="margin:24px 0 0;padding:11px 13px;border-radius:6px;'
            'background:#fef6e7;border:1px solid #f5d9a3;color:#7a4f01;'
            f'font-size:13px;line-height:1.5;">&#9888; {escape(warning)}</p>'
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
    {watched_count} tickers watched &middot; flagging moves unusual for each ticker
  </p>

  {warning_block}
  {section("Unusual moves", movers_block)}
  {section("Deep dive research", placeholder.format(
      "Stage 3 will add research reports with a letter grade, on request."))}
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


# --- research reports ------------------------------------------------------

GRADE_COLORS = {
    "A": "#0f7b3f", "B": "#3f7b0f", "C": "#8a6d1f", "D": "#b3591e", "F": "#b3261e",
}

RESEARCH_DISCLAIMER = (
    "This report is a synthesis of public information for research purposes, "
    "not financial advice. Predictions about leadership changes or stock "
    "movement are inherently uncertain."
)


def research_subject(report, dossier) -> str:
    grade = f" — {report.grade}" if report.grade else ""
    kind = {
        "baseline": "Research",
        "earnings": "Pre-earnings",
        "event": "Update",
    }.get(report.kind, "Research")
    return f"{kind}: {dossier.title}{grade}"


def render_research_text(report, dossier) -> str:
    from .synthesis import SECTIONS

    lines = [
        f"{dossier.title.upper()}",
        f"{report.kind.title()} report" + (f" — {dossier.reason}" if dossier.reason else ""),
        "",
    ]
    if report.grade:
        lines += [f"GRADE: {report.grade}", report.grade_reason, ""]
    for _, label in SECTIONS:
        body = report.sections.get(label)
        if body:
            lines += ["=" * 68, label.upper(), "=" * 68, "", body, ""]
    lines += ["-" * 68, RESEARCH_DISCLAIMER]
    return "\n".join(lines)


def render_research_html(report, dossier) -> str:
    from .synthesis import SECTIONS

    muted = "#6b7280"
    grade_color = GRADE_COLORS.get(report.grade[:1] if report.grade else "", muted)

    blocks = []
    for _, label in SECTIONS:
        body = report.sections.get(label)
        if not body:
            continue
        if body.lstrip().startswith("- "):
            items = "".join(
                f'<li style="margin:0 0 7px;">{escape(line.lstrip("- ").strip())}</li>'
                for line in body.splitlines()
                if line.strip().startswith("- ")
            )
            rendered = (
                f'<ul style="margin:0;padding-left:20px;font-size:14px;'
                f'line-height:1.55;">{items}</ul>'
            )
        else:
            rendered = (
                f'<p style="margin:0;font-size:14px;line-height:1.6;">'
                f"{escape(body)}</p>"
            )
        blocks.append(
            f'<h2 style="margin:26px 0 10px;font-size:12px;letter-spacing:.09em;'
            f'text-transform:uppercase;color:{muted};font-weight:700;'
            f'border-bottom:1px solid #e5e7eb;padding-bottom:6px;">{escape(label)}</h2>'
            f"{rendered}"
        )

    grade_block = ""
    if report.grade:
        grade_block = (
            f'<div style="margin:18px 0 0;padding:14px 16px;border-radius:8px;'
            f'background:#f6f7f9;border-left:4px solid {grade_color};">'
            f'<div style="font-size:26px;font-weight:700;color:{grade_color};'
            f'line-height:1;">{escape(report.grade)}</div>'
            f'<p style="margin:8px 0 0;font-size:13.5px;line-height:1.5;">'
            f"{escape(report.grade_reason)}</p></div>"
        )

    return f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
            max-width:660px;margin:0 auto;padding:28px 24px;color:#111827;">
  <h1 style="margin:0 0 4px;font-size:21px;font-weight:700;">{escape(dossier.title)}</h1>
  <p style="margin:0;color:{muted};font-size:13px;">
    {escape(report.kind.title())} report{escape(" — " + dossier.reason) if dossier.reason else ""}
  </p>
  {grade_block}
  {"".join(blocks)}
  <p style="margin:30px 0 0;padding-top:14px;border-top:1px solid #e5e7eb;
            color:{muted};font-size:12px;line-height:1.5;">
    {escape(RESEARCH_DISCLAIMER)}
  </p>
</div>"""

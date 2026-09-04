"""Digest formatting and delivery over Gmail SMTP."""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from html import escape

from .config import (
    EARNINGS_LEAD_DAYS,
    gmail_address,
    gmail_app_password,
    now_et,
    recipient_address,
)
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

# A feature the reader does not know exists will not get used, so every email
# carries the one line that explains it.
COMMAND_HINT = (
    'Reply to this email with "add NVDA" or "remove ROKU" to change the '
    "watchlist. Put the command on a line of its own."
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
    earnings: list | None = None,
) -> str:
    research = research or {}
    earnings = earnings or []
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

    if earnings:
        head, tail = earnings[:MAX_EARNINGS_SHOWN], earnings[MAX_EARNINGS_SHOWN:]
        lines += ["", "=" * 68, "REPORTING SOON", "=" * 68, ""]
        for event in head:
            lines.append(
                f"  {event.ticker:<8} {event.date:%b %d}  {event.period}"
                f"{'  ' + event.timing if event.timing else ''}"
            )
        if tail:
            lines.append(
                f"\n  + {len(tail)} more later that fortnight: "
                + ", ".join(e.ticker for e in tail)
            )
        lines += [
            "",
            f"  A full report on each goes out {EARNINGS_LEAD_DAYS} days before "
            "it reports.",
            "",
        ]

    if failures:
        lines += ["=" * 68, "COULD NOT PRICE", "=" * 68, ""]
        for f in failures:
            lines.append(f"  {f.ticker:<10} {f.reason}")
        lines.append("")

    lines += ["-" * 68, COMMAND_HINT, "", DISCLAIMER]
    return "\n".join(lines)


def render_html(
    shown: list[Mover],
    overflow: list[Mover],
    watched_count: int,
    failures: list[QuoteFailure],
    when: datetime,
    warning: str | None = None,
    research: dict[str, list[Bullet]] | None = None,
    earnings: list | None = None,
) -> str:
    research = research or {}
    earnings = earnings or []
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

    warning_block = ""
    if warning:
        warning_block = (
            '<p style="margin:24px 0 0;padding:11px 13px;border-radius:6px;'
            'background:#fef6e7;border:1px solid #f5d9a3;color:#7a4f01;'
            f'font-size:13px;line-height:1.5;">&#9888; {escape(warning)}</p>'
        )

    earnings_block = ""
    if earnings:
        head, tail = earnings[:MAX_EARNINGS_SHOWN], earnings[MAX_EARNINGS_SHOWN:]
        rows = "".join(
            f'<tr>'
            f'<td style="padding:5px 16px 5px 0;font-weight:600;font-size:14px;">'
            f"{escape(e.ticker)}</td>"
            f'<td style="padding:5px 16px 5px 0;font-size:14px;'
            f'white-space:nowrap;">{e.date:%b %d}</td>'
            f'<td style="padding:5px 0;color:{muted};font-size:13px;">'
            f"{escape(e.period)}{escape(' · ' + e.timing) if e.timing else ''}</td>"
            f"</tr>"
            for e in head
        )
        overflow_line = ""
        if tail:
            overflow_line = (
                f'<p style="margin:11px 0 0;color:{muted};font-size:13px;">'
                f"<strong>+ {len(tail)} more later that fortnight:</strong> "
                + escape(", ".join(e.ticker for e in tail))
                + "</p>"
            )
        earnings_block = section(
            "Reporting soon",
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;">{rows}</table>'
            + overflow_line
            + f'<p style="margin:12px 0 0;color:{muted};font-size:13px;">'
            f"A full report on each goes out {EARNINGS_LEAD_DAYS} days before "
            "it reports.</p>",
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
  {earnings_block}
  {failures_block}

  <p style="margin:32px 0 0;padding-top:14px;border-top:1px solid #e5e7eb;
            color:{muted};font-size:12px;line-height:1.5;">{escape(COMMAND_HINT)}<br><br>
            {escape(DISCLAIMER)}</p>
</div>"""


def send_email(
    subject: str,
    text_body: str,
    html_body: str,
    *,
    to: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> None:
    """Send over Gmail SMTP.

    ``in_reply_to`` and ``references`` thread a reply onto the message that
    prompted it, so a watchlist confirmation lands in the conversation the
    reader is already looking at rather than starting a new one.
    """
    sender = gmail_address()
    recipient = to or recipient_address()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
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


def _key_figures(dossier) -> list[tuple[str, str]]:
    """The handful of numbers a buy/sell/hold call actually turns on.

    All of these already exist in the dossier and all of them were previously
    reachable only by reading the prose and hoping the model had mentioned
    them. Anything unavailable is left out rather than shown as a blank: a
    missing row says less than a row saying nothing.
    """
    figures: list[tuple[str, str]] = []
    quote = getattr(dossier, "quote", None)
    if quote and quote.current:
        figures.append((
            "Price",
            f"${quote.current:,.2f}"
            + (f"  ({quote.change_pct:+.2f}%)" if quote.previous_close else ""),
        ))

    metrics = getattr(getattr(dossier, "market", None), "metrics", None) or {}

    def metric(key: str) -> float | None:
        value = metrics.get(key)
        return value if isinstance(value, (int, float)) else None

    if (pe := metric("peTTM")) is not None:
        figures.append(("P/E (TTM)", f"{pe:,.1f}"))
    if (growth := metric("revenueGrowthTTMYoy")) is not None:
        figures.append(("Revenue growth YoY", f"{growth:+.1f}%"))
    if (margin := metric("operatingMarginTTM")) is not None:
        figures.append(("Operating margin", f"{margin:.1f}%"))
    low, high = metric("52WeekLow"), metric("52WeekHigh")
    if low is not None and high is not None:
        figures.append(("52-week range", f"${low:,.2f} – ${high:,.2f}"))

    earnings = getattr(dossier, "earnings", None)
    if earnings:
        figures.append((
            "Reports",
            f"{earnings.date:%b %d} ({earnings.period})"
            + (f", cons. EPS {earnings.eps_estimate:g}"
               if earnings.eps_estimate is not None else ""),
        ))
    return figures


# Peak earnings season puts 20+ of the watchlist inside a fortnight, which is
# more than a daily email should spend on a preview. The nearest few are the
# ones worth naming in full; the rest are a list of tickers.
MAX_EARNINGS_SHOWN = 8

MAX_COVERAGE_NEWS = 6


def _coverage_bullets(dossier) -> list:
    """The filings and headlines the report was written from, best first.

    These already carry URLs and already reach the model; until now they simply
    never reached the reader, who was left to take the report's word for what
    the company filed. Filings come first because a filing is the company
    speaking rather than someone reporting on it.
    """
    news = sorted(dossier.news, key=lambda b: b.sort_key, reverse=True)
    return list(dossier.filings) + news[:MAX_COVERAGE_NEWS]


def _section_html(body: str) -> str:
    """Render one section, keeping prose that sits alongside bullets.

    The previous rule was "if it starts with a dash, keep only the dashed
    lines", which quietly deleted any sentence the model wrote around its
    bullets -- a closing line naming which filing the figures came from would
    simply not appear, and nothing in the email showed that anything was
    missing. Bullets and paragraphs are now both rendered, and a line that
    continues a wrapped bullet is folded back into it rather than promoted to
    a paragraph of its own.
    """
    out: list[str] = []
    bullets: list[str] = []
    para: list[str] = []

    def flush_bullets() -> None:
        if bullets:
            items = "".join(
                f'<li style="margin:0 0 7px;">{escape(b)}</li>' for b in bullets
            )
            out.append(
                f'<ul style="margin:0 0 10px;padding-left:20px;font-size:14px;'
                f'line-height:1.55;">{items}</ul>'
            )
            bullets.clear()

    def flush_para() -> None:
        if para:
            out.append(
                f'<p style="margin:0 0 10px;font-size:14px;line-height:1.6;">'
                f'{escape(" ".join(para))}</p>'
            )
            para.clear()

    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            flush_bullets()
            flush_para()
        elif line.startswith("- ") or line.startswith("* "):
            flush_para()
            bullets.append(line[2:].strip())
        elif bullets:
            bullets[-1] += " " + line
        else:
            para.append(line)
    flush_bullets()
    flush_para()
    return "".join(out)


def _source_line_html(sources: list, muted: str) -> str:
    if not sources:
        return ""
    links = " &middot; ".join(
        f'<a href="{escape(s.url, quote=True)}" '
        f'style="color:#1a4fa0;text-decoration:none;">{escape(s.label)}</a>'
        for s in sources
    )
    return (
        f'<p style="margin:9px 0 0;color:{muted};font-size:12px;'
        f'line-height:1.5;">Sources: {links}</p>'
    )


def _surprise_rows(dossier) -> tuple[list[tuple[str, str, str, str]], str]:
    """The beat/miss table and the line that characterises it.

    Rendered from the data rather than written by the model. The numbers are
    exact and cannot drift, and the pattern line is computed the same way every
    time -- which matters more here than elsewhere, because a run of beats is
    close to the market's base rate and it would be easy to describe it as a
    finding.
    """
    history = getattr(dossier, "surprises", None)
    if not history or not history.quarters:
        return [], ""
    rows = [
        (q.period, f"{q.estimate:.2f}", f"{q.actual:.2f}", q.summary)
        for q in history.quarters
    ]
    return rows, history.characterise()


def render_research_text(report, dossier) -> str:
    from .synthesis import SECTIONS

    lines = [
        f"{dossier.title.upper()}",
        f"{report.kind.title()} report" + (f" — {dossier.reason}" if dossier.reason else ""),
        f"{now_et():%A, %B %d, %Y}",
        "",
    ]
    figures = _key_figures(dossier)
    if figures:
        width = max(len(label) for label, _ in figures)
        for label, value in figures:
            lines.append(f"  {label:<{width}}  {value}")
        lines.append("")
    rows, pattern = _surprise_rows(dossier)
    if rows:
        lines += ["AGAINST CONSENSUS", ""]
        lines.append(f"  {'quarter':<9} {'estimate':>9} {'actual':>9}  result")
        for period, estimate, actual, result in rows:
            lines.append(f"  {period:<9} {estimate:>9} {actual:>9}  {result}")
        lines += ["", f"  {pattern}", ""]

    if report.grade:
        lines += [f"GRADE: {report.grade}", report.grade_reason, ""]
    for _, label in SECTIONS:
        body = report.sections.get(label)
        if body:
            lines += ["=" * 68, label.upper(), "=" * 68, "", body, ""]
            for source in report.sources.get(label, []):
                lines.append(f"    source: {source.label} — {source.url}")
            if report.sources.get(label):
                lines.append("")

    coverage = _coverage_bullets(dossier)
    searched = [s for s in getattr(report, "searched", []) if s.url not in
                {b.url for b in coverage}]
    if coverage or searched:
        lines += ["=" * 68, "SOURCES USED", "=" * 68, ""]
        for bullet in coverage:
            lines.append(f"  - {bullet.text}")
            if bullet.url:
                lines.append(f"    {bullet.url}")
        for source in searched:
            lines.append(f"  - {source.label}")
            lines.append(f"    {source.url}")
        lines.append("")

    lines += ["-" * 68, COMMAND_HINT, "", RESEARCH_DISCLAIMER]
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
        rendered = _section_html(body)
        blocks.append(
            f'<h2 style="margin:26px 0 10px;font-size:12px;letter-spacing:.09em;'
            f'text-transform:uppercase;color:{muted};font-weight:700;'
            f'border-bottom:1px solid #e5e7eb;padding-bottom:6px;">{escape(label)}</h2>'
            f"{rendered}"
            + _source_line_html(report.sources.get(label, []), muted)
        )

    coverage = _coverage_bullets(dossier)
    covered = {b.url for b in coverage}
    # Pages the model searched but did not cite. Shown only as a fallback, so
    # a report can never reach the reader with nothing to check it against.
    searched = [s for s in getattr(report, "searched", []) if s.url not in covered]
    if coverage or searched:
        items = "".join(
            '<li style="margin:0 0 6px;">'
            + (
                f'<a href="{escape(b.url, quote=True)}" '
                f'style="color:#1a4fa0;text-decoration:none;">{escape(b.text)}</a>'
                if b.url else escape(b.text)
            )
            + "</li>"
            for b in coverage
        ) + "".join(
            f'<li style="margin:0 0 6px;">'
            f'<a href="{escape(src.url, quote=True)}" '
            f'style="color:#1a4fa0;text-decoration:none;">{escape(src.label)}</a>'
            f"</li>"
            for src in searched
        )
        blocks.append(
            f'<h2 style="margin:26px 0 10px;font-size:12px;letter-spacing:.09em;'
            f'text-transform:uppercase;color:{muted};font-weight:700;'
            f'border-bottom:1px solid #e5e7eb;padding-bottom:6px;">Sources used</h2>'
            f'<ul style="margin:0;padding-left:20px;font-size:13px;'
            f'line-height:1.5;color:#374151;">{items}</ul>'
        )

    figures = _key_figures(dossier)
    figures_block = ""
    if figures:
        cells = "".join(
            f'<td style="padding:9px 16px 9px 0;vertical-align:top;">'
            f'<div style="color:{muted};font-size:11px;letter-spacing:.05em;'
            f'text-transform:uppercase;">{escape(label)}</div>'
            f'<div style="font-size:15px;font-weight:600;white-space:nowrap;">'
            f"{escape(value)}</div></td>"
            for label, value in figures
        )
        # One row of cells rather than a grid: Outlook and Gmail both render a
        # plain table reliably, and a horizontal strip degrades to a readable
        # stack on a phone where a float or flex layout would not.
        figures_block = (
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;margin:16px 0 0;">'
            f"<tr>{cells}</tr></table>"
        )

    rows, pattern = _surprise_rows(dossier)
    surprise_block = ""
    if rows:
        header = (
            f'<tr><th style="text-align:left;padding:4px 14px 4px 0;color:{muted};'
            f'font-size:11px;letter-spacing:.05em;text-transform:uppercase;'
            f'font-weight:600;">Quarter</th>'
            f'<th style="text-align:right;padding:4px 14px 4px 0;color:{muted};'
            f'font-size:11px;letter-spacing:.05em;text-transform:uppercase;'
            f'font-weight:600;">Estimate</th>'
            f'<th style="text-align:right;padding:4px 14px 4px 0;color:{muted};'
            f'font-size:11px;letter-spacing:.05em;text-transform:uppercase;'
            f'font-weight:600;">Actual</th>'
            f'<th style="text-align:left;padding:4px 0;color:{muted};'
            f'font-size:11px;letter-spacing:.05em;text-transform:uppercase;'
            f'font-weight:600;">Result</th></tr>'
        )
        body = "".join(
            f'<tr><td style="padding:3px 14px 3px 0;font-size:13.5px;">'
            f"{escape(period)}</td>"
            f'<td style="padding:3px 14px 3px 0;font-size:13.5px;text-align:right;'
            f'font-variant-numeric:tabular-nums;">{escape(estimate)}</td>'
            f'<td style="padding:3px 14px 3px 0;font-size:13.5px;text-align:right;'
            f'font-variant-numeric:tabular-nums;">{escape(actual)}</td>'
            f'<td style="padding:3px 0;font-size:13.5px;color:'
            f'{"#0f7b3f" if result.startswith("beat") else "#b3261e" if result.startswith("missed") else muted};">'
            f"{escape(result)}</td></tr>"
            for period, estimate, actual, result in rows
        )
        surprise_block = (
            f'<div style="margin:20px 0 0;">'
            f'<div style="color:{muted};font-size:11px;letter-spacing:.05em;'
            f'text-transform:uppercase;margin:0 0 6px;">Against consensus</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;">{header}{body}</table>'
            f'<p style="margin:8px 0 0;font-size:13px;line-height:1.5;">'
            f"{escape(pattern)}</p></div>"
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
    &middot; {now_et():%B %d, %Y}
  </p>
  {figures_block}
  {surprise_block}
  {grade_block}
  {"".join(blocks)}
  <p style="margin:30px 0 0;padding-top:14px;border-top:1px solid #e5e7eb;
            color:{muted};font-size:12px;line-height:1.5;">
    {escape(COMMAND_HINT)}<br><br>
    {escape(RESEARCH_DISCLAIMER)}
  </p>
</div>"""


# --- earnings call take-aways ---------------------------------------------

CALL_DISCLAIMER = (
    "These take-aways are a synthesis of public information for research "
    "purposes, not financial advice."
)


def _call_figures(material) -> list[tuple[str, str]]:
    """The expectation, and the outcome where it can be stated exactly.

    Rendered from data rather than from the model, so these figures cannot
    drift. Consensus is resolved from whichever source carries it -- the
    calendar first, then the surprise feed, then what was recorded before the
    call. The reported EPS comes from the surprise feed once it carries this
    quarter, which is usually a day behind; until then the row says so rather
    than showing a number nobody checked.
    """
    from .call_takeaways import format_revenue

    figures: list[tuple[str, str]] = []
    consensus = getattr(material, "consensus", None)

    if consensus is not None and consensus.eps is not None:
        figures.append(("Consensus EPS", f"{consensus.eps:.2f}"))

    history = material.surprises
    reported = None
    if history and history.quarters:
        reported = next(
            (q for q in history.quarters if q.period == material.period), None
        )
    if reported is not None:
        figures.append(("Reported EPS", f"{reported.actual:.2f}"))
        figures.append(("Surprise", reported.summary))
    elif consensus is not None and consensus.eps is not None:
        figures.append(("Reported EPS", "not yet in the feed"))

    if consensus is not None and consensus.revenue is not None:
        # No structured feed carries reported revenue on the free tier, so the
        # actual is stated by the report from the release. Showing the bar it
        # had to clear is still worth the row.
        figures.append(("Consensus revenue", format_revenue(consensus.revenue)))

    grade = (material.expectation or {}).get("grade")
    if grade:
        figures.append(("Grade going in", str(grade)))

    return figures


def _call_source_line(material) -> str:
    if material.transcript:
        t = material.transcript
        detail = f"{t.words:,} words"
        if t.analysts:
            count = len(t.analysts)
            detail += f", Q&A from {count} analyst{'' if count == 1 else 's'}"
        return f"Built on the earnings release and the call transcript ({detail})."
    return (
        "Built on the earnings release only — no transcript was available for "
        "this call, so there are no take-aways from the Q&A."
    )


def render_call_text(result, material) -> str:
    lines = [
        material.title.upper(),
        f"Earnings call take-aways — {material.period}",
        f"{now_et():%A, %B %d, %Y}",
        "",
        _call_source_line(material),
        "",
    ]

    figures = _call_figures(material)
    if figures:
        width = max(len(label) for label, _ in figures)
        for label, value in figures:
            lines.append(f"  {label:<{width}}  {value}")
        lines.append("")

    from .call_takeaways import SECTIONS

    for _, label in SECTIONS:
        body = result.sections.get(label)
        if body:
            lines += ["=" * 68, label.upper(), "=" * 68, "", body, ""]

    if material.release:
        lines += [f"Earnings release: {material.release.url}", ""]
    lines += ["-" * 68, COMMAND_HINT, "", CALL_DISCLAIMER]
    return "\n".join(lines)


def render_call_html(result, material) -> str:
    from .call_takeaways import SECTIONS

    muted = "#6b7280"
    figures = _call_figures(material)

    figures_block = ""
    if figures:
        cells = "".join(
            f'<td style="padding:9px 18px 9px 0;vertical-align:top;">'
            f'<div style="color:{muted};font-size:11px;letter-spacing:.05em;'
            f'text-transform:uppercase;">{escape(label)}</div>'
            f'<div style="font-size:15px;font-weight:600;white-space:nowrap;">'
            f"{escape(value)}</div></td>"
            for label, value in figures
        )
        figures_block = (
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;margin:18px 0 0;">'
            f"<tr>{cells}</tr></table>"
        )

    blocks = []
    for _, label in SECTIONS:
        body = result.sections.get(label)
        if not body:
            continue
        items = [line.lstrip("- ").strip() for line in body.splitlines()
                 if line.strip().startswith("-")]
        if items:
            inner = (
                '<ul style="margin:0;padding-left:20px;font-size:13.5px;'
                'line-height:1.55;">'
                + "".join(f'<li style="margin:0 0 5px;">{escape(i)}</li>' for i in items)
                + "</ul>"
            )
        else:
            inner = (
                f'<p style="margin:0;font-size:13.5px;line-height:1.55;">'
                f"{escape(body)}</p>"
            )
        blocks.append(
            f'<div style="margin:22px 0 0;">'
            f'<div style="color:{muted};font-size:11px;letter-spacing:.05em;'
            f'text-transform:uppercase;margin:0 0 7px;">{escape(label)}</div>'
            f"{inner}</div>"
        )

    release_link = ""
    if material.release:
        release_link = (
            f'<p style="margin:20px 0 0;font-size:12px;">'
            f'<a href="{escape(material.release.url, quote=True)}" '
            f'style="color:#1a4fa0;">Earnings release (SEC)</a></p>'
        )

    return f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
            max-width:660px;margin:0 auto;padding:28px 24px;color:#111827;">
  <h1 style="margin:0 0 4px;font-size:21px;font-weight:700;">{escape(material.title)}</h1>
  <p style="margin:0;color:{muted};font-size:13px;">
    Earnings call take-aways &middot; {escape(material.period)}
    &middot; {now_et():%B %d, %Y}
  </p>
  <p style="margin:10px 0 0;color:{muted};font-size:12.5px;line-height:1.5;">
    {escape(_call_source_line(material))}
  </p>
  {figures_block}
  {"".join(blocks)}
  {release_link}
  <p style="margin:28px 0 0;padding-top:14px;border-top:1px solid #e5e7eb;
            color:{muted};font-size:12px;line-height:1.5;">{escape(COMMAND_HINT)}<br><br>
    {escape(CALL_DISCLAIMER)}</p>
</div>"""


def send_credit_notice(pending: list[str], detail: str) -> None:
    """Tell the reader that research has stopped, and why.

    Delivery does not depend on the thing that failed: email goes over Gmail
    and needs no API credit, so this arrives precisely when reports cannot.
    The daily digest is unaffected and keeps running, which is worth saying --
    otherwise silence on the research side reads as the whole system being
    down.
    """
    queued = ", ".join(pending) if pending else "none"
    text = "\n".join([
        "RESEARCH REPORTS PAUSED",
        "",
        "The research reports have stopped because the Anthropic API account "
        "is out of credit.",
        "",
        f"Waiting to be written: {queued}",
        "",
        "The daily market digest is not affected and will keep arriving on "
        "schedule -- it does not use the API. Research reports resume by "
        "themselves once credit is added; nothing needs to be restarted, and "
        "nothing queued has been lost.",
        "",
        f"Reported by the API as: {detail}",
    ])
    html = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        'Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;'
        'padding:28px 24px;color:#111827;">'
        '<h1 style="margin:0 0 14px;font-size:19px;font-weight:700;">'
        "Research reports paused</h1>"
        '<p style="margin:0 0 12px;font-size:14px;line-height:1.6;">The research '
        "reports have stopped because the Anthropic API account is out of "
        "credit.</p>"
        f'<p style="margin:0 0 12px;font-size:14px;line-height:1.6;">'
        f"<strong>Waiting to be written:</strong> {escape(queued)}</p>"
        '<p style="margin:0 0 12px;font-size:14px;line-height:1.6;">The daily '
        "market digest is not affected and will keep arriving on schedule "
        "&mdash; it does not use the API. Research reports resume by "
        "themselves once credit is added; nothing needs to be restarted, and "
        "nothing queued has been lost.</p>"
        f'<p style="margin:22px 0 0;padding-top:12px;border-top:1px solid '
        f'#e5e7eb;color:#6b7280;font-size:12px;line-height:1.5;">'
        f"Reported by the API as: {escape(detail)}</p></div>"
    )
    send_email("Research reports paused — Anthropic credit exhausted", text, html)

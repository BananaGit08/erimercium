"""Turning a dossier into a written, graded research report.

This is the one part of the pipeline that needs a language model. Everything
upstream is free public data and runs without a key; without one this module
declines cleanly and says why, rather than emitting a report built on nothing.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from .dossier import Dossier, to_prompt_context

log = logging.getLogger(__name__)

# The client chose the lower-cost tier; override with RESEARCH_MODEL to compare.
DEFAULT_MODEL = "claude-sonnet-5"
MAX_WEB_SEARCHES = 6

DISCLAIMER = (
    "This report is a synthesis of public information for research purposes, "
    "not financial advice. Predictions about leadership changes or stock "
    "movement are inherently uncertain."
)

SECTIONS = [
    ("SUMMARY", "Summary"),
    ("LEADERSHIP", "Leadership"),
    ("FINANCIAL HEALTH", "Financial health"),
    ("VALUATION", "Valuation"),
    ("ANALYST SENTIMENT", "Analyst sentiment"),
    ("CATALYSTS AND RISKS", "Catalysts and risks"),
]

SYSTEM_PROMPT = """\
You write concise equity research briefings for one reader who makes his own \
investment decisions. He wants the evidence laid out, not a recommendation.

You are given a factual dossier assembled from SEC filings, market data and \
news. Use web search to research anything the dossier cannot supply. Two \
things always need searching:

- Leadership: who runs the company, how long they have been there, and what \
their track record is, including prior wins, controversies and strategic \
changes they drove.
- Analyst ratings and price targets: the structured feed for these is \
paywalled, so the dossier never carries them. Search for recent ratings \
actions and target changes, and say which way sentiment is moving.

Rules that matter more than style:
- Never invent a number. Every figure must come from the dossier or a source \
you searched. If something is unavailable, say so plainly in one clause.
- Write every sentence yourself. Sourcing a figure does not mean pasting the \
sentence it came in: quoting headlines or press-release phrasing inline \
produces bullets that do not parse as English. Take the number, state it in \
your own words, and name the source only where it matters ("JPMorgan cut it \
to Underweight"). Quote directly only when the exact wording is the point, \
such as a phrase from a company statement, and keep it under fifteen words.
- Each bullet is one complete sentence, or two short ones. If a bullet needs \
a colon, a parenthetical and a subordinate clause to hold itself together, it \
is carrying two ideas and should be two bullets or one shorter one.
- The dossier lists known data gaps. State them; do not paper over them.
- Distinguish what happened from what it might mean, and say which is which.
- No hedging filler. "Margins fell for three straight quarters" beats \
"margins may be showing some signs of potential weakness".
- The grade reflects the balance of evidence today, not a prediction of the \
share price.

Output format, exactly. Each heading on its own line, nothing before the first.
All seven headings are required, in this order, and SUMMARY comes first --
it is the only part some readers will read, so never omit it:

SUMMARY
One paragraph on what is actually going on. Write this last, once the rest is
settled, but place it first.

LEADERSHIP
2-4 bullets starting with "- ".

FINANCIAL HEALTH
2-4 bullets on revenue trend, margin trend and debt.

VALUATION
2-4 bullets on multiples versus the company's own history and its peers.

ANALYST SENTIMENT
2-4 bullets on ratings and their direction.

CATALYSTS AND RISKS
2-4 bullets on what is driving things now and what could change the picture \
over the next quarter or two.

GRADE
A single grade from A+ to F on its own line, then one sentence explaining why \
it landed there.
"""


@dataclass
class Report:
    ticker: str
    kind: str
    grade: str = ""
    grade_reason: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    raw: str = ""
    model: str = ""
    searches: int = 0

    @property
    def summary(self) -> str:
        return self.sections.get("Summary", "")


class SynthesisUnavailable(RuntimeError):
    """Raised when no API key is configured."""


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _parse(text: str, ticker: str, kind: str) -> Report:
    """Split the model's output into sections and pull out the grade.

    Parsing is lenient on purpose: a missing heading costs one section, not the
    whole report, and the raw text is always kept so nothing is silently lost.
    """
    report = Report(ticker=ticker, kind=kind, raw=text)
    headings = [h for h, _ in SECTIONS] + ["GRADE"]
    pattern = re.compile(rf"^({'|'.join(headings)})\s*$", re.MULTILINE)

    matches = list(pattern.finditer(text))
    for i, match in enumerate(matches):
        name = match.group(1)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if name == "GRADE":
            # Two-character grades must precede the bare letters, and the
            # trailing guard is a letter lookahead rather than \b: a word
            # boundary cannot fall between "+" and a newline, so "C+" would
            # match as "C". The lookahead also stops "B" matching "Because".
            grade_match = re.search(
                r"\b(A\+|A-|B\+|B-|C\+|C-|D\+|D-|A|B|C|D|F)(?![A-Za-z])", body
            )
            if grade_match:
                report.grade = grade_match.group(1)
                report.grade_reason = body[grade_match.end():].strip(" .\n—-") or body
            else:
                report.grade_reason = body
        else:
            label = dict(SECTIONS)[name]
            report.sections[label] = body

    if not report.sections and not report.grade:
        # Format drifted entirely; keep the text rather than discard it.
        report.sections["Summary"] = text.strip()
    return report


def synthesize(dossier: Dossier, model: str | None = None) -> Report:
    """Write the report. Requires ANTHROPIC_API_KEY."""
    if not available():
        raise SynthesisUnavailable(
            "ANTHROPIC_API_KEY is not set. Every other part of the research "
            "pipeline runs without it, but writing the report needs a model."
        )

    import anthropic  # imported here so the daily digest need not depend on it

    model = model or os.environ.get("RESEARCH_MODEL", "").strip() or DEFAULT_MODEL
    client = anthropic.Anthropic()

    context = to_prompt_context(dossier)
    log.info("synthesising %s report for %s (%d chars of context, model %s)",
             dossier.kind, dossier.ticker, len(context), model)

    with client.messages.stream(
        model=model,
        max_tokens=64000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        tools=[
            {
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": MAX_WEB_SEARCHES,
            }
        ],
        messages=[{
            "role": "user",
            "content": (
                f"Write the report for {dossier.title}.\n\n"
                f"Here is the dossier:\n\n{context}"
            ),
        }],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError(
            f"model declined to produce the report for {dossier.ticker}"
        )

    text = "".join(b.text for b in message.content if b.type == "text")
    report = _parse(text, dossier.ticker, dossier.kind)
    report.model = message.model
    usage = getattr(message, "usage", None)
    server_use = getattr(usage, "server_tool_use", None) if usage else None
    report.searches = getattr(server_use, "web_search_requests", 0) or 0

    expected = {label for _, label in SECTIONS}
    absent = expected - set(report.sections)
    if absent:
        # The last PYPL run silently dropped SUMMARY, which is the section a
        # busy reader is most likely to read. Say so rather than ship a report
        # that is quietly missing a required part.
        log.warning(
            "%s report is missing %s — the model did not follow the format",
            dossier.ticker,
            ", ".join(sorted(absent)),
        )
    if not report.grade:
        log.warning("%s report has no parseable grade", dossier.ticker)

    log.info("%s report: grade %s, %d/%d sections, %d searches",
             dossier.ticker, report.grade or "?", len(report.sections),
             len(expected), report.searches)
    return report

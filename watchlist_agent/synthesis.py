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

# Six was too few. The first DOCU report spent four of its bullets explaining
# that the search limit stopped it verifying the CEO's track record, the 8-K
# officer change and the analyst ratings -- the three things web search is
# there to supply. At $10 per 1,000 searches this ceiling costs about six
# cents a report.
MAX_WEB_SEARCHES = 14

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
class Source:
    """One page the model cited, carried through to the reader.

    Anthropic's web search documentation requires that citations be shown when
    API output is displayed to an end user, and the reader wants them anyway:
    a claim about analyst sentiment is worth much less if you cannot see who
    said it.
    """

    url: str
    title: str = ""

    @property
    def label(self) -> str:
        """A short name for the link — the title, or the bare domain."""
        if self.title:
            return self.title
        host = re.sub(r"^https?://(www\.)?", "", self.url)
        return host.split("/")[0] or self.url


@dataclass
class Report:
    ticker: str
    kind: str
    grade: str = ""
    grade_reason: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    # Section label -> the pages cited in it. Kept per section rather than as
    # one pile so a reader checking the valuation claim is not handed the
    # leadership sources as well.
    sources: dict[str, list[Source]] = field(default_factory=dict)
    # Pages the search tool returned, used when the model cites nothing.
    searched: list[Source] = field(default_factory=list)
    raw: str = ""
    model: str = ""
    searches: int = 0

    @property
    def summary(self) -> str:
        return self.sections.get("Summary", "")

    @property
    def all_sources(self) -> list[Source]:
        seen: set[str] = set()
        out: list[Source] = []
        for sources in self.sources.values():
            for source in sources:
                if source.url not in seen:
                    seen.add(source.url)
                    out.append(source)
        return out


class SynthesisUnavailable(RuntimeError):
    """Raised when no API key is configured."""


class CreditsExhausted(RuntimeError):
    """Raised when the Anthropic account is out of prepaid credit.

    Worth its own type rather than being folded into the generic per-ticker
    failure path: it is not a problem with one company's data, it will hit
    every remaining report in the batch identically, and unlike every other
    failure here it needs a human to spend money before anything works again.
    """


def _is_credit_error(exc: Exception) -> bool:
    # The API reports an empty balance as a 400 invalid_request_error whose
    # message names the credit balance, so the status code alone cannot
    # distinguish it from a malformed request. 402 is checked too in case that
    # ever becomes the response.
    if getattr(exc, "status_code", None) == 402:
        return True
    return "credit balance" in str(exc).lower()


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _parse(text: str, ticker: str, kind: str) -> tuple[Report, dict[str, tuple[int, int]]]:
    """Split the model's output into sections and pull out the grade.

    Parsing is lenient on purpose: a missing heading costs one section, not the
    whole report, and the raw text is always kept so nothing is silently lost.

    Also returns each section's character range in ``text``, which is what lets
    a citation be matched to the section it was used in.
    """
    report = Report(ticker=ticker, kind=kind, raw=text)
    ranges: dict[str, tuple[int, int]] = {}
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
            ranges[label] = (start, end)

    if not report.sections and not report.grade:
        # Format drifted entirely; keep the text rather than discard it.
        report.sections["Summary"] = text.strip()
        ranges["Summary"] = (0, len(text))
    return report, ranges


def _searched_sources(blocks) -> list[Source]:
    """Every page the search tool returned, as a fallback for citations.

    Citations are the better signal, because they say which claim rests on
    which page. But a report with no links at all is the worse failure, and
    this cannot be defeated by a change in how the model attributes its
    sources -- the result blocks are there whenever a search ran.
    """
    seen: set[str] = set()
    found: list[Source] = []
    for block in blocks:
        if getattr(block, "type", "") != "web_search_tool_result":
            continue
        content = getattr(block, "content", None)
        # On failure content is a single error object rather than a list, so
        # branch on the shape before iterating it.
        if not isinstance(content, list):
            log.warning("web search returned an error block: %s",
                        getattr(content, "error_code", content))
            continue
        for result in content:
            url = (getattr(result, "url", "") or "").strip()
            if url and url not in seen:
                seen.add(url)
                found.append(
                    Source(url=url, title=(getattr(result, "title", "") or "").strip())
                )
    return found


def _collect_sources(
    blocks, ranges: dict[str, tuple[int, int]]
) -> dict[str, list[Source]]:
    """Match each cited page to the section whose prose cites it.

    Citations hang off individual text blocks, but sections are found by
    splitting the concatenated text, so the two have to be reconciled by
    position. Recording where each block landed in that concatenation makes the
    match exact rather than a guess: a block belongs to a section when their
    character ranges overlap.
    """
    spans: list[tuple[int, int, list]] = []
    position = 0
    for block in blocks:
        if getattr(block, "type", "") != "text":
            continue
        start, position = position, position + len(block.text)
        citations = getattr(block, "citations", None) or []
        if citations:
            spans.append((start, position, citations))

    sources: dict[str, list[Source]] = {}
    for label, (low, high) in ranges.items():
        seen: set[str] = set()
        found: list[Source] = []
        for start, end, citations in spans:
            if start >= high or end <= low:
                continue
            for citation in citations:
                # Web search yields web_search_result_location; web fetch and
                # document citations have their own types. Anything carrying a
                # url is a page the reader can open, so key on that rather than
                # on an allowlist of citation types that would silently drop a
                # kind we have not seen yet.
                url = (getattr(citation, "url", "") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                found.append(
                    Source(url=url, title=(getattr(citation, "title", "") or "").strip())
                )
        if found:
            sources[label] = found
    return sources


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

    try:
        message = _request_report(client, model, dossier, context)
    except Exception as exc:  # noqa: BLE001 - re-raised; classified first
        if _is_credit_error(exc):
            raise CreditsExhausted(str(exc)) from exc
        raise

    if message.stop_reason == "refusal":
        raise RuntimeError(
            f"model declined to produce the report for {dossier.ticker}"
        )

    text = "".join(b.text for b in message.content if b.type == "text")
    report, ranges = _parse(text, dossier.ticker, dossier.kind)
    report.sources = _collect_sources(message.content, ranges)
    report.searched = _searched_sources(message.content)
    report.model = message.model
    usage = getattr(message, "usage", None)
    server_use = getattr(usage, "server_tool_use", None) if usage else None
    report.searches = getattr(server_use, "web_search_requests", 0) or 0
    _fill_missing_summary(client, model, dossier, report)
    _warn_on_gaps(dossier, report)
    return report


def _request_report(client, model: str, dossier: Dossier, context: str):
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
                # Without this the tool defaults to running inside code
                # execution ("dynamic filtering"), where the model reads
                # filtered output rather than cited search results -- and no
                # citations reach the text blocks at all. A live DOCU run
                # confirmed it: six searches, zero citations. Direct calling
                # puts more into context and costs a little more, which is the
                # price of a report whose claims can be checked.
                "allowed_callers": ["direct"],
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
        return stream.get_final_message()


def _fill_missing_summary(client, model: str, dossier: Dossier, report: Report) -> None:
    if "Summary" not in report.sections and report.sections:
        # Three of four reports in the first real batch dropped SUMMARY despite
        # the prompt requiring it, so asking more firmly is not a fix. Ask for
        # the one missing paragraph directly: it is cheap, needs no search, and
        # cannot be skipped because it is the entire response.
        log.info("%s: summary missing, requesting it separately", dossier.ticker)
        try:
            body = "\n\n".join(
                f"{label}\n{report.sections[label]}"
                for _, label in SECTIONS
                if label in report.sections
            )
            follow_up = client.messages.create(
                model=model,
                max_tokens=1000,
                system=(
                    "Write one paragraph summarising what is going on at this "
                    "company, for a reader who will read nothing else. State "
                    "the situation, not a recommendation. Use only what the "
                    "report below establishes. Reply with the paragraph alone "
                    "-- no heading, no preamble."
                ),
                messages=[{
                    "role": "user",
                    "content": f"Report on {dossier.title}:\n\n{body}",
                }],
            )
            text_out = "".join(
                b.text for b in follow_up.content if b.type == "text"
            ).strip()
            if text_out:
                report.sections["Summary"] = text_out
                log.info("%s: summary recovered", dossier.ticker)
        except Exception as exc:  # noqa: BLE001 - a missing summary is not fatal
            log.warning("%s: could not recover summary: %s", dossier.ticker, exc)


def _warn_on_gaps(dossier: Dossier, report: Report) -> None:
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

    if report.searches and not report.sources:
        log.warning(
            "%s: %d searches ran but no citations came back; falling back to "
            "the %d pages the search tool returned",
            dossier.ticker, report.searches, len(report.searched),
        )

    log.info("%s report: grade %s, %d/%d sections, %d searches, "
             "%d cited, %d searched",
             dossier.ticker, report.grade or "?", len(report.sections),
             len(expected), report.searches, len(report.all_sources),
             len(report.searched))

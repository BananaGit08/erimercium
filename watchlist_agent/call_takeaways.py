"""Take-aways from one earnings call.

Two sources with different jobs, which is how the reader framed it: *"the press
release is the official statement which they read on the earnings call, but
then after the CEO/CFO finishes reading the press release, they do a live Q&A
which then adds important color to the reported earnings."*

So the 8-K release is the source of record for every figure -- filed by the
company, in its own words, with the tables -- and the transcript supplies the
Q&A, which is the only part of a call management has not scripted. The
practical consequence is that the comparison against expectations works whether
or not a transcript exists: it runs on the release and on the estimates
recorded before the call. Only the Q&A section depends on the transcript, and
when there is none the report says so rather than quietly omitting a heading.

Shorter than a research report on purpose. The reader asked for take-aways, not
a document.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from .release import EarningsRelease
from .surprises import SurpriseHistory
from .transcripts import Transcript

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
COST_PER_MTOK_IN = 5.00
COST_PER_MTOK_OUT = 25.00

SECTIONS = [
    ("HEADLINE", "Headline"),
    ("EXPECTED VS ACTUAL", "Expected vs actual"),
    ("GUIDANCE", "Guidance"),
    ("WHAT ANALYSTS PUSHED ON", "What analysts pushed on"),
    ("WATCH NEXT", "Watch next"),
]

DISCLAIMER = (
    "This summary is a synthesis of public information for research purposes, "
    "not financial advice."
)

SYSTEM_PROMPT = """\
You write short take-aways from one company's quarterly earnings call, for a \
reader who follows the company and has already had a pre-earnings report on it.

You are given two things, and they are not interchangeable:

- The earnings press release the company filed with the SEC. This is the source \
of record for every number. Every figure you state must come from here.
- The call transcript, when one exists. This is where the colour is: the Q&A is \
the only part of the call management has not scripted. Prepared remarks track \
the release closely but often add segment detail worth reporting.

Rules that matter more than style:
- Never state a figure that is not in the release. If the transcript and the \
release disagree on a number, the release is right -- transcripts are \
auto-generated and mangle numerals.
- Never invent a number, and never estimate one. If something is not reported, \
say so in a clause.
- Say what happened before what it might mean, and mark which is which.
- No hedging filler. "Margins fell for the third straight quarter" beats \
"margins may be showing signs of potential weakness".
- Be short. This is a one-minute read, not a research report. Fewer, denser \
bullets beat more of them.

Output format, exactly. Each heading on its own line, nothing before the first.

HEADLINE
One paragraph: what this call changed, if anything. If it changed nothing, say
that plainly -- a quarter that went to plan is a useful thing to know.

EXPECTED VS ACTUAL
2-4 bullets starting with "- ". What was expected against what was reported,
and whether the risk the pre-earnings report flagged actually materialised.
Where no prior expectation was recorded, say so in one clause and compare
against consensus alone.

GUIDANCE
2-4 bullets on what management said about the coming quarter and the year, and
how it differs from what they guided before where that is knowable.

WHAT ANALYSTS PUSHED ON
2-4 bullets on the Q&A: what analysts pressed on, and how convincingly it was
answered. This is the most valuable part of the report. If you were given no
transcript, write exactly: "No transcript was available for this call, so the
take-aways come from the earnings release alone."

WATCH NEXT
2-3 bullets on what would confirm or break the story next quarter.
"""


@dataclass
class Takeaways:
    ticker: str
    period: str
    source: str = "release"
    sections: dict[str, str] = field(default_factory=dict)
    raw: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    @property
    def headline(self) -> str:
        return self.sections.get("Headline", "")


@dataclass
class CallMaterial:
    """Everything a take-aways report is written from."""

    ticker: str
    company: str
    period: str
    release: EarningsRelease | None = None
    transcript: Transcript | None = None
    expectation: dict = field(default_factory=dict)
    surprises: SurpriseHistory | None = None

    @property
    def title(self) -> str:
        return f"{self.company} ({self.ticker})" if self.company else self.ticker

    @property
    def source(self) -> str:
        return "transcript" if self.transcript else "release"


def parse(text: str, ticker: str, period: str) -> Takeaways:
    """Split the model's output into sections.

    Lenient in the same way the research parser is: a missing heading costs one
    section, not the report, and the raw text is kept so nothing is lost.
    """
    result = Takeaways(ticker=ticker, period=period, raw=text)
    headings = [h for h, _ in SECTIONS]
    pattern = re.compile(rf"^({'|'.join(headings)})\s*$", re.MULTILINE)

    matches = list(pattern.finditer(text))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            result.sections[dict(SECTIONS)[match.group(1)]] = body

    if not result.sections:
        result.sections["Headline"] = text.strip()
    return result


def to_prompt_context(material: CallMaterial) -> str:
    """The factual material, with each part labelled by what it can be used for."""
    lines = [
        f"COMPANY: {material.title}",
        f"REPORTING PERIOD: {material.period}",
    ]

    expectation = material.expectation
    lines += ["", "WHAT WAS EXPECTED BEFORE THE CALL:"]
    if expectation:
        for label, key in (
            ("consensus EPS estimate", "eps_estimate"),
            ("consensus revenue estimate", "revenue_estimate"),
            ("pre-earnings report grade", "grade"),
            ("analyst sentiment then", "sentiment"),
        ):
            if expectation.get(key) is not None:
                lines.append(f"  {label}: {expectation[key]}")
    else:
        lines.append(
            "  no pre-earnings expectation was recorded for this period — say so "
            "and compare against consensus in the release alone"
        )

    if material.surprises and material.surprises.quarters:
        lines += ["", "RECORD AGAINST CONSENSUS BEFORE THIS QUARTER:",
                  f"  {material.surprises.characterise()}"]

    if material.release:
        lines += [
            "",
            "=" * 70,
            f"EARNINGS PRESS RELEASE (filed {material.release.filed:%B %d, %Y}) — "
            "THE SOURCE OF RECORD FOR EVERY FIGURE",
            "=" * 70,
            "",
            material.release.text[:40_000],
        ]
    else:
        lines += ["", "No earnings release was available for this quarter."]

    if material.transcript:
        t = material.transcript
        lines += [
            "",
            "=" * 70,
            f"CALL TRANSCRIPT — {len(t.segments)} segments, {t.words} words"
            + (f", Q&A from {', '.join(t.analysts[:6])}" if t.analysts else ""),
            "Use this for colour and for the Q&A. Do not take numbers from it.",
            "=" * 70,
            "",
            t.render(),
        ]
    else:
        lines += [
            "",
            "No transcript was available for this call. Say so in the "
            "WHAT ANALYSTS PUSHED ON section, in the exact words the format "
            "requires.",
        ]

    return "\n".join(lines)


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _cost(usage) -> float:
    if usage is None:
        return 0.0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    created = getattr(usage, "cache_creation_input_tokens", 0) or 0
    plain = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    billed_in = plain + created * 1.25 + read * 0.10
    return billed_in / 1_000_000 * COST_PER_MTOK_IN + out / 1_000_000 * COST_PER_MTOK_OUT


def synthesize(material: CallMaterial, model: str | None = None) -> Takeaways:
    """Write the take-aways. Requires a model; everything upstream does not."""
    import anthropic

    from .synthesis import CreditsExhausted, _is_credit_error

    model = model or os.environ.get("CALL_MODEL", "").strip() or DEFAULT_MODEL
    context = to_prompt_context(material)
    log.info(
        "writing %s %s take-aways from the %s (%d chars of context, model %s)",
        material.ticker, material.period, material.source, len(context), model,
    )

    client = anthropic.Anthropic()
    try:
        with client.messages.stream(
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            # Reading a release and a transcript is comprehension, not
            # research: there is nothing to look up that is not already here.
            output_config={"effort": "medium"},
            messages=[{
                "role": "user",
                "content": (
                    f"Write the take-aways for {material.title}'s {material.period} "
                    f"earnings call.\n\n{context}"
                ),
            }],
        ) as stream:
            message = stream.get_final_message()
    except Exception as exc:  # noqa: BLE001 - classified below
        if _is_credit_error(exc):
            raise CreditsExhausted(str(exc)) from exc
        raise

    text = "".join(b.text for b in message.content if b.type == "text")
    result = parse(text, material.ticker, material.period)
    result.source = material.source
    result.model = message.model
    result.input_tokens = getattr(message.usage, "input_tokens", 0) or 0
    result.output_tokens = getattr(message.usage, "output_tokens", 0) or 0
    result.cost = _cost(message.usage)
    log.info(
        "%s take-aways: %d/%d sections, %d in %d out, about $%.3f",
        material.ticker, len(result.sections), len(SECTIONS),
        result.input_tokens, result.output_tokens, result.cost,
    )
    return result

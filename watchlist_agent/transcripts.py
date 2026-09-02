"""Earnings call transcripts from Alpha Vantage.

Confirmed by probe before this was written: Finnhub's transcript endpoint
answers 403 on the free tier, Financial Modeling Prep has moved transcripts
behind paid plans, and Alpha Vantage serves them free -- 42 speaker-attributed
segments and about 9,500 words for a single Adobe call.

The shape matters as much as the availability. Each segment carries a
``speaker`` and a ``title``, so an analyst can be told from the CFO
mechanically rather than by hunting for phrases. That is what makes the Q&A
separable, and the Q&A is the point: the reader put it plainly -- *"the press
release is the official statement which they read on the earnings call, but
then after the CEO/CFO finishes reading the press release, they do a live Q&A
which then adds important color to the reported earnings."*

**The free tier throttles at one request per second and roughly 25 a day, and
signals a breach with HTTP 200 and an advisory string rather than a 429.** That
reads exactly like a paywall page unless you are looking for it -- a probe run
was thrown away to it before this module existed. `RateLimited` is raised so a
caller stops for the day rather than spending the remaining budget discovering
the same thing repeatedly.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

import requests

from .config import HTTP_TIMEOUT_SECONDS

log = logging.getLogger(__name__)

ALPHA_BASE = "https://www.alphavantage.co/query"

# Their published ceiling is one request per second. Sitting just above it
# costs nothing and keeps a whole run from being discarded.
PACE_SECONDS = 1.5

# Anything shorter than this is a stub, not a call. A real transcript runs
# several thousand words; a few hundred means the quarter is not published yet.
MIN_TRANSCRIPT_WORDS = 800

_last_request = 0.0


class RateLimited(RuntimeError):
    """The daily or per-second budget is spent. Stop, do not retry."""


@dataclass(frozen=True)
class Segment:
    speaker: str
    title: str
    content: str

    @property
    def is_analyst(self) -> bool:
        """Whether this speaker is asking rather than presenting.

        Titles arrive as free text -- "Analyst", "Managing Director",
        "Morgan Stanley" -- so this matches the word rather than an enum, and
        the operator is excluded explicitly because they introduce analysts
        without being one.
        """
        haystack = f"{self.title} {self.speaker}".lower()
        if "operator" in haystack:
            return False
        return "analyst" in haystack


@dataclass(frozen=True)
class Transcript:
    ticker: str
    period: str
    segments: list[Segment] = field(default_factory=list)

    @property
    def qa_start(self) -> int | None:
        """Index where the Q&A begins: the first analyst to speak.

        Management sometimes speaks again inside the Q&A, so the boundary is
        the first analyst rather than the last executive. When no analyst is
        attributed the call has no separable Q&A, which the report says rather
        than pretending otherwise.
        """
        for i, segment in enumerate(self.segments):
            if segment.is_analyst:
                return i
        return None

    @property
    def prepared(self) -> list[Segment]:
        start = self.qa_start
        return self.segments[:start] if start is not None else list(self.segments)

    @property
    def qa(self) -> list[Segment]:
        start = self.qa_start
        return self.segments[start:] if start is not None else []

    @property
    def words(self) -> int:
        return sum(len(re.findall(r"\w+", s.content)) for s in self.segments)

    @property
    def analysts(self) -> list[str]:
        seen: list[str] = []
        for segment in self.qa:
            if segment.is_analyst and segment.speaker and segment.speaker not in seen:
                seen.append(segment.speaker)
        return seen

    def render(self, limit: int = 60_000) -> str:
        """The call as text for the writer, prepared remarks and Q&A marked.

        Bounded because a long call runs past 15,000 words and the take-aways
        do not improve for reading all of it. The Q&A is kept in full and the
        prepared remarks are truncated first -- the release already covers what
        the prepared remarks say, and the Q&A is the part nothing else carries.
        """
        def block(segments: list[Segment]) -> str:
            return "\n\n".join(
                f"{s.speaker or 'Unknown'}"
                + (f" ({s.title})" if s.title else "")
                + f":\n{s.content.strip()}"
                for s in segments if s.content.strip()
            )

        qa_text = block(self.qa)
        prepared_text = block(self.prepared)
        room = max(limit - len(qa_text) - 200, 1000)
        if len(prepared_text) > room:
            prepared_text = prepared_text[:room] + "\n\n[prepared remarks truncated]"

        parts = []
        if prepared_text:
            parts.append("=== PREPARED REMARKS ===\n\n" + prepared_text)
        if qa_text:
            parts.append("=== QUESTION AND ANSWER ===\n\n" + qa_text)
        else:
            parts.append(
                "=== QUESTION AND ANSWER ===\n\n"
                "No analyst-attributed segments in this transcript, so the Q&A "
                "could not be separated from the prepared remarks."
            )
        return "\n\n".join(parts)


def _pace() -> None:
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < PACE_SECONDS:
        time.sleep(PACE_SECONDS - elapsed)
    _last_request = time.monotonic()


def parse(ticker: str, period: str, payload: dict) -> Transcript:
    """Build a transcript from the API's JSON. Pure, so the shape is testable."""
    segments = [
        Segment(
            speaker=str(row.get("speaker", "")).strip(),
            title=str(row.get("title", "")).strip(),
            content=str(row.get("content", "")).strip(),
        )
        for row in (payload.get("transcript") or [])
        if isinstance(row, dict) and str(row.get("content", "")).strip()
    ]
    return Transcript(ticker=ticker, period=period, segments=segments)


def fetch(session: requests.Session, ticker: str, period: str) -> Transcript | None:
    """One call's transcript, or None when it is not published yet.

    Raises RateLimited when the budget is spent, so the caller can stop for the
    day instead of spending the rest of it on the same answer.
    """
    key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    if not key:
        log.info("no ALPHAVANTAGE_API_KEY — transcripts unavailable, release only")
        return None

    _pace()
    try:
        resp = session.get(
            ALPHA_BASE,
            params={
                "function": "EARNINGS_CALL_TRANSCRIPT",
                "symbol": ticker,
                "quarter": period,
                "apikey": key,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.warning("%s transcript request failed: %s", ticker, exc)
        return None

    if not resp.ok:
        log.warning("%s transcript HTTP %d", ticker, resp.status_code)
        return None

    try:
        payload = resp.json()
    except ValueError:
        log.warning("%s transcript returned non-JSON", ticker)
        return None

    # A throttle or an exhausted daily budget arrives as a 200 with prose.
    for field_name in ("Information", "Note"):
        if field_name in payload:
            raise RateLimited(str(payload[field_name])[:200])
    if "Error Message" in payload:
        log.warning("%s transcript error: %s", ticker, str(payload["Error Message"])[:160])
        return None

    transcript = parse(ticker, period, payload)
    if transcript.words < MIN_TRANSCRIPT_WORDS:
        log.info(
            "%s %s transcript not published yet (%d words)",
            ticker, period, transcript.words,
        )
        return None

    log.info(
        "%s %s transcript: %d segments, %d words, %d in Q&A from %d analyst(s)",
        ticker, period, len(transcript.segments), transcript.words,
        len(transcript.qa), len(transcript.analysts),
    )
    return transcript

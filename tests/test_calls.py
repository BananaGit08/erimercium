"""Tests for earnings call take-aways.

No network and no credentials: the due queue, the transcript shape, the Q&A
boundary, HTML-to-text, section parsing and the state that keeps the pair
together are all exercised over fixtures.
"""

from __future__ import annotations

from datetime import date, timedelta

from watchlist_agent.call_takeaways import (
    CallMaterial,
    format_revenue,
    parse,
    resolve_consensus,
    to_prompt_context,
)
from watchlist_agent.calls import Due, close_expired, due_for_calls
from watchlist_agent.earnings import EarningsEvent
from watchlist_agent.release import EarningsRelease, to_text
from watchlist_agent.report_state import ReportState
from watchlist_agent.transcripts import Segment, Transcript, parse as parse_transcript

TODAY = date(2026, 9, 1)


def event(ticker="AAPL", days_ago=1, year=2026, quarter=3):
    return EarningsEvent(
        ticker=ticker, date=TODAY - timedelta(days=days_ago), year=year,
        quarter=quarter, eps_estimate=1.50, revenue_estimate=1e10, hour="amc",
    )


def state(tmp_path):
    return ReportState(path=tmp_path / "reports_sent.json")


# --- the due queue ---------------------------------------------------------


def test_a_company_that_reported_is_due(tmp_path):
    due = due_for_calls({"AAPL": event(days_ago=1)}, state(tmp_path), TODAY)
    assert [d.ticker for d in due] == ["AAPL"]


def test_a_company_that_has_not_reported_yet_is_not_due(tmp_path):
    due = due_for_calls({"AAPL": event(days_ago=-2)}, state(tmp_path), TODAY)
    assert due == []


def test_already_covered_is_not_due(tmp_path):
    s = state(tmp_path)
    s.record_call("AAPL", "2026Q3", "transcript")
    assert due_for_calls({"AAPL": event()}, s, TODAY) == []


def test_a_rescheduled_date_still_yields_one_report(tmp_path):
    """Keyed by fiscal period, so moving the date cannot cause a repeat."""
    s = state(tmp_path)
    s.record_call("AAPL", "2026Q3", "transcript")
    moved = event(days_ago=3, quarter=3)
    assert due_for_calls({"AAPL": moved}, s, TODAY) == []


def test_release_take_aways_leave_the_period_open_for_the_transcript(tmp_path):
    """The defect that made every take-away release-only.

    A press release is on EDGAR minutes after a company reports and the
    transcript takes hours, so the first poll always finds the release alone.
    Recording that as finished shut the four-day window before it opened.
    """
    s = state(tmp_path)
    s.record_call("AAPL", "2026Q3", "release")
    due = due_for_calls({"AAPL": event()}, s, TODAY)
    assert [(d.ticker, d.upgrade) for d in due] == [("AAPL", True)]


def test_transcript_take_aways_close_the_period(tmp_path):
    s = state(tmp_path)
    s.record_call("AAPL", "2026Q3", "transcript")
    assert due_for_calls({"AAPL": event()}, s, TODAY) == []


def test_a_release_covered_period_stops_being_chased_past_the_window(tmp_path):
    """Otherwise the upgrade candidate is polled forever."""
    s = state(tmp_path)
    s.record_call("AAPL", "2026Q3", "release")
    assert due_for_calls({"AAPL": event(days_ago=9)}, s, TODAY) == []


def test_a_company_owed_nothing_yet_outranks_one_owed_only_an_upgrade(tmp_path):
    """A reader with no email at all has more at stake than a better email."""
    s = state(tmp_path)
    s.record_call("ADBE", "2026Q3", "release")
    calendar = {
        "AAPL": event("AAPL", days_ago=1),
        "ADBE": event("ADBE", days_ago=3),
    }
    due = due_for_calls(calendar, s, TODAY)
    assert [(d.ticker, d.upgrade) for d in due] == [("AAPL", False), ("ADBE", True)]


def test_a_legacy_call_entry_without_a_source_is_left_alone(tmp_path):
    """Entries predating the source field must not all reopen at once."""
    s = state(tmp_path)
    s._doc.setdefault("calls", {})["AAPL"] = {"2026Q3": "2026-08-31"}
    assert s.awaits_transcript("AAPL", "2026Q3") is False
    assert due_for_calls({"AAPL": event()}, s, TODAY) == []


def test_past_the_window_is_not_due(tmp_path):
    assert due_for_calls({"AAPL": event(days_ago=9)}, state(tmp_path), TODAY) == []


def test_oldest_first_so_a_closing_window_is_covered_before_a_fresh_one(tmp_path):
    calendar = {
        "AAPL": event("AAPL", days_ago=1),
        "ADBE": event("ADBE", days_ago=3),
        "AMD": event("AMD", days_ago=2),
    }
    due = due_for_calls(calendar, state(tmp_path), TODAY)
    assert [d.ticker for d in due] == ["ADBE", "AMD", "AAPL"]


def test_expired_window_is_recorded_as_missed_not_retried_forever(tmp_path):
    s = state(tmp_path)
    calendar = {"AAPL": event(days_ago=9)}
    assert close_expired(calendar, s, TODAY) == 1
    assert s.has_call("AAPL", "2026Q3")
    # And once recorded it is not closed again.
    assert close_expired(calendar, s, TODAY) == 0


def test_a_company_still_inside_the_window_is_not_given_up_on(tmp_path):
    assert close_expired({"AAPL": event(days_ago=2)}, state(tmp_path), TODAY) == 0


# --- the pair: expectations recorded before, read after --------------------


def test_expectation_round_trips(tmp_path):
    s = state(tmp_path)
    s.record_earnings("AAPL", "2026Q3", expectation={
        "eps_estimate": 1.50, "grade": "B+", "revenue_estimate": None,
    })
    kept = s.expectation("AAPL", "2026Q3")
    assert kept["eps_estimate"] == 1.50
    assert kept["grade"] == "B+"
    # None values are not stored as nulls to be reasoned about later.
    assert "revenue_estimate" not in kept


def test_a_legacy_date_only_entry_reads_as_no_expectation(tmp_path):
    """Entries written before expectations were kept are plain strings."""
    s = state(tmp_path)
    s.record_earnings("AAPL", "2026Q3")
    assert s.expectation("AAPL", "2026Q3") == {}
    assert s.has_earnings("AAPL", "2026Q3")


def test_a_missing_prior_does_not_block_the_report():
    """And must not make the report claim no comparison is possible."""
    material = CallMaterial(
        ticker="AAPL", company="Apple", period="2026Q3",
        consensus=resolve_consensus(1.60, 9.4e10, None, None),
    )
    context = to_prompt_context(material)
    assert "consensus EPS: 1.60" in context
    assert "no earlier grade or flagged risk" in context
    assert "do not say a comparison is impossible" in context


# --- transcripts -----------------------------------------------------------

SEGMENTS = [
    {"speaker": "Operator", "title": "Operator", "content": "Welcome to the call."},
    {"speaker": "Tim Cook", "title": "CEO", "content": "We had a strong quarter."},
    {"speaker": "Kevan Parekh", "title": "CFO", "content": "Revenue grew nine percent."},
    {"speaker": "Erik Woodring", "title": "Analyst, Morgan Stanley",
     "content": "Can you talk about iPhone units?"},
    {"speaker": "Tim Cook", "title": "CEO", "content": "Units were up modestly."},
    {"speaker": "Amit Daryanani", "title": "Analyst, Evercore",
     "content": "What about gross margin?"},
]


def test_qa_begins_at_the_first_analyst():
    t = parse_transcript("AAPL", "2026Q3", {"transcript": SEGMENTS})
    assert t.qa_start == 3
    assert len(t.prepared) == 3
    assert len(t.qa) == 3


def test_management_speaking_inside_the_qa_stays_in_the_qa():
    """The boundary is the first analyst, not the last executive."""
    t = parse_transcript("AAPL", "2026Q3", {"transcript": SEGMENTS})
    assert any(s.title == "CEO" for s in t.qa)


def test_the_operator_is_not_mistaken_for_an_analyst():
    assert not Segment("Operator", "Operator", "Our first question comes from an analyst").is_analyst


def test_analysts_are_listed_without_duplicates():
    t = parse_transcript("AAPL", "2026Q3", {"transcript": SEGMENTS + [SEGMENTS[3]]})
    assert t.analysts == ["Erik Woodring", "Amit Daryanani"]


def test_a_call_with_no_attributed_analysts_says_so_rather_than_guessing():
    t = parse_transcript("X", "2026Q1", {"transcript": SEGMENTS[:3]})
    assert t.qa == []
    assert "could not be separated" in t.render()


def test_render_marks_both_halves():
    rendered = parse_transcript("AAPL", "2026Q3", {"transcript": SEGMENTS}).render()
    assert "=== PREPARED REMARKS ===" in rendered
    assert "=== QUESTION AND ANSWER ===" in rendered
    assert rendered.index("PREPARED") < rendered.index("QUESTION AND ANSWER")


def test_the_qa_survives_truncation_and_the_prepared_remarks_give_way():
    """The release already covers the prepared remarks; nothing else carries the Q&A."""
    long_prepared = [
        {"speaker": "CEO", "title": "CEO", "content": "word " * 4000},
        {"speaker": "An Analyst", "title": "Analyst, Citi", "content": "A pointed question."},
    ]
    rendered = parse_transcript("X", "2026Q1", {"transcript": long_prepared}).render(limit=3000)
    assert "A pointed question." in rendered
    assert "[prepared remarks truncated]" in rendered


def test_empty_segments_are_dropped():
    t = parse_transcript("X", "2026Q1", {"transcript": [
        {"speaker": "A", "title": "CEO", "content": "   "},
        {"speaker": "B", "title": "CFO", "content": "Real content."},
    ]})
    assert len(t.segments) == 1


# --- the release -----------------------------------------------------------


def test_html_exhibit_becomes_readable_text():
    html = "<html><body><p>Revenue was $94.0 billion</p><p>EPS was $1.64</p></body></html>"
    text = to_text(html)
    assert "Revenue was $94.0 billion" in text
    assert "EPS was $1.64" in text
    assert "<" not in text


def test_entities_are_decoded_and_scripts_dropped():
    assert "&" in to_text("<p>AT&amp;T</p>")
    assert "alert" not in to_text("<script>alert(1)</script><p>Results</p>")


def test_table_structure_survives_enough_to_read():
    html = "<table><tr><td>Revenue</td><td>94,036</td></tr></table>"
    assert "Revenue" in to_text(html) and "94,036" in to_text(html)


# --- the report ------------------------------------------------------------

MODEL_OUTPUT = """\
HEADLINE
A clean quarter that changed little.

EXPECTED VS ACTUAL
- EPS of $1.64 beat the $1.60 consensus.
- The margin risk flagged before the call did not materialise.

GUIDANCE
- Management guided the December quarter above consensus.

WHAT ANALYSTS PUSHED ON
- Analysts pressed on China, and got a partial answer.

WATCH NEXT
- Whether services growth holds above 12%.
"""


def test_sections_parse():
    result = parse(MODEL_OUTPUT, "AAPL", "2026Q3")
    assert set(result.sections) == {
        "Headline", "Expected vs actual", "Guidance",
        "What analysts pushed on", "Watch next",
    }
    assert "changed little" in result.headline


def test_a_missing_section_costs_one_section_not_the_report():
    trimmed = MODEL_OUTPUT.replace("GUIDANCE\n- Management guided the December quarter above consensus.\n", "")
    result = parse(trimmed, "AAPL", "2026Q3")
    assert "Guidance" not in result.sections
    assert "Headline" in result.sections


def test_output_that_ignores_the_format_is_kept_not_discarded():
    result = parse("The company did fine this quarter.", "AAPL", "2026Q3")
    assert result.sections["Headline"] == "The company did fine this quarter."


def test_context_names_the_release_as_the_source_of_record():
    material = CallMaterial(
        ticker="AAPL", company="Apple Inc.", period="2026Q3",
        release=EarningsRelease("AAPL", TODAY, "0000-00", "http://x", "Revenue was $94B"),
    )
    context = to_prompt_context(material)
    assert "SOURCE OF RECORD FOR EVERY FIGURE" in context
    assert "No transcript was available" in context


def test_context_tells_the_writer_not_to_take_numbers_from_the_transcript():
    material = CallMaterial(
        ticker="AAPL", company="Apple Inc.", period="2026Q3",
        transcript=Transcript("AAPL", "2026Q3", [
            Segment("Tim Cook", "CEO", "We had a strong quarter."),
        ]),
    )
    assert "Do not take numbers from it" in to_prompt_context(material)


def test_source_reflects_what_was_actually_used():
    assert CallMaterial("AAPL", "Apple", "2026Q3").source == "release"
    with_transcript = CallMaterial(
        "AAPL", "Apple", "2026Q3",
        transcript=Transcript("AAPL", "2026Q3", [Segment("A", "CEO", "x")]),
    )
    assert with_transcript.source == "transcript"


# --- choosing the right document out of a filing ---------------------------
#
# On the first live run Dell's "press release" was EDGAR's own index-header
# page: HTML, long enough to pass a word count, and entirely filing metadata.


from watchlist_agent.release import _looks_like_exhibit, _plausible  # noqa: E402


def test_the_conventional_exhibit_is_recognised():
    assert _looks_like_exhibit("ex991q426earningsrelease.htm")
    assert _looks_like_exhibit("a8-kex991q3202606272026.htm")
    assert _looks_like_exhibit("adbeex991q226.htm")


def test_edgar_machinery_is_never_a_candidate():
    for name in (
        "0001571996-26-000039-index-headers.html",
        "0001571996-26-000039-index.htm",
        "R2.htm",
        "form8k.xsd",
    ):
        assert not _plausible(name), name


def test_an_unconventionally_named_document_is_still_allowed():
    assert _plausible("dell-q2fy27results.htm")
    assert _plausible("pressrelease.txt")


def test_a_non_document_is_not_plausible():
    assert not _plausible("logo.jpg")


# --- consensus: the numbers that were fetched and then dropped -------------
#
# PANW's report said "no like-for-like comparison is possible" while the
# consensus sat in two variables in memory: the calendar entry for that
# quarter, and the surprise feed's estimate behind "beat by 2.4%".


def test_the_calendar_estimate_is_preferred():
    """The bar standing when they reported, not the one from a week earlier."""
    c = resolve_consensus(1.00, 3.38e9, {"eps_estimate": 0.96}, 0.99)
    assert c.eps == 1.00
    assert c.eps_source == "Finnhub earnings calendar"
    assert c.revenue == 3.38e9


def test_the_surprise_feed_is_the_second_source():
    c = resolve_consensus(None, None, None, 0.99)
    assert c.eps == 0.99
    assert c.eps_source == "Finnhub surprise feed"


def test_the_recorded_expectation_is_the_last_resort():
    c = resolve_consensus(None, None, {"eps_estimate": 0.96}, None)
    assert c.eps == 0.96
    assert c.eps_source == "recorded before the call"


def test_revenue_falls_back_to_what_was_recorded():
    c = resolve_consensus(1.00, None, {"revenue_estimate": 3.3e9}, None)
    assert c.revenue == 3.3e9
    assert c.revenue_source == "recorded before the call"


def test_no_source_means_no_consensus_rather_than_a_guess():
    c = resolve_consensus(None, None, None, None)
    assert c.eps is None and c.revenue is None


def test_estimate_drift_is_surfaced_only_when_visible():
    """A reader only needs telling when the printed number would differ."""
    moved = resolve_consensus(1.00, None, {"eps_estimate": 0.96}, None)
    assert moved.eps_moved
    held = resolve_consensus(1.00, None, {"eps_estimate": 1.001}, None)
    assert not held.eps_moved


def test_drift_is_explained_in_the_prompt():
    material = CallMaterial(
        ticker="PANW", company="Palo Alto Networks", period="2026Q4",
        consensus=resolve_consensus(1.00, None, {"eps_estimate": 0.96}, None),
    )
    context = to_prompt_context(material)
    assert "used 0.96" in context
    assert "stood at 1.00" in context


def test_the_prompt_names_the_source_of_each_figure():
    """Panels differ between providers; a stated source is not an error."""
    material = CallMaterial(
        ticker="PANW", company="Palo Alto Networks", period="2026Q4",
        consensus=resolve_consensus(1.00, 3.38e9, None, None),
    )
    context = to_prompt_context(material)
    assert "source: Finnhub earnings calendar" in context
    assert "name the source" in context


def test_revenue_formatting_reads_as_a_person_would_say_it():
    assert format_revenue(3.41e9) == "$3.41B"
    assert format_revenue(940e6) == "$940M"
    assert format_revenue(None) == ""


# --- the transcript is an enrichment, not a precondition -------------------


def _patch_sources(monkeypatch, *, transcript=None, release=object(), raises=None):
    """Stand in for every network fetch gather() makes."""
    from watchlist_agent import calls as mod

    monkeypatch.setattr(mod, "company_name", lambda s, t: "Apple Inc.")
    monkeypatch.setattr(mod, "fetch_release", lambda *a, **k: release)
    monkeypatch.setattr(mod, "fetch_surprises", lambda *a, **k: None)

    def _transcript(*a, **k):
        if raises is not None:
            raise raises
        return transcript

    monkeypatch.setattr(mod, "fetch_transcript", _transcript)


def test_a_spent_transcript_budget_does_not_take_down_the_release_report(
    tmp_path, monkeypatch
):
    """An optional source used to abort the run that the release could serve.

    RateLimited propagated out of gather() and broke the loop, so a company
    whose 8-K was already in hand got no email at all.
    """
    from watchlist_agent import calls as mod

    sentinel = object()
    _patch_sources(monkeypatch, release=sentinel, raises=mod.RateLimited("budget"))

    material = mod.gather(Due(event=event(), reason=""), state(tmp_path))

    assert material.release is sentinel
    assert material.transcript is None
    assert material.transcript_unavailable is True


def test_the_transcript_is_not_requested_once_the_budget_is_known_spent(
    tmp_path, monkeypatch
):
    from watchlist_agent import calls as mod

    def _boom(*a, **k):
        raise AssertionError("asked for a transcript with no budget left")

    _patch_sources(monkeypatch)
    monkeypatch.setattr(mod, "fetch_transcript", _boom)

    material = mod.gather(
        Due(event=event(), reason=""), state(tmp_path), want_transcript=False
    )
    assert material.transcript_unavailable is True


def test_an_upgrade_carries_the_follow_up_marking(tmp_path, monkeypatch):
    from watchlist_agent import calls as mod

    _patch_sources(monkeypatch)
    material = mod.gather(
        Due(event=event(), reason="", upgrade=True), state(tmp_path)
    )
    assert material.follows_release is True


# --- a second email on one quarter must not read as a duplicate ------------


def _material(**kw):
    base = dict(ticker="AAPL", company="Apple Inc.", period="2026Q3")
    return CallMaterial(**{**base, **kw})


def test_the_follow_up_says_why_it_exists(tmp_path):
    from watchlist_agent.email_report import _call_source_line

    transcript = Transcript(
        ticker="AAPL", period="2026Q3",
        segments=[Segment("Jane Doe", "Analyst, Big Bank", "On margins — " * 200)],
    )
    line = _call_source_line(_material(transcript=transcript, follows_release=True))
    assert "Follows the take-aways sent from the earnings release" in line
    assert "Q&A" in line


def test_a_first_report_is_not_described_as_a_follow_up(tmp_path):
    from watchlist_agent.email_report import _call_source_line

    line = _call_source_line(_material())
    assert "Follows" not in line
    assert "no transcript was available" in line


def test_the_writer_is_told_to_lead_with_what_the_release_could_not_carry():
    context = to_prompt_context(_material(follows_release=True))
    assert "SECOND REPORT ON THIS QUARTER" in context
    assert "Q&A" in context


def test_a_first_report_gets_no_second_report_framing():
    assert "SECOND REPORT ON THIS QUARTER" not in to_prompt_context(_material())

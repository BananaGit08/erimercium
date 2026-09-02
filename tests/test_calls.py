"""Tests for earnings call take-aways.

No network and no credentials: the due queue, the transcript shape, the Q&A
boundary, HTML-to-text, section parsing and the state that keeps the pair
together are all exercised over fixtures.
"""

from __future__ import annotations

from datetime import date, timedelta

from watchlist_agent.call_takeaways import CallMaterial, parse, to_prompt_context
from watchlist_agent.calls import close_expired, due_for_calls
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
    s.record_call("AAPL", "2026Q3", "release")
    moved = event(days_ago=3, quarter=3)
    assert due_for_calls({"AAPL": moved}, s, TODAY) == []


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
    material = CallMaterial(ticker="AAPL", company="Apple", period="2026Q3")
    context = to_prompt_context(material)
    assert "no pre-earnings expectation was recorded" in context


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

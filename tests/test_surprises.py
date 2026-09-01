"""Tests for the record against consensus.

The point of this feature is calibration, and the trap it exists to avoid is
reporting a bare count -- most large caps beat EPS consensus most quarters, so
"beat 4 of 4" is close to the base rate rather than a finding. These pin the
things that actually distinguish one company from another.
"""

from __future__ import annotations

import pytest

from watchlist_agent.surprises import Surprise, SurpriseHistory, _parse

# The exact shape Finnhub /stock/earnings returns, from a live probe run.
ADBE_ROW = {
    "symbol": "ADBE", "estimate": 5.9385, "actual": 5.96, "period": "2026-06-30",
    "surprise": 0.0215, "surprisePercent": 0.362, "year": 2026, "quarter": 2,
}


def rows(*pairs, start_year=2026, start_q=4):
    """Newest-first rows, as the endpoint returns them."""
    out = []
    year, quarter = start_year, start_q
    for estimate, actual in pairs:
        out.append({"estimate": estimate, "actual": actual, "year": year, "quarter": quarter})
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
    return out


def history(*pairs):
    return SurpriseHistory(ticker="TEST", quarters=_parse("TEST", rows(*pairs)))


# --- parsing the live shape ------------------------------------------------


def test_parses_the_shape_the_endpoint_actually_returns():
    quarters = _parse("ADBE", [ADBE_ROW])
    assert len(quarters) == 1
    assert quarters[0].period == "2026Q2"
    assert quarters[0].actual == pytest.approx(5.96)
    assert quarters[0].verdict == "beat"
    # Cross-check against the surprisePercent the endpoint itself reports.
    assert quarters[0].pct == pytest.approx(0.362, abs=0.01)


def test_takes_only_four_quarters():
    assert len(_parse("T", rows(*[(1.0, 1.1)] * 9))) == 4


def test_quarter_without_an_estimate_is_excluded_not_counted_as_a_miss():
    """An absent estimate is an absence, not a disappointment."""
    parsed = _parse("T", [
        {"estimate": None, "actual": 1.2, "year": 2026, "quarter": 4},
        {"estimate": 1.0, "actual": 1.1, "year": 2026, "quarter": 3},
    ])
    assert len(parsed) == 1
    assert parsed[0].period == "2026Q3"


def test_unparseable_rows_are_dropped():
    assert _parse("T", ["nonsense", {}, {"estimate": "x", "actual": "y"}]) == []


# --- the numbers -----------------------------------------------------------


def test_counts_and_spread():
    h = history((1.00, 1.10), (1.00, 0.90), (1.00, 1.05), (1.00, 1.02))
    assert (h.beats, h.misses) == (3, 1)
    assert h.spread == pytest.approx(20.0)  # +10% best, -10% worst


def test_percentage_withheld_when_the_estimate_is_too_small_to_bear_one():
    """A two-cent beat on a one-cent estimate is +200% and means nothing."""
    q = Surprise(period="2026Q1", actual=0.03, estimate=0.01)
    assert q.pct is None
    assert q.summary == "beat by $0.02"


def test_percentage_used_when_the_estimate_is_large_enough():
    q = Surprise(period="2026Q1", actual=1.10, estimate=1.00)
    assert q.pct == pytest.approx(10.0)
    assert q.summary == "beat by 10.0%"


def test_exactly_in_line():
    q = Surprise(period="2026Q1", actual=1.00, estimate=1.00)
    assert q.verdict == "in line"
    assert q.summary == "in line"


# --- the characterisation, which is the whole point ------------------------


def test_never_reports_a_bare_count():
    """The count alone is the base rate. Something else must always appear."""
    line = history((1.00, 1.10), (1.00, 1.08), (1.00, 1.06), (1.00, 1.04)).characterise()
    assert "all 4" in line
    assert line != "beat 4 of 4"
    assert "%" in line


def test_narrowing_margin_is_named():
    # Oldest to newest: +10, +8, +6, +4 -- shrinking. Rows arrive newest first.
    line = history((1.00, 1.04), (1.00, 1.06), (1.00, 1.08), (1.00, 1.10)).characterise()
    assert "narrowing" in line


def test_widening_margin_is_named():
    line = history((1.00, 1.10), (1.00, 1.08), (1.00, 1.06), (1.00, 1.04)).characterise()
    assert "widening" in line


def test_scattered_record_reports_its_range():
    line = history((1.00, 1.12), (1.00, 0.90), (1.00, 1.05), (1.00, 0.97)).characterise()
    assert "2 beats and 2 misses" in line
    assert "ranging from" in line


def test_all_misses():
    line = history((1.00, 0.95), (1.00, 0.92), (1.00, 0.98), (1.00, 0.90)).characterise()
    assert "missed consensus in all 4" in line


def test_short_history_says_how_short():
    line = history((1.00, 1.10), (1.00, 1.05)).characterise()
    assert "only 2 quarters on file" in line


def test_no_history_says_so_rather_than_implying_a_clean_record():
    assert SurpriseHistory(ticker="T").characterise() == "no surprise history available"


def test_paywall_note_is_carried_into_the_characterisation():
    """The free tier answers premium endpoints with an HTML page and a 200."""
    h = SurpriseHistory(ticker="T", note="200 but non-JSON body: '<!doctype html>'")
    assert "non-JSON" in h.characterise()

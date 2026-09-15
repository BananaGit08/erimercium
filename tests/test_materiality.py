"""Tests for headline figures that disagree with the move we measured.

Written from a live digest. A reader saw "CrowdStrike Shares Rise 4.6%"
printed under our own CRWD +13.85% and asked whether the agent was pulling
outdated data. It was not: the 4.6% was Yahoo's, quoted verbatim from a
headline written earlier in the session, and our price was correct. The
defect was that nothing in the email said whose number was whose.
"""

from __future__ import annotations

from watchlist_agent.email_report import _bullet_label
from watchlist_agent.materiality import (
    Bullet,
    conflicting_move,
    stated_day_move,
)
from watchlist_agent.research import flag_stale_moves


class FakeMover:
    def __init__(self, ticker: str, change_pct: float):
        self.ticker = ticker
        self.change_pct = change_pct


# --- reading a price move out of a headline --------------------------------


def test_the_headline_that_started_this():
    assert stated_day_move(
        "CrowdStrike Shares Rise 4.6% After CEO Comments on AI and Cybersecurity"
    ) == 4.6


def test_the_company_itself_as_subject():
    assert stated_day_move("Corning Tumbles 12% on $2B Equity Offering") == -12.0


def test_only_the_first_company_in_a_roundup_counts():
    """The others are separate stories sharing one headline."""
    assert stated_day_move(
        "Corning Tumbles 12% on Equity Offering; Coherent Sinks 11%, Lumentum Drops 9%"
    ) == -12.0


def test_a_year_to_date_figure_is_not_todays_move():
    assert stated_day_move("Okta Earnings, AI Offerings Drive Shares Up 94% YTD") is None


def test_a_growth_rate_is_not_a_price_move():
    assert stated_day_move(
        "Palo Alto (PANW) Reports 63% NGS ARR Growth but a $282M GAAP Net Loss"
    ) is None
    assert stated_day_move("Acme Revenue Rises 12% on Strong Demand") is None
    assert stated_day_move("Acme Margin Falls 5% in Q3") is None


def test_a_cause_clause_about_earnings_does_not_disqualify_a_price_move():
    """Why the check reads the words touching the figure, not the headline.

    "Shares Rise 8% on Strong Revenue" is a price move whose cause happens to
    be revenue. Scanning the whole headline for "revenue" threw these away.
    """
    assert stated_day_move("Acme Shares Rise 8% on Strong Revenue Growth") == 8.0
    assert stated_day_move("Acme Stock Drops 9% After Guidance Cut") == -9.0


def test_bare_up_needs_shares_or_stock_as_its_subject():
    """"Ramps up 20%" is not a price move; "Stock up 7%" is.

    Bare up/down carry no sense of price on their own, so they count only
    where the subject is named. Treating them as move verbs anywhere in a
    headline read capacity and buyback figures as share prices.
    """
    assert stated_day_move("Acme Ramps Up 20% of Capacity by 2027") is None
    assert stated_day_move("Acme Steps Up 15% Buyback Programme") is None
    assert stated_day_move("Acme Stock Up 7% After Deal") == 7.0
    assert stated_day_move("Acme Shares Down 6% on Recall") == -6.0


def test_a_headline_with_no_figure_claims_nothing():
    for headline in (
        "GLW Stock Drops Premarket As Corning Launches $2B Equity Offering",
        "Wells Fargo Maintains Overweight on Roblox, Raises Price Target to $64",
        "ASML reports transactions under its current share buyback program",
        "",
    ):
        assert stated_day_move(headline) is None


# --- deciding whether it contradicts us ------------------------------------


def test_a_figure_close_to_ours_is_the_same_story():
    """An intraday 12% against a 13.7% close needs no explaining."""
    assert conflicting_move("Corning Tumbles 12% on Equity Offering", -13.70) is None


def test_a_figure_far_from_ours_conflicts():
    assert conflicting_move(
        "CrowdStrike Shares Rise 4.6% After CEO Comments", 13.85
    ) == 4.6


def test_the_opposite_direction_always_conflicts():
    """Even a small figure pointing the wrong way reads as a contradiction."""
    assert conflicting_move("Acme Shares Rise 1% on Demand", -10.76) == 1.0


# --- what the reader ends up seeing ----------------------------------------


def test_a_conflicting_headline_is_annotated_not_dropped():
    """The news is still news. Only the number needed explaining."""
    bullet = Bullet(
        text="CrowdStrike Shares Rise 4.6% After CEO Comments on AI and Cybersecurity",
        url="https://example.test/1", score=5, kind="news", source="Yahoo",
    )
    out = flag_stale_moves({"CRWD": [bullet]}, [FakeMover("CRWD", 13.85)])
    (result,) = out["CRWD"]
    assert result.text == bullet.text
    assert "+4.6%" in result.note and "publisher" in result.note
    assert "+13.85%" in result.note


def test_an_agreeing_headline_is_left_clean():
    bullet = Bullet(
        text="Corning Tumbles 12% on $2B Equity Offering",
        url="", score=5, kind="news", source="Yahoo",
    )
    out = flag_stale_moves({"GLW": [bullet]}, [FakeMover("GLW", -13.70)])
    assert out["GLW"][0].note == ""


def test_filings_are_never_checked_against_the_move():
    """A filing is the company speaking, not a publisher quoting a price."""
    bullet = Bullet(
        text="10-K filed Sep 10 — annual report", url="", score=6, kind="filing",
    )
    out = flag_stale_moves({"PANW": [bullet]}, [FakeMover("PANW", 13.09)])
    assert out["PANW"][0].note == ""


def test_a_headline_is_quoted_and_credited():
    bullet = Bullet(
        text="Shares Rise 4.6% After CEO Comments", url="", score=5,
        kind="news", source="Yahoo",
    )
    assert _bullet_label(bullet) == 'Yahoo: "Shares Rise 4.6% After CEO Comments"'


def test_a_filing_stays_in_our_own_voice():
    bullet = Bullet(text="10-K filed Sep 10 — annual report", url="", score=6, kind="filing")
    assert _bullet_label(bullet) == "10-K filed Sep 10 — annual report"


def test_a_ticker_with_no_measured_move_is_passed_through():
    bullet = Bullet(text="Acme Shares Rise 4%", url="", score=5, kind="news", source="Yahoo")
    out = flag_stale_moves({"ACME": [bullet]}, [])
    assert out["ACME"][0].note == ""

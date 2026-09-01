"""Tests for the email command intake.

No network and no credentials: every test drives the pure planning half of
watchlist_agent.inbox over raw message strings.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from watchlist_agent.inbox import (
    Command,
    Outcome,
    check_guard_rails,
    parse_commands,
    plan_message,
    reply_body,
    strip_quoted,
)

SENDER = "reader@icloud.com"
HELD = ["AAPL", "ABNB", "ADBE", "AMD", "AMZN"]


def make_message(
    body: str = "",
    *,
    sender: str = SENDER,
    subject: str = "Re: Watchlist digest — Sep 01 — 3 movers, led by AAPL +2.1%",
    dmarc: str = "pass",
    message_id: str = "<abc123@mail.example>",
    html: str | None = None,
) -> str:
    message = EmailMessage()
    message["From"] = f"A Reader <{sender}>"
    message["To"] = "agent@gmail.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    if dmarc:
        message["Authentication-Results"] = (
            f"mx.google.com; spf=pass smtp.mailfrom={sender}; dmarc={dmarc} "
            'header.from="icloud.com"'
        )
    if html is None:
        message.set_content(body)
    else:
        message.set_content(body)
        message.add_alternative(html, subtype="html")
    return message.as_string()


def plan(body: str = "", **kwargs):
    return plan_message(make_message(body, **kwargs), HELD, SENDER)


# --- quote stripping -------------------------------------------------------
#
# The failure this guards against: a reply quotes a digest listing a dozen
# tickers, and every quoted line parses as a command.

QUOTED_DIGEST = """\
add NVDA

On Mon, Sep 1, 2026 at 4:30 PM Erimercium <agent@gmail.com> wrote:
> remove AAPL
> remove ABNB
> add TSLA
> AMZN   +4.1%
"""


def test_angle_bracket_quoting_is_cut():
    assert parse_commands("", QUOTED_DIGEST) == [Command("add", "NVDA")]


def test_gmail_attribution_line_is_cut():
    body = "remove ROKU\n\nOn Mon, Sep 1, 2026 at 4:30 PM Someone wrote:\nadd TSLA\n"
    assert parse_commands("", body) == [Command("remove", "ROKU")]


def test_wrapped_gmail_attribution_is_cut():
    """Gmail wraps a long attribution, leaving "wrote:" on the second line."""
    body = (
        "add NVDA\n"
        "On Mon, Sep 1, 2026 at 4:30 PM Erimercium Watchlist Agent\n"
        "<agent@gmail.com> wrote:\n"
        "add TSLA\n"
    )
    assert parse_commands("", body) == [Command("add", "NVDA")]


def test_outlook_original_message_separator_is_cut():
    body = "add NVDA\n\n-----Original Message-----\nFrom: agent\nadd TSLA\n"
    assert parse_commands("", body) == [Command("add", "NVDA")]


def test_signature_separator_is_cut():
    body = "add NVDA\n-- \nSent from somewhere\nadd TSLA\n"
    assert parse_commands("", body) == [Command("add", "NVDA")]


def test_strip_quoted_keeps_everything_when_nothing_is_quoted():
    assert strip_quoted("add NVDA\nremove ROKU") == "add NVDA\nremove ROKU"


# --- grammar ---------------------------------------------------------------


def test_command_in_subject():
    assert plan(subject="add NVDA").commands == [Command("add", "NVDA")]


def test_reply_prefix_stripped_from_subject():
    assert plan(subject="Re: Fwd: add NVDA").commands == [Command("add", "NVDA")]


def test_command_in_body():
    assert plan("add NVDA").commands == [Command("add", "NVDA")]


def test_multiple_tickers_on_one_line():
    assert parse_commands("", "add NVDA, PLTR") == [
        Command("add", "NVDA"),
        Command("add", "PLTR"),
    ]


def test_and_separator():
    assert parse_commands("", "add NVDA and PLTR") == [
        Command("add", "NVDA"),
        Command("add", "PLTR"),
    ]


@pytest.mark.parametrize("line", ["add NVDA", "Add NVDA", "ADD NVDA", "+NVDA"])
def test_add_spellings(line):
    assert parse_commands("", line) == [Command("add", "NVDA")]


@pytest.mark.parametrize("line", ["remove ROKU", "drop ROKU", "delete ROKU", "-ROKU"])
def test_remove_spellings(line):
    assert parse_commands("", line) == [Command("remove", "ROKU")]


@pytest.mark.parametrize("line", ["- ROKU", "+ NVDA"])
def test_sigil_must_bind_tight_to_its_ticker(line):
    """A dash with a space after it is a bullet, not a command.

    The digest writes its news bullets as "  - Apple names a new CFO", which
    would otherwise parse as removals when quoted back.
    """
    assert parse_commands("", line) == []


def test_crypto_pair_parses():
    assert parse_commands("", "add BTC-USD") == [Command("add", "BTC-USD")]
    assert parse_commands("", "-BTC-USD") == [Command("remove", "BTC-USD")]


def test_list_command():
    assert parse_commands("", "list") == [Command("list")]


def test_trailing_punctuation_tolerated():
    assert parse_commands("", "add NVDA.") == [Command("add", "NVDA")]
    assert parse_commands("", "please add NVDA") == [Command("add", "NVDA")]


def test_duplicate_across_subject_and_body_collapses():
    assert plan("add NVDA", subject="add NVDA").commands == [Command("add", "NVDA")]


def test_commands_apply_in_order():
    assert parse_commands("", "add NVDA\nremove ROKU\nadd PLTR") == [
        Command("add", "NVDA"),
        Command("remove", "ROKU"),
        Command("add", "PLTR"),
    ]


# --- prose must not mutate the watchlist -----------------------------------
#
# Precedence rule: a line is a command in full or not at all. Every token after
# the verb must be ticker-shaped, so a trailing word longer than five letters
# disqualifies the whole line rather than the token.


@pytest.mark.parametrize(
    "line",
    [
        "ADD SOME CONTEXT TO THE ADBE REPORT",
        "could you add NVDA when you get a chance",
        "I want to drop ROKU because it keeps moving",
        "Add more detail about margins",
        "remove the earnings section",
    ],
)
def test_prose_is_not_a_command(line):
    assert parse_commands("", line) == []


def test_digest_body_lines_are_not_commands():
    """The digest's own text must not read as commands if it comes back."""
    body = "  AAPL   +2.31%   moved 2.4 sigma\n  - Apple names a new CFO\n"
    assert parse_commands("", body) == []


def test_separator_rule_is_not_a_command():
    assert parse_commands("", "-" * 68) == []


def test_digest_news_bullet_is_not_a_command():
    """Every word here is ticker-shaped; only the punctuation rule saves it."""
    assert parse_commands("", "  - Apple names a new CFO") == []


def test_spaces_are_not_a_list_separator():
    """"remove the AAPL row" must not delete AAPL out of a sentence."""
    assert parse_commands("", "remove the AAPL row") == []
    assert parse_commands("", "add NVDA PLTR") == []


# --- sender verification ---------------------------------------------------


def test_wrong_sender_rejected():
    result = plan("add NVDA", sender="stranger@example.com")
    assert not result.authorized
    assert "not the authorized sender" in result.auth_reason
    assert result.commands == []


def test_dmarc_failure_rejected():
    result = plan("add NVDA", dmarc="fail")
    assert not result.authorized
    assert "DMARC" in result.auth_reason


def test_missing_authentication_results_rejected():
    """An absent verdict is not a pass."""
    result = plan("add NVDA", dmarc="")
    assert not result.authorized


def test_authorized_sender_accepted():
    assert plan("add NVDA").authorized


def test_sender_comparison_is_case_insensitive():
    assert plan("add NVDA", sender=SENDER.upper()).authorized


# --- guard rails -----------------------------------------------------------


def test_too_many_removals_refused():
    held = [f"TK{i:02d}" for i in range(20)]
    commands = [Command("remove", t) for t in held[:11]]
    assert "more than the 10" in check_guard_rails(commands, held)


def test_eleven_removals_of_untracked_tickers_is_not_refused():
    """Only removals that would actually take effect count against the cap."""
    commands = [Command("remove", f"ZZ{i:02d}") for i in range(11)]
    assert check_guard_rails(commands, HELD) == ""


def test_emptying_the_watchlist_refused():
    commands = [Command("remove", t) for t in HELD]
    assert "empty the watchlist" in check_guard_rails(commands, HELD)


def test_emptying_is_allowed_when_something_is_added_back():
    commands = [Command("remove", t) for t in HELD] + [Command("add", "NVDA")]
    assert check_guard_rails(commands, HELD) == ""


def test_too_many_tickers_refused():
    commands = [Command("add", f"TK{i:02d}") for i in range(26)]
    assert "more than the 25" in check_guard_rails(commands, HELD)


def test_ordinary_message_passes_guard_rails():
    assert check_guard_rails([Command("add", "NVDA")], HELD) == ""


# --- no command found ------------------------------------------------------


def test_unrecognised_message_yields_no_commands_and_does_not_raise():
    result = plan("Thanks, this is useful.", subject="Re: Watchlist digest")
    assert result.authorized
    assert result.commands == []
    assert result.refusal == ""


def test_empty_body_does_not_raise():
    assert plan("").commands == []


def test_html_only_body_is_parsed():
    message = EmailMessage()
    message["From"] = SENDER
    message["Subject"] = "Re: digest"
    message["Message-ID"] = "<x@y>"
    message["Authentication-Results"] = "mx.google.com; dmarc=pass"
    message.set_content("<p>add NVDA</p>", subtype="html")
    assert plan_message(message.as_string(), HELD, SENDER).commands == [
        Command("add", "NVDA")
    ]


def test_message_id_and_sender_carried_for_threading():
    result = plan("add NVDA")
    assert result.message_id == "<abc123@mail.example>"
    assert result.sender == SENDER


# --- replies ---------------------------------------------------------------


def test_bare_list_reply_shows_the_watchlist_not_the_help_text():
    result = plan("list")
    text, _ = reply_body(result, [], HELD)
    assert "could not find a command" not in text
    assert "holds 5 tickers" in text
    assert "AAPL, ABNB, ADBE, AMD, AMZN" in text


def test_unrecognised_message_gets_help_text():
    result = plan("Thanks, this is useful.")
    text, _ = reply_body(result, [], HELD)
    assert "could not find a command" in text
    assert "add NVDA" in text


def test_reply_states_each_outcome():
    result = plan("add NVDA\nremove ROKU\nadd ZZZZ")
    outcomes = [
        Outcome("NVDA", "added"),
        Outcome("ROKU", "absent"),
        Outcome("ZZZZ", "rejected", "no quote from Finnhub"),
    ]
    text, markup = reply_body(result, outcomes, HELD + ["NVDA"])
    assert "Added NVDA." in text
    assert "ROKU was not on the watchlist." in text
    assert "Could not add ZZZZ — no quote from Finnhub." in text
    assert "holds 6 tickers" in text
    assert "<div" in markup and "Added NVDA." in markup


def test_refusal_reply_makes_no_claim_of_change():
    result = plan("\n".join(f"remove {t}" for t in HELD))
    text, _ = reply_body(result, [], HELD, "the message would empty the watchlist")
    assert "No changes were made" in text
    assert "Removed" not in text


def test_html_reply_escapes_its_content():
    result = plan("add NVDA")
    _, markup = reply_body(result, [Outcome("NVDA", "added")], HELD)
    assert "<script>" not in markup

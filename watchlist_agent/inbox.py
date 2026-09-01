"""Watchlist changes requested by email reply.

The digest recipient replies to any erimercium email with `add NVDA` or
`remove ROKU`; a scheduled poll reads the mailbox, applies what it understands,
and answers in the same thread.

Three decisions here are load-bearing and none of them are obvious.

**Quoted text is cut before anything is parsed.** Mail clients quote the message
being replied to, and a digest quotes back a list of up to twelve tickers. Parse
the whole body and that quoted list reads as a batch of commands, so a one-line
reply rewrites the entire watchlist. Everything from the first quote marker
onward is discarded before a single command is read. This is the failure this
module is most likely to have, which is why the quote markers are enumerated
rather than guessed at.

**A command must occupy its whole line.** `add NVDA` is a command; `ADD SOME
CONTEXT TO THE ADBE REPORT` is not, because `CONTEXT` is not ticker-shaped and
the line therefore fails to parse as a unit. Anchoring both ends is what keeps
ordinary prose from mutating the watchlist. A partially valid line is rejected
outright rather than partially applied -- half-obeying a line nobody meant as a
command is worse than ignoring it.

**Recency, not unread status, decides what is examined.** The mailbox this polls
is a person's working inbox, not a dedicated robot account. A command read by a
human -- or merely touched by a preview pane -- before the poll runs is marked
`\\Seen` by Gmail, and a search for unread mail would then skip it forever. So
the poll looks at recent mail from the authorized sender regardless of read
state and relies on the processed-ID ledger for idempotency, which is what that
ledger was for. One exception: the "I did not understand that" reply is only
sent for mail that was still unread, so widening the window cannot produce a
burst of replies to messages already dealt with by hand.

**Identity is checked twice.** The From address must match the configured
sender, and Gmail's own `Authentication-Results` header must record a DMARC
pass. A From header is forgeable by anyone who knows the address; the DMARC
verdict is Gmail's, reached at delivery time, and cannot be set by the sender.
A message failing either check is left unread and never answered -- replying to
a forged sender only confirms the address is live.
"""

from __future__ import annotations

import argparse
import email
import html as html_module
import imaplib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from html import escape
from pathlib import Path

from .config import (
    IMAP_HOST,
    INBOX_LOOKBACK_DAYS,
    IMAP_SSL_PORT,
    MAX_REMOVALS_PER_MESSAGE,
    MAX_TICKERS_PER_MESSAGE,
    command_sender,
    gmail_address,
    gmail_app_password,
)
from .email_report import send_email
from .prices import fetch_quotes
from .watchlist import Watchlist

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "inbox_state.json"

# Keep the ledger from growing without bound. Anything this old has long since
# been answered; the flag on the message itself is the real duplicate guard.
LEDGER_LIMIT = 500

TICKER_RE = re.compile(r"^(?:[A-Z]{1,5}|[A-Z]{2,5}-USD)$")

# Where a reply stops being the reader's own words and starts being the message
# he replied to. Gmail and Apple Mail write "On <date> <someone> wrote:",
# Outlook writes a rule of underscores or "-----Original Message-----", and
# every client quotes with a leading ">". "-- " on its own line opens a
# signature, which is not quoted text but is equally not a command.
_QUOTE_MARKERS = (
    re.compile(r"^\s*>"),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^\s*_{5,}\s*$"),
    re.compile(r"^--\s*$"),
    re.compile(r"^\s*From:\s*.+@", re.IGNORECASE),
    re.compile(r"^\s*Sent from my \w+", re.IGNORECASE),
)
_WROTE_RE = re.compile(r"^\s*On\b.*\bwrote:\s*$", re.IGNORECASE)
_WROTE_OPENER_RE = re.compile(r"^\s*On\b.{0,200}$", re.IGNORECASE)
_WROTE_TAIL_RE = re.compile(r".*\bwrote:\s*$", re.IGNORECASE)

_VERBS = {
    "add": "add",
    "include": "add",
    "remove": "remove",
    "drop": "remove",
    "delete": "remove",
}

_COMMAND_RE = re.compile(
    r"^\s*(?:please\s+)?(?P<verb>add|include|remove|drop|delete)\s+(?P<args>\S.*?)"
    r"\s*[.!]?\s*$",
    re.IGNORECASE,
)
_SIGIL_RE = re.compile(r"^\s*(?P<sign>[+-])(?P<args>[A-Za-z].*?)\s*[.!]?\s*$")
_LIST_RE = re.compile(
    r"^\s*(?:please\s+)?(?:list|show)(?:\s+(?:the\s+)?watchlist)?\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fwd|fw)\s*:\s*)+", re.IGNORECASE)


@dataclass(frozen=True)
class Command:
    action: str  # "add", "remove" or "list"
    ticker: str = ""


@dataclass
class Plan:
    """What a single message asks for, decided without touching the network."""

    message_id: str = ""
    subject: str = ""
    sender: str = ""
    authorized: bool = False
    auth_reason: str = ""
    commands: list[Command] = field(default_factory=list)
    refusal: str = ""

    @property
    def actionable(self) -> bool:
        return self.authorized and not self.refusal and bool(self.commands)


# --- parsing ---------------------------------------------------------------


def header_text(message: Message, name: str) -> str:
    """A header as readable text, with RFC 2047 encoding undone."""
    raw = message.get(name)
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw.strip()


def _html_to_text(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html_module.unescape(text)


def body_text(message: Message) -> str:
    """The reader's own words, preferring the plain-text alternative.

    HTML is only fallen back to when a client sent nothing else; tag-stripped
    markup is noisier to parse, and the line structure commands depend on
    survives far better in text/plain.
    """
    plain: list[str] = []
    markup: list[str] = []

    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        (plain if ctype == "text/plain" else markup).append(decoded)

    if plain:
        return "\n".join(plain)
    return _html_to_text("\n".join(markup))


def strip_quoted(text: str) -> str:
    """Discard everything from the first quote marker onward.

    See the module docstring: without this a reply that quotes a digest listing
    twelve tickers parses those tickers as commands.
    """
    kept: list[str] = []
    lines = text.splitlines()

    for i, line in enumerate(lines):
        if any(marker.search(line) for marker in _QUOTE_MARKERS):
            break
        if _WROTE_RE.match(line):
            break
        # Gmail wraps a long attribution across two lines, leaving "wrote:" on
        # the second. Match the pair so the quoted body below it is still cut.
        if _WROTE_OPENER_RE.match(line) and i + 1 < len(lines):
            if _WROTE_TAIL_RE.match(lines[i + 1]):
                break
        kept.append(line)

    return "\n".join(kept)


def parse_tickers(chunk: str) -> list[str] | None:
    """Every item in ``chunk`` as a ticker, or None if any of it is not one.

    All-or-nothing on purpose, and a bare space is not a list separator.
    English is full of five-letter-or-shorter words, so "remove the AAPL row"
    splits into three perfectly ticker-shaped tokens and would delete a real
    holding out of a sentence nobody meant as a command. Requiring a comma or
    "and" between tickers draws the line where a writer's intent actually
    changes: lists get punctuation, prose does not.
    """
    parts = [p.strip() for p in re.split(r",|\band\b", chunk, flags=re.IGNORECASE)]
    parts = [p for p in parts if p]
    if not parts:
        return None

    tickers: list[str] = []
    for part in parts:
        if re.search(r"\s", part):
            return None
        candidate = part.strip(".,;:!?()[]").upper()
        if not TICKER_RE.match(candidate):
            return None
        tickers.append(candidate)
    return tickers


def parse_line(line: str) -> list[Command]:
    """Commands on one line. A line is a command in full, or not at all."""
    if _LIST_RE.match(line):
        return [Command("list")]

    match = _COMMAND_RE.match(line)
    if match:
        action = _VERBS[match.group("verb").lower()]
        tickers = parse_tickers(match.group("args"))
        if tickers:
            return [Command(action, t) for t in tickers]
        return []

    match = _SIGIL_RE.match(line)
    if match:
        action = "add" if match.group("sign") == "+" else "remove"
        tickers = parse_tickers(match.group("args"))
        if tickers:
            return [Command(action, t) for t in tickers]
    return []


def parse_commands(subject: str, body: str) -> list[Command]:
    """Commands from the subject line first, then the unquoted body.

    Duplicates are collapsed: a reader who puts the same request in both the
    subject and the body means it once, and answering it twice reads like a
    malfunction.
    """
    found: list[Command] = []
    seen: set[Command] = set()

    subject = _SUBJECT_PREFIX_RE.sub("", subject or "")
    sources = [subject] + strip_quoted(body or "").splitlines()

    for line in sources:
        for command in parse_line(line):
            if command not in seen:
                seen.add(command)
                found.append(command)
    return found


# --- sender verification ---------------------------------------------------


def is_unread(fetch_envelope: bytes | str) -> bool:
    """Whether a FETCH response's FLAGS say the message is still unread.

    The envelope looks like ``1 (FLAGS (\\Seen) BODY[] {2048}``. Only the
    absence of ``\\Seen`` counts as unread; an envelope that could not be read
    is treated as already-read, which is the conservative side -- it suppresses
    a help reply rather than sending a spurious one.
    """
    if isinstance(fetch_envelope, bytes):
        fetch_envelope = fetch_envelope.decode(errors="replace")
    return "\\Seen" not in fetch_envelope


def dmarc_passed(message: Message) -> bool:
    """Whether Gmail recorded a DMARC pass when it accepted the message.

    Gmail stamps this itself on delivery, so unlike the From header it is not
    something a sender can assert. Absent header means absent verdict, which is
    treated as a failure: a message that arrived without Gmail's own
    authentication result is not one to act on.
    """
    for raw in message.get_all("Authentication-Results") or []:
        if re.search(r"\bdmarc\s*=\s*pass\b", raw, re.IGNORECASE):
            return True
    return False


def check_sender(message: Message, expected: str) -> tuple[bool, str]:
    _, address = parseaddr(header_text(message, "From"))
    address = address.strip().lower()
    if address != expected.strip().lower():
        return False, f"From {address or '(none)'} is not the authorized sender"
    if not dmarc_passed(message):
        return False, f"no DMARC pass recorded for {address}"
    return True, ""


# --- guard rails -----------------------------------------------------------


def check_guard_rails(commands: list[Command], current: list[str]) -> str:
    """A refusal reason, or "" when the message is safe to apply whole.

    Refusal is all-or-nothing. A message big enough to trip a guard rail is
    more likely to be a forwarded thread parsed by accident than a considered
    request, and applying the first half of one would be the worst outcome.
    """
    tickers = [c for c in commands if c.ticker]
    if len(tickers) > MAX_TICKERS_PER_MESSAGE:
        return (
            f"the message names {len(tickers)} tickers, more than the "
            f"{MAX_TICKERS_PER_MESSAGE} allowed in one email"
        )

    held = {t.upper() for t in current}
    removals = {c.ticker for c in commands if c.action == "remove" and c.ticker in held}
    if len(removals) > MAX_REMOVALS_PER_MESSAGE:
        return (
            f"the message removes {len(removals)} tickers, more than the "
            f"{MAX_REMOVALS_PER_MESSAGE} allowed in one email"
        )

    additions = {c.ticker for c in commands if c.action == "add"} - held
    if held and not (held - removals) and not additions:
        return "the message would empty the watchlist"
    return ""


def plan_message(raw: str | bytes, current: list[str], expected_sender: str) -> Plan:
    """Decide what a raw message asks for. Pure: no network, no mutation."""
    message = (
        email.message_from_bytes(raw)
        if isinstance(raw, bytes)
        else email.message_from_string(raw)
    )

    plan = Plan(
        message_id=header_text(message, "Message-ID"),
        subject=header_text(message, "Subject"),
        sender=parseaddr(header_text(message, "From"))[1],
    )
    plan.authorized, plan.auth_reason = check_sender(message, expected_sender)
    if not plan.authorized:
        return plan

    plan.commands = parse_commands(plan.subject, body_text(message))
    if plan.commands:
        plan.refusal = check_guard_rails(plan.commands, current)
    return plan


# --- applying --------------------------------------------------------------


@dataclass
class Outcome:
    ticker: str
    result: str  # added | present | removed | absent | rejected
    detail: str = ""


def validate_additions(tickers: list[str]) -> dict[str, str]:
    """Reasons the given tickers cannot be priced, keyed by ticker.

    A symbol that cannot be priced would otherwise become a permanent entry in
    the digest's "could not price" list, which nobody reads as an error. Better
    to refuse it at the door and say why.
    """
    if not tickers:
        return {}
    _, failures = fetch_quotes(tickers)
    return {f.ticker.upper(): f.reason for f in failures}


def apply_commands(commands: list[Command], watchlist: Watchlist) -> list[Outcome]:
    held = {t.upper() for t in watchlist.tickers}
    pending = [c.ticker for c in commands if c.action == "add" and c.ticker not in held]
    unpriceable = validate_additions(sorted(set(pending)))

    outcomes: list[Outcome] = []
    for command in commands:
        if command.action == "list":
            continue
        if command.action == "add":
            if command.ticker in unpriceable:
                outcomes.append(
                    Outcome(command.ticker, "rejected", unpriceable[command.ticker])
                )
                log.info("rejected %s: %s", command.ticker, unpriceable[command.ticker])
                continue
            added = watchlist.add(command.ticker)
            outcomes.append(Outcome(command.ticker, "added" if added else "present"))
            log.info("%s %s", "added" if added else "already held", command.ticker)
        else:
            removed = watchlist.remove(command.ticker)
            outcomes.append(Outcome(command.ticker, "removed" if removed else "absent"))
            log.info("%s %s", "removed" if removed else "not held", command.ticker)
    return outcomes


# --- replying --------------------------------------------------------------

_RESULT_TEXT = {
    "added": "Added {t}.",
    "present": "{t} was already on the watchlist.",
    "removed": "Removed {t}.",
    "absent": "{t} was not on the watchlist.",
    "rejected": "Could not add {t} — {d}.",
}

HELP_TEXT = (
    "I could not find a command in that message. Put one on a line of its own:\n"
    "\n"
    "    add NVDA\n"
    "    add NVDA, PLTR\n"
    "    remove ROKU\n"
    "    list\n"
    "\n"
    'The whole line has to be the command, so "add NVDA" works but "could you '
    'add NVDA when you get a chance" does not. Separate several tickers with a '
    "comma."
)


def reply_body(
    plan: Plan, outcomes: list[Outcome], tickers: list[str], refused: str = ""
) -> tuple[str, str]:
    """The confirmation, as (plain text, html)."""
    lines: list[str] = []

    if refused:
        lines.append(f"No changes were made: {refused}.")
        lines.append("")
        lines.append("Send the change in smaller batches and it will go through.")
    elif outcomes:
        for outcome in outcomes:
            lines.append(
                _RESULT_TEXT[outcome.result].format(t=outcome.ticker, d=outcome.detail)
            )
    elif not plan.commands:
        # Only a message with nothing recognisable in it gets the help text. A
        # bare "list" produces no outcomes but is perfectly well understood.
        lines.append(HELP_TEXT)

    wants_list = any(c.action == "list" for c in plan.commands)
    if not refused and (wants_list or outcomes):
        lines.append("")
        lines.append(f"The watchlist now holds {len(tickers)} tickers.")
    if wants_list:
        lines.append("")
        lines.append(", ".join(tickers))

    text = "\n".join(lines)
    body = escape(text).replace("\n", "<br>")
    markup = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:14px;line-height:1.55;color:#111827;max-width:640px;">'
        f"<p style=\"margin:0;\">{body}</p></div>"
    )
    return text, markup


def send_reply(plan: Plan, text: str, html: str, dry_run: bool) -> None:
    subject = plan.subject or "Watchlist"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    if dry_run:
        print(f"--- reply to {plan.sender} ---\nSubject: {subject}\n\n{text}\n")
        return

    send_email(
        subject,
        text,
        html,
        to=plan.sender,
        in_reply_to=plan.message_id or None,
        references=plan.message_id or None,
    )


# --- the processed ledger --------------------------------------------------


class Ledger:
    """Message-IDs already dealt with, committed back between runs.

    This is the primary duplicate guard, not a supplement to the IMAP flags.
    The poll searches by recency rather than read state, so the same message is
    returned by every search inside the lookback window; only this ledger stops
    it being answered again each time. GitHub Actions runs share no storage,
    which is why it is committed back to the repository.
    """

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        if path.exists():
            self._doc = json.loads(path.read_text())
        else:
            self._doc = {
                "_comment": (
                    "Message-IDs of watchlist command emails already handled, so "
                    "a message left unread is not re-examined on every poll."
                ),
                "processed": {},
            }

    def seen(self, message_id: str) -> bool:
        return bool(message_id) and message_id in self._doc["processed"]

    def record(self, message_id: str, outcome: str) -> None:
        if not message_id:
            return
        self._doc["processed"][message_id] = {
            "at": date.today().isoformat(),
            "outcome": outcome,
        }

    def save(self) -> None:
        entries = self._doc["processed"]
        if len(entries) > LEDGER_LIMIT:
            newest = sorted(entries.items(), key=lambda kv: kv[1]["at"], reverse=True)
            self._doc["processed"] = dict(newest[:LEDGER_LIMIT])
        self.path.write_text(json.dumps(self._doc, indent=2, sort_keys=True) + "\n")


# --- the run ---------------------------------------------------------------


def process_mailbox(dry_run: bool = False) -> int:
    ledger = Ledger()
    watchlist = Watchlist()
    handled = 0

    sender = command_sender()
    since = (date.today() - timedelta(days=INBOX_LOOKBACK_DAYS)).strftime("%d-%b-%Y")

    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_SSL_PORT) as imap:
        imap.login(gmail_address(), gmail_app_password())
        imap.select("INBOX")
        # Read state is deliberately not part of this query -- see the module
        # docstring. Narrowing to the one authorized sender keeps the window
        # small even though it spans days rather than "since last read".
        typ, data = imap.search(None, "FROM", f'"{sender}"', "SINCE", since)
        if typ != "OK":
            log.warning("IMAP search failed: %s", typ)
            return 0

        numbers = data[0].split()
        log.info("%d message(s) from %s since %s", len(numbers), sender, since)

        for number in numbers:
            # PEEK, so examining a message we will not act on does not silently
            # mark it read and hide it from the person who has to look at it.
            typ, payload = imap.fetch(number, "(FLAGS BODY.PEEK[])")
            if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                log.warning("could not fetch message %s", number.decode())
                continue

            was_unread = is_unread(payload[0][0])
            plan = plan_message(payload[0][1], watchlist.tickers, sender)
            if ledger.seen(plan.message_id):
                continue

            if not plan.authorized:
                log.warning("ignoring message: %s", plan.auth_reason)
                ledger.record(plan.message_id, "unauthorized")
                continue

            if plan.refusal:
                text, markup = reply_body(plan, [], watchlist.tickers, plan.refusal)
                log.warning("refused message from %s: %s", plan.sender, plan.refusal)
            elif plan.commands:
                outcomes = apply_commands(plan.commands, watchlist)
                text, markup = reply_body(plan, outcomes, watchlist.tickers)
            elif was_unread:
                text, markup = reply_body(plan, [], watchlist.tickers)
                log.info("no command found in message from %s", plan.sender)
            else:
                # Already-read mail with no command is ordinary correspondence
                # the reader has dealt with. Answering it because the search
                # window widened would be noise, not help.
                log.info("skipping read message with no command")
                if not dry_run:
                    ledger.record(plan.message_id, "no-command")
                continue

            send_reply(plan, text, markup, dry_run)
            handled += 1

            if not dry_run:
                ledger.record(
                    plan.message_id,
                    "refused" if plan.refusal else "applied" if plan.commands else "help",
                )
                imap.store(number, "+FLAGS", "\\Seen")

    if not dry_run:
        ledger.save()
    log.info("handled %d message(s)", handled)
    return handled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply watchlist changes sent by email.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print replies instead of sending them, and leave messages unread.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    process_mailbox(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

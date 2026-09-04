# erimercium — stock watchlist agent

A daily email digest over a watchlist of US-listed tickers, plus a chat interface
through Claude Code for managing the list and researching individual names.

No portfolio, no share counts, no cost basis, no P&L. Just a list of tickers and
what is happening to them.

## Status

| Stage | Scope | State |
|---|---|---|
| 1 | Price digest — per-ticker move detection, email at 4:30pm ET | **Done** |
| 2 | Company news + SEC filings (10-Q / 10-K / 8-K), materiality-filtered | **Done** |
| 3 | Research reports: baseline, pre-earnings, event-triggered | **Done** |
| 4 | Formatting, source links, filter tuning | Not started |
| 5 | Watchlist changes by email reply | **Done** |
| 6 | Earnings call take-aways, paired with the pre-earnings report | **Done** |

## Setup

### 1. Secrets

Add these under **Settings → Secrets and variables → Actions**:

| Secret | Where it comes from |
|---|---|
| `FINNHUB_API_KEY` | Free key from <https://finnhub.io/register> |
| `GMAIL_ADDRESS` | The Gmail account that sends the digest |
| `GMAIL_APP_PASSWORD` | 16-character app password from <https://myaccount.google.com/apppasswords> (requires 2-Step Verification) |

Google revokes all app passwords whenever the account password changes. If the
digest silently stops arriving, check that first.

The same app password is used to *read* replies over IMAP, so **IMAP must be
enabled** on that account (Gmail → Settings → Forwarding and POP/IMAP → Enable
IMAP). App passwords authenticate fine with IMAP switched off, and the mailbox
job then fails on every poll with a login error.

Recipient defaults to `christian.na@icloud.com`. Override with a repository
variable named `DIGEST_RECIPIENT`.

### 2. Local runs

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in real values; .env is gitignored
set -a && . ./.env && set +a

python -m watchlist_agent.digest --force --dry-run   # print, do not send
python -m watchlist_agent.digest --force             # actually send

python -m watchlist_agent.inbox --dry-run            # read mail, print replies
python -m watchlist_agent.inbox                      # apply and reply
```

Tests need no key and no network:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

`--force` bypasses the time-window gate; `--dry-run` prints the digest instead
of emailing it.

## The watchlist

`watchlist.json` holds tickers and the flagging rules, nothing else:

```json
{
  "thresholds": {
    "z_score": 2.0,
    "min_abs_pct": 1.5,
    "always_flag_abs_pct": 8.0,
    "fallback_pct": 3.0,
    "max_shown": 12
  },
  "tickers": ["AAPL", "ABNB", "..."]
}
```

Edit it directly, ask Claude Code (`add NVDA`, `drop ROKU`), or reply to any
digest or research email with the same commands -- see below.

Symbols ending in `-USD` (`BTC-USD`, `ETH-USD`, `XRP-USD`) are treated as
Coinbase product IDs and priced from Coinbase's free public candles API, because
Finnhub's free tier `/quote` endpoint covers equities only. Everything else goes
to Finnhub.

Any ticker that cannot be priced is reported in a **Could not price** section of
the digest rather than failing the run — that is the feedback loop for finding
delisted, renamed, or uncovered symbols.

## Changing the watchlist by email

Reply to any erimercium email with a command on a line of its own:

```
add NVDA              remove ROKU           list
+NVDA                 drop ROKU
add NVDA, PLTR        -ROKU
```

A poll every 15 minutes reads the mailbox over IMAP, applies what it
understands, commits `watchlist.json` back, and answers in the same thread
saying exactly what changed. Turnaround is therefore up to about a quarter of
an hour, not instant.

**The poll reads Gmail's archive, not the inbox.** IMAP's INBOX is not a place
in Gmail, it is a label. Archiving a message -- by hand, by a filter, or by the
mobile app's swipe -- removes that label, and the message disappears from INBOX
while still existing in the archive. On the first live test the reader watched a
command land in the inbox and then vanish, and three consecutive polls of INBOX
found nothing. The poll now selects the folder advertising the `\All`
special-use flag, located by flag rather than by name because the name is
localised. Gmail excludes Spam and Trash from that folder, so a command
filtered as spam is still invisible -- no folder sees everything.

**Read state does not decide what gets examined.** The mailbox is the account
the digests are sent *from*, which is a person's working inbox rather than a
dedicated robot account. The first version searched for unread mail, and a
command read by a human -- or merely touched by a preview pane -- before the
poll ran was skipped permanently. That is exactly what happened on the first
live test. The poll now looks at mail from the authorized sender within the
last `INBOX_LOOKBACK_DAYS` days regardless of whether it has been read, and
leans on the processed-ID ledger for idempotency.

Read state still gates one thing: the "I did not understand that" reply is only
sent for mail that was still unread. Widening the search window therefore
cannot produce a burst of replies to correspondence already handled by hand.

**A sentence works too.** The commands above are read first, for nothing and
instantly. Anything they cannot read is handed to a model, which extracts the
same three intents from ordinary English and resolves company names to tickers:

```
Add oracle and send me a list of my current stocks
```

is read as `add ORCL` plus `list`. That is a real message -- it is the one that
prompted this, and under the strict grammar alone it got a help reply, because
"oracle" is a company name rather than a ticker and the sentence carries two
requests at once. Both are ordinary English; asking the reader to write like a
command line when the whole point is that he can just reply to an email gets
the trade backwards.

Three things keep it bounded. It is a **fallback**, so a well-formed command
never reaches a model and the common path stays free. It **resolves rather
than acts** -- every guard below still applies to what it returns, additions
are still priced before they are accepted, and the reply opens with *"I read
that as: add ORCL, send the watchlist"* so a misread is visible rather than
silent. And it is **allowed to say it does not know**: a company it cannot
place is named back to the reader instead of guessed at, because a wrong symbol
ends up on the watchlist and an admission does not.

Reading a message costs a fraction of a cent, and only unread mail is ever
handed to the model -- a read message with no command is correspondence already
dealt with by hand. Without `ANTHROPIC_API_KEY` the job still runs; plain
English simply gets the help reply instead.

### The strict grammar

**A command must be its whole line.** `add NVDA` is a command; `could you add
NVDA when you get a chance` is not. Anchoring both ends is what stops ordinary
prose from editing the watchlist, and a line that is only partly valid is
rejected rather than half-applied.

**Several tickers need a comma or `and` between them.** `add NVDA, PLTR` works;
`add NVDA PLTR` does not. This looks fussy until you count how many English
words are five letters or shorter: `remove the AAPL row` splits into three
perfectly ticker-shaped tokens, and under a space-separated rule it deletes a
real holding out of a sentence nobody meant as a command. The first version of
this parser did exactly that to `- Apple names a new CFO`, a line the digest
writes itself. Punctuation is where a writer's intent actually changes -- lists
get it, prose does not.

**Quoted text is cut before parsing.** Mail clients quote the message being
replied to, and a digest quotes back up to twelve tickers. Everything from the
first quote marker onward -- a leading `>`, Gmail's `On ... wrote:` (including
the wrapped two-line form), Outlook's `-----Original Message-----`, a `-- `
signature rule -- is discarded before a single command is read. Without this,
one-line replies rewrite the whole watchlist.

**Additions are priced before they are accepted.** A symbol that cannot be
quoted is refused, with the reason in the reply, rather than becoming a
permanent entry in the digest's "could not price" section that nobody reads as
an error. Removals are not price-checked: a ticker already on the list is
removable whether or not its data source still answers.

### Who is allowed to send commands

Two checks, both required:

1. The `From` address matches `COMMAND_SENDER` (defaults to
   `DIGEST_RECIPIENT`).
2. Gmail's own `Authentication-Results` header records `dmarc=pass`.

The second check is the one doing the work. A `From` header is forgeable by
anyone who learns the address; the DMARC verdict is Gmail's, reached when it
accepted the message, and a sender cannot assert it. A missing header is
treated as a failure rather than a pass.

A message failing either check is left unread, logged at `WARNING`, and **not
answered**. Replying would confirm to a forger that the address is live.

### Guard rails

| Rule | Why |
|---|---|
| At most 25 tickers per message | A message this large is more likely a forwarded thread parsed by accident than a considered request |
| At most 10 effective removals per message | Same, on the destructive side |
| Never leave the watchlist empty | No single email should be able to switch the whole system off |

Tripping a guard rail refuses the message **whole** and says so in the reply.
Applying the first half of a message that was never meant as a command is the
worst available outcome.

### Duplicates

`inbox_state.json` records the `Message-ID`s already handled and is committed
back like `reports_sent.json`, since Actions runs share no storage. This is the
primary duplicate guard rather than a supplement to the IMAP flags: because the
poll searches by recency, the same message comes back from every search inside
the lookback window, and only the ledger stops it being answered each time.
Handled messages are still flagged `\Seen` as well.

One limit is accepted rather than engineered away: if the git push fails after
a reply has already gone out, the next poll re-reads that message and sends a
second confirmation. `add` and `remove` are idempotent, so the watchlist stays
correct and only the confirmation repeats. The push step fails the job loudly
instead.

## What counts as a move worth reporting

A flat percentage bar is the wrong shape for a list this varied. AMZN moving 4%
is a major event; RGTI moving 5% is a Tuesday. A single global threshold has to
choose which of those to get wrong — tuned high enough to silence the
speculative names, it goes deaf to the megacaps. On the first live run, 33 of
100 tickers cleared a flat 3%, which is a wall of text rather than a digest.

So each ticker is measured against its own history. `volatility.py` computes the
standard deviation of that ticker's daily returns over the trailing 60 trading
days, and a move is flagged when it is at least `z_score` of those standard
deviations. Three guards keep the rule honest at the edges:

| Guard | Why |
|---|---|
| `z_score` (2.0) | The move must be unusual *for this stock* |
| `min_abs_pct` (1.5%) | A statistically odd but trivial wobble in a very steady name is not news |
| `always_flag_abs_pct` (8.0%) | A genuinely large move is always reported, even in a name volatile enough to make it unremarkable statistically |
| `fallback_pct` (3.0%) | Used for any ticker whose history could not be fetched |

"Typical daily move" is the **median absolute deviation** of the trailing
returns, scaled by 1.4826, not their standard deviation. That choice is not
cosmetic. Every stock has one earnings gap per quarter, and inside a 60-day
window a single gap inflates a standard deviation enough to make the stock
unflaggable until it rolls off:

| Ticker | Outlier in window | Plain stdev | Robust (MAD) |
|---|---|---|---|
| AMZN | +15.32% earnings, 2026-07-31 | 2.83% | 1.95% |
| MRNA | +176.97% corporate action, 2026-08-19 | 23.62% | 4.81% |
| SNDK | none — genuinely volatile | 8.83% | 8.91% |

Under plain stdev, AMZN's +3.97% scored 1.40σ and was ignored; under MAD it is
2.04σ and reported. MRNA was unflaggable at any realistic threshold. SNDK shows
the estimator does not over-tighten a name that really is that volatile.

Note that Yahoo's `adjclose` is byte-identical to `close` for these tickers, so
adjusted prices do **not** rescue the MRNA case — only a robust estimator does.

Finnhub's free tier does not serve historical candles (`/stock/candle` returns
403), so daily closes come from Yahoo's chart API for equities and Coinbase for
crypto. Both are free and keyless. A ticker whose history cannot be fetched
falls back to `fallback_pct` rather than being dropped.

Stooq was tried first and does not work from CI: it answers a plain HTTP client
with 404, and a browser user-agent with a JavaScript bot-check page. When it
failed for all 99 tickers the run still went green and the digest quietly
reverted to the flat threshold — so when volatility coverage drops below 50%,
the digest now carries a visible warning and the job logs a `WARNING`. A data
source going dark should not look like an ordinary day.

`tools/probe_history.py` (run via the **Probe history sources** workflow) checks
which sources are reachable from a runner; the development sandbox has no
outbound network, so source reachability can only be tested from CI.

## Scheduling and daylight saving

The digest targets **4:30pm ET every weekday**, just after the US close.

GitHub Actions cron is UTC-only with no DST awareness, so the workflow registers
both offsets:

- `30 20 * * 1-5` → 4:30pm EDT (March–November)
- `30 21 * * 1-5` → 4:30pm EST (November–March)

Both fire year round, so the job discards the off-season one. It does that by
comparing **which cron expression triggered the run** (`github.event.schedule`)
against the current `America/New_York` UTC offset — not by checking the clock
when the runner starts.

That distinction is load-bearing. GitHub delays scheduled workflows on shared
runners, and this repo has already seen an 81-minute delay: on 2026-08-28 both
entries were queued for 20:30 and 21:30 UTC and both actually started at 21:51
UTC. A wall-clock window would have thrown away the correct run and sent
nothing that day. Keying off the triggering cron means the in-season entry
sends however late it starts, and the off-season entry never does.

## Layout

```
watchlist.json                     tickers + move threshold
watchlist_agent/
  config.py                        env/secrets, ET scheduling gate
  watchlist.py                     load/mutate watchlist.json
  prices.py                        Finnhub quotes, Coinbase crypto, rate limiting
  volatility.py                    per-ticker daily-return sigma from Stooq/Coinbase
  movers.py                        which moves clear the bar
  news.py                          Finnhub company news
  filings.py                       SEC EDGAR submissions, ticker -> CIK
  materiality.py                   what is worth reading
  research.py                      per-ticker gathering
  email_report.py                  text + HTML rendering, Gmail SMTP
  digest.py                        entry point
  inbox.py                         watchlist commands sent by email reply
.github/workflows/daily-digest.yml schedule + manual dispatch
.github/workflows/inbox.yml        mailbox poll for watchlist commands
tests/                             pytest suite (python -m pytest)
```

## News and filings

For each **flagged** ticker only, the digest gathers recent company news
(Finnhub) and recent SEC filings (EDGAR), and lists what survives filtering
underneath the move that prompted it. Researching all 100 names daily would
mean ~200 requests to answer a question nobody asked about the ~90 that did
nothing.

**Filings are classified, not keyword-matched.** 10-K and 10-Q always count. An
8-K counts only if its item codes say something — 5.02 (officer departure),
4.02 (financials cannot be relied upon), 2.02 (results), 1.01 (material
agreement) and similar. Routine Reg FD and shareholder-vote 8-Ks are dropped.
This is why the digest uses EDGAR's **submissions** feed rather than full-text
search: full-text search answers "which filings contain this phrase" and needs
a query term, so it cannot enumerate a company's recent filings. The
submissions feed lists form types and 8-K item codes directly.

**News is keyword-filtered**, and this is the part weakened by running without
an LLM. A headline passes three checks:

1. **Format** — dropped if it matches a never-material shape (listicles,
   "here's why the stock is moving", market wraps, Cramer).
2. **Attribution** — dropped unless it actually names the company, by ticker or
   by the distinctive part of its registered name. Finnhub's feed returns
   anything *mentioning* a symbol, including wire roundups covering a dozen
   tickers; the first live run filed a Rivian CFO resignation under AMZN and a
   QFIN earnings miss under IREN. Ticker matching is case-sensitive and needs
   three characters, since `F`, `Q`, `ON`, `BE` and `GE` are ordinary words.
3. **Subject** — kept either way, but demoted if the company is not named in
   the opening words. Headlines lead with their subject, so "NVIDIA CFO Says Co
   & Amazon Web Services To Deploy..." is Nvidia news that mentions Amazon.
   Demoting rather than dropping means a bystander mention still surfaces when
   nothing better exists.

The filter judges shape, vocabulary and position — not substance — so it will
pass some noise and miss some unusually worded stories. Bullets are the
headline itself with a source link, not a synthesised takeaway; writing "the
major takeaway" needs a model reading the article.

Tickers with no US filings (foreign private issuers like `ASML` and `BABA`,
ETFs like `FBTC`, OTC symbols like `HYMLF`) simply have no CIK in EDGAR and are
skipped; crypto pairs have neither news feed nor filings.

## Research reports

Reports are generated on three triggers, not on price moves:

| Trigger | When |
|---|---|
| **Baseline** | Once per holding, ever. Batched a few per run rather than 100 at once. |
| **Pre-earnings** | 7 days before a company reports, from Finnhub's free earnings calendar |
| **Event** | An 8-K whose item codes say the picture changed |

Price moves deliberately do not trigger a report. Move-triggering is unbounded
— one volatile session flagged 33 of 100 tickers — and it fires after the fact.
Earnings-triggering is predictable (~400/year) and lands while the information
is still actionable.

**Two scheduling guarantees**, both tested against the awkward cases:

- Earnings reports are keyed by **fiscal period** (`2026Q3`), never by date.
  Companies reschedule constantly; keying on the date would send a second
  report for the same quarter every time one moved.
- The lead window is **one-sided**, so a date that slips out of range is caught
  again when it returns rather than being skipped.

State lives in `reports_sent.json`, committed back between runs since Actions
runs share no storage. Event triggers key on SEC accession number, so a
re-examined filing cannot produce a duplicate.

**Event triggers read 8-K item codes, not headlines.** A CEO departure is Item
5.02 whether or not anyone wrote about it, and filing it is mandatory. Item
2.02 (results of operations) is deliberately excluded — earnings are already
covered by the scheduled cadence, and including it would fire a duplicate
report every quarter for every holding.

### The record against consensus

Every report carries the last four quarters of reported EPS against what
consensus expected, from Finnhub's free `/stock/earnings` endpoint, with one
line naming the pattern.

**The line never reports a bare count, and that is the point.** Companies guide
conservatively and most large caps beat EPS consensus most quarters, so "beat 4
of 4" is roughly the market's base rate rather than a finding about this
company. What distinguishes one name from another is the size of the surprises,
whether they are consistent or scattered, and which way they are trending -- so
the line says *"beat consensus in all 4, but by a narrowing margin (averaging
+1.6%)"* or *"2 beats and 2 misses, ranging from -9.7% to +12.4%"*.

The percentage is withheld where the estimate is too small to bear one: a
two-cent beat on a one-cent estimate is +200% and says more about the
denominator than the company, so below a nickel the difference is reported in
cents instead. A quarter with no published estimate is excluded rather than
counted as a miss -- an absence is not a disappointment.

The table is rendered from the data rather than written by the model, so the
figures cannot drift.

### What a report is built from

Everything except the writing runs on free public data and needs no API key:

| Source | Supplies |
|---|---|
| SEC XBRL company facts | Quarterly revenue, margin and leverage trends |
| Finnhub basic financials | P/E, P/S, P/B, margins, 52-week range, beta |
| Finnhub peers | Peer set, with P/E fetched for each |
| Finnhub earnings calendar | Date, consensus EPS and revenue estimates |
| Finnhub recommendation trends | Analyst rating mix and its direction |
| SEC submissions | Recent filings, classified by form and item code |
| Finnhub company news | Filtered headlines with source links |

XBRL is inconsistent across filers — NKE does not tag `OperatingIncomeLoss` at
all — so each metric tries several concepts and the margin series falls through
operating → gross → net, carrying the name of whichever line it used.

The dossier records what it could **not** get and passes that to the writer as
an instruction to state the gap. Analyst price targets are permanently on that
list, being a premium endpoint. A report that says "price target data
unavailable" is worth more than one that leaves the reader assuming the
sentiment read was complete.

Reports run at **7:00am ET every weekday**, before the open, so a pre-earnings
report is useful the day it lands. Both DST cron offsets are registered and the
off-season one is discarded by the same cron-identity check the digest uses.

A run is capped at six reports. Earnings cluster hard — eight holdings reported
within one week of each other in the first live batch — and at roughly five
minutes each a full queue outlives any sensible timeout. The cap takes the
front of the queue, which is earnings ordered soonest-first; the rest stay due
and are picked up the next morning.

Delivery state is committed back on `always()`, including when a run is
cancelled. The first batch was cut off by a timeout after emailing four
reports and skipped its recording step, which would have re-sent all four the
next day.

| Command | Does |
|---|---|
| `--due` | Show what would be generated today, generating nothing |
| `--run` | Generate, email and record everything due |
| `--ticker X --dossier-only` | Print the gathered data, no model, no key needed |
| `--ticker X` | One report on demand |

## Earnings call take-aways

After a holding reports, a poll every three hours looks for what it said and
sends a short set of take-aways — a one-minute read, not a research report.

**Two sources with different jobs.** The reader framed it himself: *"the press
release is the official statement which they read on the earnings call, but
then after the CEO/CFO finishes reading the press release, they do a live Q&A
which then adds important color."*

| Source | Supplies | Availability |
|---|---|---|
| SEC 8-K Item 2.02 release | Every reported figure, and guidance | Always, for a US filer |
| Alpha Vantage transcript | The Q&A, and framing in the prepared remarks | Usually within a day |

So they are not a source and a fallback. The release is the record for numbers —
it is the company's own words, filed within minutes, with the tables, and
auto-generated transcripts mangle numerals. The transcript carries the part
nothing else does. The consequence is that the **comparison against
expectations always works**, transcript or not; only the Q&A section depends on
one, and when there is none the report says so rather than dropping a heading
silently.

**Consensus comes from whichever source carries it.** The calendar entry for
the quarter is preferred — that is the bar standing when the company reported,
which is what the market judged them against — then the surprise feed, then
whatever the pre-earnings report recorded. Where the earlier prediction differs
from the final consensus by enough to print differently, the report says so and
judges the result against the later figure.

Both were being fetched and neither reached the report for a while, so every
take-away said no comparison was possible while the numbers sat unused in
memory. The reader noticed: he could Google the beat and the email could not
state it.

Note that analyst panels differ between providers, so a surprise quoted here
may not match Google to the decimal. The report names its source rather than
implying a single official number. Reported revenue has no free structured
feed, so the consensus is shown and the actual is read from the release.

**The pair is the product.** A pre-earnings report records what it expected —
consensus EPS and revenue, its grade, its sentiment read — as a few structured
fields in `reports_sent.json`. The take-aways read them back and open on the
gap. The report prose is emailed and discarded; archiving it to get this would
have been the expensive way to the same place. Entries written before this
existed are plain date strings, so a missing prior is normal: the report says
so and compares against consensus alone.

**Finding the Q&A.** Transcript segments carry a speaker and a title, so the
boundary is the first analyst to speak — not the last executive, since
management answers throughout the Q&A. A call with no analyst-attributed
segments has no separable Q&A, and the report says that rather than blurring
the two halves.

**Two reports, because the two sources arrive hours apart.** The 8-K release is
on EDGAR minutes after a company reports; the transcript takes hours. So the
first poll after a call always finds the release alone, and the take-aways
written from it go out that day. The period then stays open: if a transcript
appears before the window closes, a second report follows, marked as a
follow-up and weighted towards what the release could not carry — above all the
Q&A. Only a transcript justifies that second email; without one nothing is
sent, so a quarter never produces two versions of the same content.

This is worth stating plainly because the first live week got it wrong. The
send recorded the period as finished regardless of what it had been written
from, so the release-based report closed the door on day one and the four-day
window never opened. Nine take-aways went out over two days and every one of
them was release-only — no Q&A, in a feature whose reason for existing was the
Q&A. The bug was ours, not the data source's: rate limits and quarter-label
mismatch were both suspected first and both ruled out with probes.

**Budgets and windows.** Alpha Vantage's free tier is roughly 25 requests a day
at one per second, and signals a breach with HTTP 200 and an advisory string
rather than a 429 — which reads exactly like a paywall page, and cost one probe
run before this was understood. Only companies actually due are polled, never
the watchlist. A spent budget stops the run asking for transcripts but does not
stop the run: a report the release can carry needs nothing from that source,
and used to be lost along with it. A company is polled for four days after
reporting and then recorded as missed: foreign issuers and small caps often
have no transcript, and without closing the window they would be re-polled
forever.

**Open question: how current the transcript feed is.** Alpha Vantage returns
full transcripts for older quarters — DocuSign's fiscal 2026 Q1 through Q3 all
come back at 6,000–11,000 words, correctly labelled by the company's own fiscal
quarter, the same label Finnhub uses. What is not yet established is how soon
after a call one appears. A day after four companies reported, all four were
still empty, and the probe hit the daily cap before the two intervening
quarters could be checked. Until that is settled, treat "as soon as the
transcript is available" as unproven: the queue now waits correctly, but
whether anything arrives to fill it is a property of the source.

### What was ruled out, so nobody re-tries it

| Source | Result |
|---|---|
| Finnhub `/stock/transcripts` | HTTP 403 — premium |
| Financial Modeling Prep | 401; transcripts have moved to paid plans |
| Motley Fool, Seeking Alpha, Insider Monkey | Not probed. Publicly readable, copyrighted, and their terms forbid automated collection |

## Disclaimer

Everything this repo produces is a synthesis of public information for research
purposes, not financial advice.

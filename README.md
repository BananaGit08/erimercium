# erimercium — stock watchlist agent

A daily email digest over a watchlist of US-listed tickers, plus a chat interface
through Claude Code for managing the list and researching individual names.

No portfolio, no share counts, no cost basis, no P&L. Just a list of tickers and
what is happening to them.

## Status

| Stage | Scope | State |
|---|---|---|
| 1 | Price digest — per-ticker move detection, email at 4:30pm ET | **Done** |
| 2 | Company news + SEC filings (10-Q / 10-K / 8-K), materiality-filtered | Not started |
| 3 | Deep-dive research reports with a letter grade | Not started |
| 4 | Formatting, source links, filter tuning | Not started |

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

Recipient defaults to `christian.na@icloud.com`. Override with a repository
variable named `DIGEST_RECIPIENT`.

### 2. Local runs

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in real values; .env is gitignored
set -a && . ./.env && set +a

python -m watchlist_agent.digest --force --dry-run   # print, do not send
python -m watchlist_agent.digest --force             # actually send
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

Edit it directly, or ask Claude Code (`add NVDA`, `drop ROKU`).

Symbols ending in `-USD` (`BTC-USD`, `ETH-USD`, `XRP-USD`) are treated as
Coinbase product IDs and priced from Coinbase's free public candles API, because
Finnhub's free tier `/quote` endpoint covers equities only. Everything else goes
to Finnhub.

Any ticker that cannot be priced is reported in a **Could not price** section of
the digest rather than failing the run — that is the feedback loop for finding
delisted, renamed, or uncovered symbols.

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
  email_report.py                  text + HTML rendering, Gmail SMTP
  digest.py                        entry point
.github/workflows/daily-digest.yml schedule + manual dispatch
```

## Disclaimer

Everything this repo produces is a synthesis of public information for research
purposes, not financial advice.

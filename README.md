# LLM Paper Trading Bot — Alpaca free tier

Simulated (fake money) **swing / mid-term** trading experiment using an LLM
via OpenRouter and an **Alpaca paper trading account**. Positions are meant
to be carried for days to weeks, not scalped, so the model is fed about a
month of daily closes and the last few days of headlines alongside the live
tape. It also knows what day it is: Alpaca's calendar tells it whether today
is a trading day, a weekend, or a market holiday, and whether holding
overnight means holding across a long weekend. No real money, no
funding required — Alpaca's paper account and IEX market data are both
free. Runs fine on a normal laptop; it's just API calls.

## 1. Install Python dependencies

```
pip install requests
```

(yfinance is no longer needed — market data now comes from Alpaca.)

## 2. Get free Alpaca paper trading keys

1. Sign up at https://alpaca.markets (no deposit, no funding needed)
2. In the dashboard, flip the toggle to **Paper Trading**
3. Under **API Keys**, click *Generate New Key* — copy the Key ID
   (starts with `PK`) and the Secret (shown once)

Your paper account starts with $100k of fake money by default. You can
change the starting balance in the dashboard (Paper Account > Reset) if
you'd rather test with a smaller number.

## 3. Get an OpenRouter API key

1. Sign up at https://openrouter.ai
2. Create a key at https://openrouter.ai/keys
3. Add a small amount of credit (a few dollars is plenty)

## 4. Put your keys in `secret.env`

Create a file called `secret.env` next to `paper_trader.py`:

```
APCA_API_KEY_ID=PK-your-key-id
APCA_API_SECRET_KEY=your-secret
OPENROUTER_API_KEY=sk-or-your-key-here
```

The script loads it automatically on startup. Names must match exactly.
**Never commit this file** — add it to `.gitignore` if you put this repo
anywhere public.

Real environment variables take priority if you'd rather use those:

```
setx APCA_API_KEY_ID "PK-your-key-id"
```
(Windows; close and reopen your terminal after. On Mac/Linux use `export`
in your `~/.bashrc` or `~/.zshrc`.)

## 5. Check it works, offline

```
python paper_trader.py --selftest
```

Runs the order-sizing and parsing logic against fake data. No API calls,
no network, no orders. Should print `selftest OK`.

Then check the screener is reachable — this hits Alpaca but calls no LLM
and places no orders:

```
python paper_trader.py --screen
```

You should get today's candidate pool with prices and % moves. If the
screener endpoints error, tell me what it printed and I'll adjust the
parsing.

## 6. Run it once

```
python paper_trader.py
```

This will:
- Skip immediately if the US market is closed (it asks Alpaca's clock)
- Build a candidate pool from Alpaca's screener — today's most-active
  names and biggest movers, plus anything the account already holds
- Pull snapshots + 5-minute bars for that pool
- **Let the LLM choose** which of them to trade, if any
- Submit those as **paper** market orders to Alpaca (fractional dollar
  amounts for buys, share counts for sells)
- Wait for each order to fill and record the actual execution price
- Compare post-fill account equity against a buy-and-hold baseline
- Log everything to `trade_log.jsonl`

## 7. Run it automatically on GitHub Actions (recommended)

US market hours are 8:30pm–3:00am WIB, so you do not want to babysit this.
Actions runs it for you on US infrastructure — which also sidesteps the
connection problem if Alpaca is blocked on your ISP.

**Make the repo public.** Private repos on the Free plan share a 2,000
minute/month pool *across your whole account*; public repos are unlimited.
At ~36 runs/day this matters. Nothing sensitive is committed — see below.

1. Create the repo and push. **Confirm `secret.env` is not in the push** —
   `.gitignore` covers it, but check `git status` before your first commit.
2. Repo **Settings → Secrets and variables → Actions → New repository
   secret**, add three: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`,
   `OPENROUTER_API_KEY`.
3. Repo **Settings → Pages → Source: Deploy from a branch**, branch `main`,
   folder `/docs`. Your dashboard appears at
   `https://<you>.github.io/<repo>/`.
4. **Actions** tab → `paper-trade` → **Run workflow** to test it immediately
   instead of waiting for the schedule.

The workflow is [.github/workflows/trade.yml](.github/workflows/trade.yml).

**Set it up once and it runs itself.** Nothing needs to be started by hand
on a trading day.

**It fires once per session, not on every cycle.** That is deliberate.
GitHub's scheduler is unreliable at short intervals: a `*/15` cron runs
late and silently drops slots, and asking it for 36 fires a day gives it
36 chances to fail. Instead it fires once, and `paper_trader.py --session`
holds the cadence itself with real sleeps. The cadence becomes exact; only
the start time is at GitHub's mercy.

**The cadence is hourly (`--session 60`).** Re-deciding four times an hour
churns positions the model is supposed to be carrying for days.

**Holidays need no cron rule.** The crons cover Mon-Fri; on Thanksgiving or
Labor Day the script asks Alpaca's clock and calendar, prints why the market
is closed, and exits after two API calls.

This costs about 6 hours of runner time per day. On a **public** repo that
is free and unlimited, which is the whole reason the repo is public. If
you ever make it private, this blows through the 2,000 minute allowance in
under a week.

How it is set up:

- **Two triggers, 13:35 and 14:35 UTC.** Cron cannot follow US daylight
  saving. 13:35 UTC is 09:35 ET in summer, 14:35 UTC is 09:35 ET in
  winter. Whichever lands while the market is open runs the session; the
  other sees a closed market and exits after one API call.
- **`concurrency`** guarantees the two never trade the account at once.
- **State is pushed every cycle**, not at the end. A runner hard-caps a
  job at 6 hours and the script stops itself at 5.75, but if it were ever
  killed mid-session, committing only at the end would lose the whole day.
- **`timeout-minutes: 350`** keeps the job under that ceiling.
- **Final sync runs `if: always()`** so a crash still commits what it had.
- The loop exits as soon as Alpaca reports the session over, so it does
  not idle until the cap.

If a day's trigger is missed entirely, no trading happens that day. Start
one by hand from the Actions tab, or run `python paper_trader.py` locally.

## 8. Monitoring

The dashboard is rebuilt from `trade_log.jsonl` on every run and served
from GitHub Pages — one self-contained HTML file, no JavaScript, no CDN.
It shows equity vs both benchmarks, current holdings, and the LLM's actual
reasoning for each order.

Preview it locally with synthetic data before any real runs exist:

```
python build_dashboard.py --demo
```

Then open `docs/index.html`. Use `python build_dashboard.py` (no flag) to
build from your real log.

Where to look, and for what:

| Where | Good for |
|---|---|
| **Your Pages dashboard** | The morning check. Equity vs SPY/QQQ, and *why* it traded. |
| **Alpaca dashboard** | Ground truth on positions, fills, and the equity curve. |
| **Actions tab** | Did runs actually fire? Red X = crash, and the log shows it. |
| `trade_log.jsonl` | Load into pandas when you want to analyse properly. |

The single number that matters is the bot's equity against QQQ over
**months**. Everything else on that page is texture.

## 9. Run it continuously during market hours (optional)

```
python paper_trader.py --loop 15
```

Polls every 15 minutes (CI uses `--session 60`, which also exits on its own
at the close). US market hours are 9:30am–4:00pm ET, roughly
8:30pm–3:00am WIB (Indonesia time) — late nights if you babysit it, so a
scheduler is usually nicer. Off-hours runs cost one cheap clock call and
exit, so leaving the loop running overnight is harmless.

### Automating on Windows

Use Task Scheduler to run `python paper_trader.py` on a repeating trigger.

### Automating with cron (Linux/Mac)

```
crontab -e
```
```
*/15 * * * * cd /path/to/paper_trader && /usr/bin/python3 paper_trader.py >> cron.log 2>&1
```

## 10. Reviewing results

- **Alpaca dashboard** — positions, order history, and the equity curve
  are all tracked for you there. This is the main place to look now.
- `trade_log.jsonl` — every order, its fill price, its reasoning, and
  periodic equity snapshots (one JSON object per line — easy to load into
  pandas later). The `held` field on each snapshot shows what the LLM had
  accumulated at that moment.
- `baseline_state.json` — the frozen SPY and QQQ buy & hold comparisons

To reset and start over: reset the paper account in the Alpaca dashboard,
then delete `baseline_state.json` and `trade_log.jsonl`.

## What "free tier" actually means

- **Market data is the IEX feed only.** Real-time, but it only sees trades
  that happened on IEX (~2% of US volume), so prices can drift slightly
  from the consolidated tape. The free plan's full-market (SIP) data is
  15 minutes delayed. Fine for this experiment, not institutional-grade.
- **200 API requests/minute.** About 3 per run, plus a few per order
  while it waits for the fill.
- **Fractional/notional orders are regular-hours only**, with a $1
  minimum per order. The script skips anything smaller.
- Paper accounts under $25k equity are still subject to pattern day
  trader rules — if you reset your balance to something small and trade
  actively, Alpaca will start rejecting orders. Default $100k avoids it.

## How the LLM picks stocks

There's no fixed watchlist. Every run:

1. Alpaca's screener returns the day's most-active names (by volume) and
   biggest movers (gainers and losers), `SCREEN_TOP` of each.
2. Anything the account already holds is force-added to the pool — without
   this, a position that falls off the screener could never be sold.
3. The pool is capped at `MAX_CANDIDATES` (20) to keep prompt cost down,
   and names under `MIN_PRICE` ($5) are dropped, since movers lists are
   full of illiquid penny stocks.
4. SPY and QQQ are always priced, as benchmarks. The LLM may trade them.
5. The LLM sees that pool and decides — including deciding to do nothing.

Two consequences worth understanding:

**The pool is momentum-biased.** "Most active" and "biggest mover" are not
neutral samples of the market — they're where today's volatility is. The
LLM never sees a quiet, boring, fairly-valued stock, so it cannot pick
one. That's a real constraint on what this experiment can conclude.

**It still has no memory.** It may buy a name at 10:00 and, at 10:15, see
it in the pool again with no recollection of why it bought. The prompt
tells it to give a decision for everything it holds, so positions do get
revisited — but by a fresh mind each time, not a continuing strategy.

## Benchmarks

The script tracks two, both seeded with your full starting equity on the
first run and never rebalanced:

- **SPY** — the S&P 500, the default "just buy the market" answer
- **QQQ** — the Nasdaq 100, tech-heavy, and the fairer comparison given
  that most-active lists skew hard toward big tech

Beating SPY while losing to QQQ mostly means the LLM picked tech, not that
it picked well. That's exactly why both are tracked.

## Choosing a model

The **script** does the trading — it reads Alpaca, submits orders, clamps
sizes. The model's only job is returning a JSON list of buy/sell/hold
decisions. So there's no such thing as a model that "does the trading
itself"; you're just picking who writes the opinions. What actually
matters, in order:

1. **Returns clean JSON.** The single biggest failure mode. The script
   tolerates fences and chatter, but a model that rambles will still cost
   you runs.
2. **Cheap enough for ~26 calls/day.** Each call is ~1.5k tokens.
3. **Basic numeric reasoning** over six price bars. Almost anything
   current can do this — you do *not* need a frontier model.

Prices below are $/M tokens (input/output), current as of Aug 2026 — check
https://openrouter.ai/models for today's list.

| Model slug | In / Out | ~Cost/month here | Notes |
|---|---|---|---|
| `deepseek/deepseek-v4-flash` | 0.08 / 0.16 | ~$0.12 | **default** — good balance |
| `qwen/qwen3.7-flash` | 0.03 / 0.13 | ~$0.06 | cheapest sane option |
| `openai/gpt-5-mini` | 0.25 / 2.00 | ~$0.75 | noticeably better reasoning |
| `anthropic/claude-haiku-4.5` | 1.00 / 5.00 | ~$1.50 | sharpest of the cheap tier |
| `minimax/minimax-m3:free` | free | $0 | rate-limited, but keeps the whole stack free |

Change `MODEL` at the top of `paper_trader.py`. Since the whole point is
measuring whether the LLM beats buy-and-hold, **only change one thing at a
time** — swapping models mid-experiment resets your evidence.

## Notes on cost

Each run = one LLM API call. Hourly across ~6.5 hours of market time is
~7 calls/day. No output or thinking cap is sent, so a reasoning model will
use its full budget and those calls are not tiny; on the default model
that is still cents per month, and on a free slug it is nothing. Alpaca
itself costs nothing here, news endpoint included.

## Important honesty check

- This is a **learning/research tool**, not a proven money-maker.
- Always compare against the buy-and-hold baseline the script tracks — if
  it can't beat that over months (not days), that's a real signal.
- The script talks **only** to `paper-api.alpaca.markets`, and asserts
  that on startup. Pointing it at the live endpoint would mean editing
  `TRADE_URL` and funding a real account — a separate, deliberate step
  you'd only take after you trust the simulated results.

"""
LLM Paper Trading Bot — Alpaca free tier (Swing / mid-term, US Stocks)
==============================================================
Simulates trading decisions using an LLM (via OpenRouter) against a real
Alpaca PAPER account. Fake money, real order plumbing, real market data.

HOW IT WORKS
------------
1. Checks Alpaca's clock AND calendar, so it knows whether today is a
   trading day, a weekend, or a market holiday, and how long until the
   next session. It skips the run when the market is closed.
2. Builds a candidate pool from Alpaca's screener: today's most-active
   names + biggest movers, plus whatever the account already holds.
3. Pulls snapshots, ~20 daily closes (the swing signal), recent intraday
   bars, and the last few days of headlines for those names (free IEX feed
   plus Alpaca's news API).
4. Sends that data + your paper account state (shares, entry, days held,
   cash) to an LLM on OpenRouter. No output or thinking cap is imposed.
5. THE LLM PICKS the tickers and returns buy / sell / hold per name.
6. Submits the orders to the PAPER endpoint and waits for the fill,
   so the real execution price lands in the log (no real money).
7. Tracks SPY and QQQ buy-and-hold baselines for honest comparison.

The horizon is SWING / MID-TERM: positions are meant to be carried for days
to weeks, across nights and weekends, not scalped intraday.

.github/workflows/trade.yml runs this on GitHub Actions every weekday and
commits the state and dashboard back, so nothing has to be run by hand.

SETUP
-----
1. pip install requests
2. Alpaca: sign up (free), switch to Paper Trading, generate API keys.
     setx APCA_API_KEY_ID     "PK..."      (Windows, then restart terminal)
     setx APCA_API_SECRET_KEY "..."
3. OpenRouter key: setx OPENROUTER_API_KEY "sk-or-..."
4. Edit MODEL / MAX_CANDIDATES below if you want.
5. python paper_trader.py            (one run)
   python paper_trader.py --loop 15  (poll every 15 min)
   python paper_trader.py --screen   (show today's pool, no LLM, no orders)
   python paper_trader.py --selftest (offline sanity check, no API calls)

FREE TIER LIMITS
----------------
- Data feed is IEX only (feed="iex"): real-time but IEX volume only, so
  prices can differ slightly from the consolidated (SIP) tape. SIP on the
  free plan is 15 minutes delayed.
- 200 API requests/minute. ~5 calls per run, plus a few per order
  while waiting for it to fill.
- Fractional/notional orders are regular-hours only, $1 minimum.

THIS ONLY EVER TALKS TO THE PAPER ENDPOINT. NOT FINANCIAL ADVICE.
"""

import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ----------------------------- CONFIG ---------------------------------

# The LLM picks what to trade. It can't scan 10,000 tickers, so each run
# builds a candidate pool from Alpaca's screener (today's most-active names
# and biggest movers) and lets the model choose from that.
SCREEN_TOP = 15            # how many to pull from each screener
MAX_CANDIDATES = 20        # cap on the pool sent to the LLM (token cost)
MIN_PRICE = 5.00           # skip sub-$5 names; screeners surface junk
TARGET_DEPLOYED_PCT = 60   # how much equity the model is told to put to work
MAX_PER_NAME_PCT = 25      # and the most it should put in any single name
BENCHMARKS = ["SPY", "QQQ"]  # buy & hold comparisons, tracked separately

BAR_TIMEFRAME = "5Min"                 # 1Min, 5Min, 15Min, 1Hour...
BARS_SHOWN = 6                         # recent intraday bars sent to the LLM

# This portfolio is SWING / MID-TERM: positions are meant to be carried for
# days to weeks, so the model needs more than today's tape. Daily closes give
# it the multi-week shape, and news moves a multi-day horizon far more than a
# 5-minute bar does.
DAILY_BARS_SHOWN = 20                  # ~1 month of daily closes per name
DAILY_LOOKBACK_DAYS = 45               # calendar days pulled to fill them
HOLD_DAYS_TARGET = "3 to 15 trading days"
NEWS_LOOKBACK_DAYS = 5
NEWS_LIMIT = 40

# OpenRouter model, $/M tokens in/out as of 2026-08. The script does the
# trading; the model only has to return the JSON, so cheap is fine.
#   deepseek/deepseek-v4-flash   0.08 / 0.16   <- default, ~$0.12/month here
#   qwen/qwen3.7-flash           0.03 / 0.13   cheapest sane option
#   openai/gpt-5-mini            0.25 / 2.00   better reasoning, still cheap
#   anthropic/claude-haiku-4.5   1.00 / 5.00   sharpest, ~$1.50/month here
#   minimax/minimax-m3:free      free          rate-limited, keeps this $0
# Live list + prices: https://openrouter.ai/models
MODEL = "deepseek/deepseek-v4-flash"

# Keys come from the environment, or from secret.env next to this script
# (plain KEY=VALUE lines). Real env vars win, so setx still overrides.
_ENV_FILE = Path(__file__).parent / "secret.env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

ALPACA_KEY = os.environ.get("APCA_API_KEY_ID")
ALPACA_SECRET = os.environ.get("APCA_API_SECRET_KEY")
# Written the way Alpaca's docs show it, with the /v2 suffix. _api() strips
# it, because each path below carries its own prefix (/v2 for trading and
# stock bars, /v1beta1 for the screener). Either form works.
TRADE_URL = "https://paper-api.alpaca.markets/v2"   # PAPER ONLY — never live
DATA_URL = "https://data.alpaca.markets"
FEED = "iex"                                     # free tier feed
MIN_NOTIONAL = 1.00                              # Alpaca's minimum $ order
FILL_TIMEOUT = 20                                # secs to wait for a fill

assert "paper-api" in TRADE_URL, "refusing to run against a live endpoint"

STATE_FILE = Path(__file__).parent / "baseline_state.json"
LOG_FILE = Path(__file__).parent / "trade_log.jsonl"

# ----------------------------- ALPACA -----------------------------------

def _headers():
    if not (ALPACA_KEY and ALPACA_SECRET):
        raise RuntimeError(
            "APCA_API_KEY_ID / APCA_API_SECRET_KEY not set. Generate paper "
            "keys at https://app.alpaca.markets (Paper Trading > API Keys)."
        )
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "accept": "application/json",
    }


def _api(method, base, path, **kw):
    # every path below already carries its own /v2 or /v1beta1 prefix, so
    # tolerate a base URL written with one to avoid /v2/v2/... requests
    base = base.rstrip("/").removesuffix("/v2")
    r = requests.request(method, base + path, headers=_headers(), timeout=30, **kw)
    if not r.ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def get_clock():
    return _api("GET", TRADE_URL, "/v2/clock")


def get_account():
    return _api("GET", TRADE_URL, "/v2/account")


def get_positions():
    """ticker -> full position detail. `qty` stays the API's exact string."""
    out = {}
    for p in _api("GET", TRADE_URL, "/v2/positions"):
        out[p["symbol"]] = {
            "qty": p["qty"],
            "qty_f": float(p["qty"]),
            "avg_entry": round(float(p["avg_entry_price"]), 2),
            "current_price": round(float(p.get("current_price") or 0), 2),
            "market_value": round(float(p.get("market_value") or 0), 2),
            "cost_basis": round(float(p.get("cost_basis") or 0), 2),
            "unrealized_pl": round(float(p.get("unrealized_pl") or 0), 2),
            "unrealized_plpc": round(float(p["unrealized_plpc"]) * 100, 2),
        }
    return out


def get_calendar(start, end):
    return _api("GET", TRADE_URL, "/v2/calendar",
                params={"start": str(start), "end": str(end)})


def day_context(clock):
    """What day is it, is the market open, and why not if it is closed.

    Alpaca's calendar already knows every US market holiday and half day, so
    weekend / holiday / after-hours never has to be guessed from a weekday
    number. The model gets this too: holding over a 3-day weekend is a
    different decision from holding over one night.
    """
    now = datetime.fromisoformat(clock["timestamp"])
    today = now.date()
    try:
        cal = get_calendar(today - timedelta(days=5), today + timedelta(days=12))
    except Exception as e:
        print(f"  [warn] calendar failed: {e}")
        cal = []
    sessions = [c["date"] for c in cal]
    is_trading_day = str(today) in sessions
    later = [d for d in sessions if d > str(today)]
    next_day = later[0] if later else None
    gap = (date.fromisoformat(next_day) - today).days if next_day else None

    if clock["is_open"]:
        mins_left = round(
            (datetime.fromisoformat(clock["next_close"]) - now).total_seconds() / 60)
        why = f"open, {mins_left} min to the close"
    else:
        mins_left = None
        if not is_trading_day:
            why = "weekend" if today.weekday() >= 5 else "market holiday"
        else:
            why = "outside regular hours"

    return {
        "date": str(today),
        "weekday": now.strftime("%A"),
        "market_open": clock["is_open"],
        "status": why,
        "minutes_to_close": mins_left,
        "next_open": clock.get("next_open"),
        "next_trading_day": next_day,
        # >1 means the next session is not tomorrow: a weekend or a holiday
        # sits in between, so anything held now is held across it.
        "days_until_next_session": gap,
        "summary": (f"{now:%a %d %b %Y %H:%M} ET - {why}"
                    + (f"; next session {next_day}" if next_day else "")),
    }


def holding_days(symbols):
    """symbol -> days since the buy that opened the position, from our log.

    Alpaca exposes no entry date on a position, so it is read back out of
    trade_log.jsonl. A swing bot needs to know it has been sitting in
    something for two weeks; None just means the log does not go back far
    enough.
    """
    # ponytail: any sell restarts the clock, even a partial trim. Track
    # per-lot entries only if partial exits ever become common.
    opened = {}
    if not LOG_FILE.exists():
        return {}
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "order":
            continue
        sym = (e.get("order") or {}).get("symbol")
        if sym not in symbols:
            continue
        if (e.get("order") or {}).get("side") == "sell":
            opened.pop(sym, None)
        else:
            opened.setdefault(sym, e.get("timestamp"))
    now = datetime.now(timezone.utc)
    out = {}
    for sym, ts in opened.items():
        try:
            out[sym] = round((now - datetime.fromisoformat(ts)).total_seconds() / 86400, 1)
        except (TypeError, ValueError):
            continue
    return out


def submit_order(order):
    return _api("POST", TRADE_URL, "/v2/orders", json=order)


TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def wait_for_fill(order_id, timeout=FILL_TIMEOUT):
    """Orders come back 'accepted'; poll briefly so we can log the real price.

    Returns the last order state seen — still pending if it outlives the
    timeout, which is fine: the next run reads the account fresh anyway.
    """
    deadline = time.monotonic() + timeout
    while True:
        order = _api("GET", TRADE_URL, f"/v2/orders/{order_id}")
        if order["status"] in TERMINAL_STATUSES or time.monotonic() >= deadline:
            return order
        time.sleep(1)

# ----------------------------- DATA -------------------------------------

def _symbols_from(payload):
    """Pull symbols out of any screener response.

    Deliberately shape-agnostic — most-actives and movers use different
    key names ("most_actives" vs "gainers"/"losers"), so just take every
    dict with a symbol out of every list in the response.
    """
    found = []
    for value in payload.values():
        if isinstance(value, list):
            found += [x["symbol"] for x in value
                      if isinstance(x, dict) and x.get("symbol")]
    return found


def fetch_candidates(held):
    """Today's most-active + biggest movers, plus anything already held.

    Held names MUST stay in the pool — otherwise a position that drops off
    the screener can never be sold, and the bot is stuck holding it.
    """
    screened = []
    for path, params in (
        ("/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": SCREEN_TOP}),
        ("/v1beta1/screener/stocks/movers", {"top": SCREEN_TOP}),
    ):
        try:
            screened += _symbols_from(_api("GET", DATA_URL, path, params=params))
        except Exception as e:
            print(f"  [warn] screener {path.rsplit('/', 1)[-1]} failed: {e}")
    # held first so they survive the cap; benchmark last, for pricing only
    pool = list(dict.fromkeys(list(held) + screened))[:MAX_CANDIDATES]
    return list(dict.fromkeys(pool + BENCHMARKS))


def fetch_market_data(tickers, session_date):
    """Snapshots + intraday bars (today) + daily bars (the swing picture)."""
    syms = ",".join(tickers)
    raw = _api("GET", DATA_URL, "/v2/stocks/snapshots",
               params={"symbols": syms, "feed": FEED})
    snaps = raw.get("snapshots", raw)
    bars = _api("GET", DATA_URL, "/v2/stocks/bars",
                params={"symbols": syms, "timeframe": BAR_TIMEFRAME,
                        "start": session_date, "feed": FEED,
                        "limit": 10000}).get("bars") or {}
    # A mid-term call cannot be made off six 5-minute bars, so pull about a
    # month of daily closes as well. Non-fatal: a run on intraday data alone
    # still beats no run.
    day_start = (date.fromisoformat(session_date)
                 - timedelta(days=DAILY_LOOKBACK_DAYS)).isoformat()
    try:
        dailies = _api("GET", DATA_URL, "/v2/stocks/bars",
                       params={"symbols": syms, "timeframe": "1Day",
                               "start": day_start, "feed": FEED,
                               "limit": 10000}).get("bars") or {}
    except Exception as e:
        print(f"  [warn] daily bars failed: {e}")
        dailies = {}

    data = {}
    for t in tickers:
        s = snaps.get(t) or {}
        daily = s.get("dailyBar") or {}
        if not daily:
            print(f"  [warn] no snapshot for {t}")
            continue
        last = ((s.get("latestTrade") or {}).get("p")
                or (s.get("minuteBar") or {}).get("c") or daily.get("c"))
        day_open = daily.get("o")
        prev_close = (s.get("prevDailyBar") or {}).get("c")
        full = [round(b["c"], 2) for b in dailies.get(t, [])]
        closes = full[-DAILY_BARS_SHOWN:]

        def back(n, _c=full, _last=last):
            """% move vs n daily bars ago; None when history is that short.

            Reads the whole pulled series, not the shown slice, so a 20-day
            comparison is not asking a 20-long list for its 20th-from-last.
            """
            return (round((_last - _c[-n]) / _c[-n] * 100, 2)
                    if len(_c) > n else None)

        data[t] = {
            "last_price": round(float(last), 2),
            "day_open": round(float(day_open), 2),
            "prev_close": round(float(prev_close), 2) if prev_close else None,
            "pct_change_today": round((last - day_open) / day_open * 100, 2),
            "pct_change_5d": back(5),
            "pct_change_20d": back(20),
            "high_20d": max(closes) if closes else None,
            "low_20d": min(closes) if closes else None,
            "daily_closes": closes,
            "recent_closes": [round(b["c"], 2) for b in bars.get(t, [])[-BARS_SHOWN:]],
        }
    return data


def fetch_news(tickers, days=NEWS_LOOKBACK_DAYS):
    """Recent headlines for the pool, from Alpaca's news API.

    Same keys, same host, no extra dependency and no extra secret, which is
    why this and not yfinance or a pile of RSS feeds. Non-fatal on failure:
    the run falls back to price data alone.
    """
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        items = _api("GET", DATA_URL, "/v1beta1/news",
                     params={"symbols": ",".join(tickers), "start": start,
                             "limit": NEWS_LIMIT, "sort": "desc",
                             "include_content": "false"}).get("news") or []
    except Exception as e:
        print(f"  [warn] news fetch failed: {e}")
        return []
    pool = set(tickers)
    return [{"when": (n.get("created_at") or "")[:10],
             "symbols": [x for x in (n.get("symbols") or []) if x in pool],
             "headline": (n.get("headline") or "").strip(),
             "summary": (n.get("summary") or "").strip()[:300],
             "source": n.get("source") or ""}
            for n in items if n.get("headline")]

# ----------------------------- LLM ---------------------------------------

def format_news(news):
    lines = []
    for n in news:
        tag = ", ".join(n["symbols"]) or "market"
        head = f"- [{n['when']}] {tag}: {n['headline']}"
        if n["source"]:
            head += f" ({n['source']})"
        if n["summary"]:
            head += f"\n    {n['summary']}"
        lines.append(head)
    return "\n".join(lines) or "(no headlines for these names)"


def build_prompt(market_data, account, positions, day, news, held_days):
    holdings = {
        t: {"shares": round(p["qty_f"], 6),
            "avg_entry": p["avg_entry"],
            "last_price": p["current_price"],
            "market_value": p["market_value"],
            "unrealized_pl": p["unrealized_pl"],
            "unrealized_pct": p["unrealized_plpc"],
            "days_held": held_days.get(t)}
        for t, p in positions.items()
    }
    carry = day.get("days_until_next_session")
    carry_note = ("The next session is TOMORROW."
                  if carry == 1 else
                  f"The next session is {day.get('next_trading_day')}, "
                  f"{carry} days away, so anything held now is held across a "
                  f"weekend or market holiday with no chance to react."
                  if carry else "Next session date unknown.")

    return f"""You are a trading research assistant running a SIMULATED
(Alpaca paper trading, no real money) portfolio experiment. Analyze the
data below and return a decision for each ticker.

TIME HORIZON: this is a SWING / MID-TERM portfolio, not a day-trading one.
You are holding for {HOLD_DAYS_TARGET} - days to weeks, not minutes. Judge
a name on its multi-day trend, its position in the 20-day range, and its
news, not on the last few 5-minute bars. Intraday noise is not a reason to
trade. Positions carry over between runs and across nights and weekends.

TODAY: {day['weekday']} {day['date']}, market is {day['status']}.
{carry_note}

Account equity: ${float(account['equity']):.2f}
Cash on hand:   ${float(account['cash']):.2f}
Current positions (shares, entry, and how long they have been held):
{json.dumps(holdings, indent=2)}

Candidates (today's most-active names and biggest movers, Alpaca IEX feed).
`daily_closes` is the last {DAILY_BARS_SHOWN} daily closes (oldest first) -
that is your swing signal. `recent_closes` is today's {BAR_TIMEFRAME} bars,
context only. {" and ".join(BENCHMARKS)} are included as market references -
you may trade them like anything else:
{json.dumps(market_data, indent=2)}

RECENT NEWS for these names (last {NEWS_LOOKBACK_DAYS} days, newest first).
Weigh this: over a multi-day horizon, a catalyst or a guidance cut matters
more than the chart. Say in your reasoning when a headline drove the call:
{format_news(news)}

YOU choose which of these to trade. This is an ACTIVE experiment: cash
sitting idle earns nothing and generates no data, so bias toward holding
real positions - but "active" means committed, not twitchy.

- Aim to keep roughly {TARGET_DEPLOYED_PCT}% of total equity deployed,
  spread across about 2 to 5 names.
- Open a position when the multi-day trend and the news agree. A modest
  edge is enough; you do not need a textbook setup.
- Once you own something, give the thesis room to work. Do not sell a
  position that is a day or two old just because it moved against you
  slightly. Sell when the thesis breaks, the news turns, or it has run.
- If you are already near the target deployment, rotate: sell the weakest
  thesis and buy a stronger one, rather than adding on top.

For each ticker you want to act on, give: buy, sell, or hold.
- If buy: specify dollar amount to allocate (must not exceed available cash
  across all buys combined; minimum ${MIN_NOTIONAL:.2f} per order).
- If sell: specify number of shares to sell (must not exceed current holding).
- Include every ticker you currently hold, so each position gets a decision.
- Keep any single position under about {MAX_PER_NAME_PCT}% of equity. There
  are no stop losses, so size as if you cannot watch it.
- Always give brief reasoning (1-2 sentences) grounded in the data provided.
- Do not invent information not present in the data above. The candidate
  list changes every run; your positions and their ages above are the only
  memory you have of previous runs.

Respond with ONLY valid JSON, no markdown fences, no commentary outside the
JSON, in this exact structure:
{{
  "decisions": [
    {{"ticker": "AAPL", "action": "buy|sell|hold", "amount_usd": 0, "shares": 0, "reasoning": "..."}}
  ]
}}
"""


def _extract_json(content):
    """Cheap models like to wrap JSON in fences or chatter. Take the object."""
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in model reply: {content[:200]!r}")
    return json.loads(content[start:end + 1])


def call_llm(prompt, effort="high"):
    """One OpenRouter call. No output ceiling is sent, on purpose.

    Omitting max_tokens lets the model use its own full completion budget,
    and reasoning effort "high" lets it think as long as it wants. The one
    exception is the retry below: a model that blew its OWN ceiling mid-JSON
    gets one more pass at lower effort, because a shorter answer beats
    losing the run. That is a fallback, not a cap.
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY environment variable not set. "
            "Get a key at https://openrouter.ai/keys"
        )
    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            # route to whichever host serves this model cheapest
            "provider": {"sort": "price"},
            # models that support thinking will use it; the rest ignore this
            "reasoning": {"effort": effort},
        },
        timeout=600,
    )
    resp.raise_for_status()
    body = resp.json()
    choice = body["choices"][0]
    content = choice["message"].get("content") or ""
    if choice.get("finish_reason") == "length":
        if effort == "high":
            print("  [warn] model hit its own output ceiling; retrying at "
                  "medium reasoning effort")
            return call_llm(prompt, effort="medium")
        raise RuntimeError(
            "model reply was truncated at its own output limit, so the JSON is "
            "incomplete. Lower MAX_CANDIDATES or pick a model with more room.")
    meta = {
        "model": body.get("model") or MODEL,
        # stored in full: the whole point of the log is reading what it thought
        "raw": content,
        "thinking": choice["message"].get("reasoning") or "",
        "usage": body.get("usage") or {},
        "effort": effort,
    }
    return _extract_json(content), meta


# ----------------------------- ORDERS -------------------------------------

def _qty_str(qty):
    return format(qty, ".9f").rstrip("0").rstrip(".")


def plan_orders(decisions, market_data, cash, positions):
    """Turn LLM decisions into Alpaca order payloads, clamped to reality.

    Pure function, no network — covered by --selftest.
    """
    orders, remaining = [], cash
    for d in decisions.get("decisions", []):
        ticker = d.get("ticker")
        action = (d.get("action") or "").lower()
        if ticker not in market_data:
            continue

        if action == "buy":
            amount = math.floor(min(float(d.get("amount_usd") or 0), remaining) * 100) / 100
            if amount < MIN_NOTIONAL:
                continue
            remaining -= amount
            orders.append({"symbol": ticker, "notional": amount, "side": "buy",
                           "type": "market", "time_in_force": "day",
                           "_reasoning": d.get("reasoning", "")})

        elif action == "sell":
            pos = positions.get(ticker)
            requested = float(d.get("shares") or 0)
            if not pos or requested <= 0:
                continue
            qty = min(requested, pos["qty_f"])
            # sell-all reuses the API's own string to dodge float drift
            qty_s = pos["qty"] if qty >= pos["qty_f"] else _qty_str(qty)
            orders.append({"symbol": ticker, "qty": qty_s, "side": "sell",
                           "type": "market", "time_in_force": "day",
                           "_reasoning": d.get("reasoning", "")})
    return orders

# ----------------------------- BASELINE -----------------------------------

def log_event(event: dict):
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


def load_baseline(market_data, equity):
    """Buy & hold the benchmark, seeded from equity on the very first run.

    Since the LLM now picks its own tickers from a rotating pool, the only
    honest comparison is 'what if you'd just bought the index instead'.
    """
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    missing = [b for b in BENCHMARKS if b not in market_data]
    if missing:
        raise RuntimeError(f"no price for benchmark(s) {missing}; cannot seed baseline")
    # each baseline is the full starting equity in that one ETF, so the
    # comparison reads as "what if you had just bought SPY / just bought QQQ"
    state = {
        "starting_equity": equity,
        "shares": {b: equity / market_data[b]["last_price"] for b in BENCHMARKS},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))
    return state


def baseline_values(state, market_data):
    """ticker -> what the starting equity would be worth held in it."""
    return {b: shares * market_data[b]["last_price"]
            for b, shares in state["shares"].items() if b in market_data}

# ----------------------------- MAIN ---------------------------------------

def run_once():
    print(f"\n=== Run at {datetime.now(timezone.utc).isoformat()} ===")
    clock = get_clock()
    day = day_context(clock)
    print(f"  {day['summary']}")
    if not clock["is_open"]:
        print(f"Market closed ({day['status']}). Next open: {clock['next_open']}.")
        return False

    account = get_account()
    positions = get_positions()

    candidates = fetch_candidates(positions.keys())
    market_data = fetch_market_data(candidates, clock["timestamp"][:10])
    if not market_data:
        print("No market data returned. Skipping this cycle.")
        return True
    # drop penny/junk names, but never drop something we hold or the benchmark
    market_data = {t: d for t, d in market_data.items()
                   if d["last_price"] >= MIN_PRICE or t in positions or t in BENCHMARKS}
    print(f"  candidates: {', '.join(t for t in market_data if t not in BENCHMARKS)}")

    baseline = load_baseline(market_data, float(account["equity"]))

    news = fetch_news(list(market_data))
    print(f"  news headlines: {len(news)}")

    try:
        decisions, meta = call_llm(build_prompt(
            market_data, account, positions, day, news, holding_days(positions)))
    except Exception as e:
        print(f"LLM call failed: {e}")
        # logged, not just printed, so the dashboard can show what broke
        log_event({"type": "llm_error", "model": MODEL, "error": str(e)[:1000]})
        return True

    # Log every decision, not just the ones that became orders. Holds are
    # most of what the model does, and its reasoning for passing is the
    # interesting half of the experiment.
    log_event({
        "type": "decisions",
        "model": meta["model"],
        "raw": meta["raw"],
        "thinking": meta["thinking"],
        "usage": meta["usage"],
        "effort": meta["effort"],
        "day": day,
        "news": news,
        "candidates": sorted(t for t in market_data if t not in BENCHMARKS),
        "decisions": [
            {"ticker": d.get("ticker"),
             "action": (d.get("action") or "").lower(),
             "reasoning": (d.get("reasoning") or "").strip()[:400]}
            for d in decisions.get("decisions", []) if d.get("ticker")
        ],
    })

    # cash, not margin buying power — fractional orders are non-marginable
    avail = min(float(account["cash"]),
                float(account.get("non_marginable_buying_power", account["cash"])))
    orders = plan_orders(decisions, market_data, avail, positions)
    if not orders:
        print("  (no actionable decisions this run)")
    for order in orders:
        reasoning = order.pop("_reasoning")
        size = order.get("notional") or order.get("qty")
        try:
            placed = submit_order(order)
            final = wait_for_fill(placed["id"])
            price = final.get("filled_avg_price")
            price = float(price) if price else None
            print(f"  {order['side']:>4} {order['symbol']}: {size} -> "
                  f"{final['status']}" + (f" @ ${price:.2f}" if price else ""))
            log_event({"type": "order", "order": order, "id": placed["id"],
                       "status": final["status"],
                       "filled_qty": final.get("filled_qty"),
                       "filled_avg_price": price, "reasoning": reasoning})
        except Exception as e:
            print(f"  [warn] order failed {order}: {e}")
            log_event({"type": "order_failed", "order": order, "error": str(e)})

    # re-read account AND positions after fills, else the dashboard shows the
    # portfolio as it was before this run traded it
    if orders:
        account, positions = get_account(), get_positions()
    bot_value = float(account["equity"])
    cash = float(account["cash"])
    bases = baseline_values(baseline, market_data)
    print(f"Bot equity:               ${bot_value:.2f}  (cash ${cash:,.2f})")
    for sym, pos in sorted(positions.items()):
        print(f"    {sym:<6} {pos['qty_f']:>10.4f} sh @ ${pos['avg_entry']:>8.2f}"
              f"  now ${pos['market_value']:>10.2f}  ({pos['unrealized_plpc']:+.2f}%)")
    for bench, value in bases.items():
        print(f"  vs {bench} buy&hold:       ${value:>10.2f}  "
              f"({bot_value - value:+.2f})")
    log_event({"type": "snapshot", "bot_value": round(bot_value, 2),
               "cash": round(cash, 2),
               "baselines": {b: round(v, 2) for b, v in bases.items()},
               "positions": {t: {"shares": round(p["qty_f"], 6),
                                 "avg_entry": p["avg_entry"],
                                 "last": p["current_price"],
                                 "value": p["market_value"],
                                 "pl": p["unrealized_pl"],
                                 "pl_pct": p["unrealized_plpc"]}
                             for t, p in positions.items()},
               "held": sorted(positions)})
    return True


def sync_to_git():
    """Commit and push state, if AUTO_COMMIT=1. Used by --session in CI.

    The session job can be killed at the runner's 6-hour ceiling, so state
    is pushed every cycle rather than once at the end.
    """
    if os.environ.get("AUTO_COMMIT") != "1":
        return
    import subprocess
    here = str(Path(__file__).parent)

    def sh(*args):
        return subprocess.run(args, cwd=here, capture_output=True, text=True)

    # A merge or rebase left half-done by an earlier cycle detaches HEAD, and
    # from then on every push fails with "You are not currently on a branch".
    # Clear the wreckage and get back on the branch before touching anything.
    sh("git", "rebase", "--abort")
    sh("git", "merge", "--abort")
    if sh("git", "symbolic-ref", "-q", "HEAD").returncode:
        sh("git", "checkout", "-B", "main")

    sh("python", "build_dashboard.py")
    sh("git", "add", "-A", "baseline_state.json", "trade_log.jsonl", "docs")
    if sh("git", "diff", "--staged", "--quiet").returncode == 0:
        return                                    # nothing changed this cycle
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if sh("git", "commit", "-m", f"run {stamp}").returncode:
        print("  [warn] commit failed; state stays local this cycle")
        return

    # Merge, never rebase: a conflicted rebase detaches HEAD, a conflicted
    # merge does not. `-X ours` settles baseline_state.json and docs/ (both
    # are rebuilt from the log every cycle, so ours is the fresher copy) and
    # .gitattributes unions the log itself, so there is nothing left to stop
    # on. Retry because origin can move between the merge and the push.
    for attempt in range(3):
        pull = sh("git", "pull", "--no-rebase", "--no-edit", "-X", "ours",
                  "origin", "main")
        if pull.returncode:
            sh("git", "merge", "--abort")
            print(f"  [warn] merge failed: {pull.stderr.strip()[:200]}")
            return                          # next cycle retries from scratch
        if sh("git", "push", "origin", "HEAD:main").returncode == 0:
            return
    print("  [warn] push kept losing the race; next cycle will carry the state")


def run_loop(interval_minutes=15, until_close=False, max_hours=5.75):
    """Poll on a fixed interval. Ctrl+C to stop.

    until_close=True is the CI mode: exit as soon as the session ends, and
    exit immediately if the market was already closed on the first check,
    so a backup trigger that fires after hours costs one API call.
    max_hours keeps the job under the runner's 6-hour ceiling.
    """
    print(f"Polling every {interval_minutes} min"
          + (f", until the close (max {max_hours}h)." if until_close else ". Ctrl+C to stop."))
    deadline = time.monotonic() + max_hours * 3600
    seen_open = False
    while time.monotonic() < deadline:
        try:
            is_open = run_once()
            if until_close:
                if is_open:
                    seen_open = True
                elif seen_open:
                    print("Session over. Exiting.")
                    return
                else:
                    print("Market was already closed. Nothing to do.")
                    return
            sync_to_git()
        except Exception as e:
            print(f"Unexpected error: {e}")
        time.sleep(interval_minutes * 60)
    print(f"Hit the {max_hours}h cap. Exiting so the job finishes cleanly.")


def selftest():
    """Offline check of the money path: sizing, clamping, sell-all."""
    md = {"AAPL": {"last_price": 100.0}, "NVDA": {"last_price": 50.0}}
    pos = {"NVDA": {"qty": "2.5", "qty_f": 2.5}}
    orders = plan_orders({"decisions": [
        {"ticker": "AAPL", "action": "buy", "amount_usd": 999},   # over cash
        {"ticker": "NVDA", "action": "sell", "shares": 99},        # over held
        {"ticker": "MSFT", "action": "buy", "amount_usd": 50},     # not in data
        {"ticker": "AAPL", "action": "hold"},
    ]}, md, cash=40.0, positions=pos)
    assert orders[0] == {"symbol": "AAPL", "notional": 40.0, "side": "buy",
                         "type": "market", "time_in_force": "day",
                         "_reasoning": ""}, orders[0]
    assert orders[1]["side"] == "sell" and orders[1]["qty"] == "2.5", orders[1]
    assert len(orders) == 2, orders

    # second buy only gets what's left, sub-$1 remainders are dropped
    o2 = plan_orders({"decisions": [
        {"ticker": "AAPL", "action": "buy", "amount_usd": 39.5},
        {"ticker": "NVDA", "action": "buy", "amount_usd": 10},
    ]}, md, cash=40.0, positions={})
    assert [o["notional"] for o in o2] == [39.5], o2

    # selling something you don't hold is a no-op
    assert plan_orders({"decisions": [{"ticker": "NVDA", "action": "sell", "shares": 1}]},
                       md, 0.0, {}) == []
    assert _qty_str(1.5) == "1.5" and _qty_str(2.0) == "2"

    # model replies are messy: fences, preamble, trailing chatter
    # screener responses differ in shape; we only want the symbols
    assert _symbols_from({"most_actives": [{"symbol": "NVDA", "volume": 1}],
                          "last_updated": "x"}) == ["NVDA"]
    assert _symbols_from({"gainers": [{"symbol": "A"}], "losers": [{"symbol": "B"}],
                          "market_type": "stocks"}) == ["A", "B"]
    assert _symbols_from({"nothing": "here"}) == []

    # news formatting: symbols, source and summary all survive
    line = format_news([{"when": "2026-08-27", "symbols": ["NVDA"],
                         "headline": "Nvidia beats", "summary": "Raised guidance.",
                         "source": "benzinga"}])
    assert "NVDA: Nvidia beats" in line and "Raised guidance." in line, line
    assert format_news([]).startswith("(no headlines")

    # holding age comes from our own log, and a sell restarts the clock
    global LOG_FILE, _api
    real, LOG_FILE = LOG_FILE, Path(__file__).parent / "_tmp_selftest_log.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    LOG_FILE.write_text("".join(json.dumps(e) + "\n" for e in [
        {"type": "order", "timestamp": old, "order": {"symbol": "AAA", "side": "buy"}},
        {"type": "order", "timestamp": old, "order": {"symbol": "BBB", "side": "buy"}},
        {"type": "order", "timestamp": recent, "order": {"symbol": "BBB", "side": "sell"}},
        {"type": "order", "timestamp": recent, "order": {"symbol": "BBB", "side": "buy"}},
    ]))
    ages = holding_days({"AAA", "BBB"})
    LOG_FILE.unlink()
    LOG_FILE = real
    assert round(ages["AAA"]) == 6 and round(ages["BBB"]) == 1, ages

    # the prompt must state the horizon, the day, and the weekend carry
    prompt = build_prompt(
        {"AAPL": {"last_price": 100.0}},
        {"equity": "1000", "cash": "500"},
        {"AAPL": {"qty": "1", "qty_f": 1.0, "avg_entry": 90.0,
                  "current_price": 100.0, "market_value": 100.0,
                  "cost_basis": 90.0, "unrealized_pl": 10.0,
                  "unrealized_plpc": 11.1}},
        {"weekday": "Friday", "date": "2026-08-28",
         "status": "open, 30 min to the close",
         "next_trading_day": "2026-08-31", "days_until_next_session": 3},
        [], {"AAPL": 4.0})
    for must in ("SWING", "Friday 2026-08-28", "3 days away", "days_held",
                 "Cash on hand", "RECENT NEWS", "daily_closes"):
        assert must in prompt, must

    for reply in ('{"decisions": []}',
                  '```json\n{"decisions": []}\n```',
                  'Sure! Here you go:\n{"decisions": []}\nHope that helps.'):
        assert _extract_json(reply) == {"decisions": []}, reply
    # Alpaca response shapes, parsed against a stub. The real calls cannot be
    # exercised from a machine that cannot reach Alpaca, and these three are
    # the parsing that a silent API change would break first.
    def fake_api(method, base, path, **kw):
        if path == "/v2/calendar":
            return [{"date": "2026-08-27"}, {"date": "2026-08-28"},
                    {"date": "2026-08-31"}]
        if path == "/v2/stocks/snapshots":
            return {"snapshots": {"AAA": {"dailyBar": {"o": 100.0, "c": 104.0},
                                          "latestTrade": {"p": 105.0},
                                          "prevDailyBar": {"c": 99.0}}}}
        if path == "/v2/stocks/bars":
            if kw["params"]["timeframe"] == "1Day":
                return {"bars": {"AAA": [{"c": 90.0 + i} for i in range(25)]}}
            return {"bars": {"AAA": [{"c": 104.0}, {"c": 105.0}]}}
        if path == "/v1beta1/news":
            return {"news": [{"headline": "AAA wins a contract",
                              "summary": "Big one.", "source": "benzinga",
                              "symbols": ["AAA", "ZZZ"],
                              "created_at": "2026-08-27T12:00:00Z"}]}
        raise AssertionError(path)

    live_api, _api = _api, fake_api
    try:
        friday = day_context({"timestamp": "2026-08-28T15:30:00-04:00",
                              "is_open": True,
                              "next_close": "2026-08-28T16:00:00-04:00",
                              "next_open": "2026-08-31T09:30:00-04:00"})
        saturday = day_context({"timestamp": "2026-08-29T10:00:00-04:00",
                                "is_open": False, "next_open": "x"})
        holiday = day_context({"timestamp": "2026-09-07T10:00:00-04:00",
                               "is_open": False, "next_open": "x"})
        md = fetch_market_data(["AAA"], "2026-08-28")
        news = fetch_news(["AAA"])
    finally:
        _api = live_api

    assert friday["weekday"] == "Friday" and friday["minutes_to_close"] == 30, friday
    assert friday["next_trading_day"] == "2026-08-31", friday
    assert friday["days_until_next_session"] == 3, friday   # long weekend
    assert saturday["status"] == "weekend", saturday
    assert holiday["status"] == "market holiday", holiday   # a Monday, no session

    bar = md["AAA"]
    assert bar["daily_closes"][-1] == 114.0, bar
    assert len(bar["daily_closes"]) == DAILY_BARS_SHOWN, bar
    assert (bar["high_20d"], bar["low_20d"]) == (114.0, 95.0), bar
    assert bar["pct_change_5d"] == -4.55 and bar["pct_change_20d"] == 10.53, bar
    assert bar["recent_closes"] == [104.0, 105.0], bar
    # news is filtered down to the pool we asked about
    assert news[0]["symbols"] == ["AAA"] and news[0]["when"] == "2026-08-27", news

    print("selftest OK")


def show_screen():
    """Print the candidate pool and exit. No LLM call, no orders."""
    held = get_positions()
    candidates = fetch_candidates(held.keys())
    print(f"pool ({len(candidates)}): {', '.join(candidates)}")
    data = fetch_market_data(candidates, get_clock()["timestamp"][:10])
    for t, d in sorted(data.items(), key=lambda kv: -abs(kv[1]["pct_change_today"])):
        flags = " [held]" if t in held else (" [benchmark]" if t in BENCHMARKS else "")
        skip = "" if d["last_price"] >= MIN_PRICE or flags else "  <- below MIN_PRICE"
        print(f"  {t:<6} ${d['last_price']:>9.2f}  {d['pct_change_today']:>+7.2f}%{flags}{skip}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--selftest":
        selftest()
    elif arg == "--screen":
        show_screen()
    elif arg == "--loop":
        run_loop(int(sys.argv[2]) if len(sys.argv) > 2 else 15)
    elif arg == "--session":
        # one CI job covers the whole trading session
        run_loop(int(sys.argv[2]) if len(sys.argv) > 2 else 15, until_close=True)
    else:
        run_once()

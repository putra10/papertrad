"""
LLM Paper Trading Bot — Alpaca free tier (Intraday, US Stocks)
==============================================================
Simulates trading decisions using an LLM (via OpenRouter) against a real
Alpaca PAPER account. Fake money, real order plumbing, real market data.

HOW IT WORKS
------------
1. Checks Alpaca's clock — skips the run if the market is closed.
2. Builds a candidate pool from Alpaca's screener: today's most-active
   names + biggest movers, plus whatever the account already holds.
3. Pulls snapshots + recent intraday bars for those (free IEX feed).
4. Sends that data + your paper account state to an LLM on OpenRouter.
5. THE LLM PICKS the tickers and returns buy / sell / hold per name.
6. Submits the orders to the PAPER endpoint and waits for the fill,
   so the real execution price lands in the log (no real money).
7. Tracks SPY and QQQ buy-and-hold baselines for honest comparison.

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
from datetime import datetime, timezone
from pathlib import Path

import requests

# ----------------------------- CONFIG ---------------------------------

# The LLM picks what to trade. It can't scan 10,000 tickers, so each run
# builds a candidate pool from Alpaca's screener (today's most-active names
# and biggest movers) and lets the model choose from that.
SCREEN_TOP = 15            # how many to pull from each screener
MAX_CANDIDATES = 20        # cap on the pool sent to the LLM (token cost)
MIN_PRICE = 5.00           # skip sub-$5 names; screeners surface junk
BENCHMARKS = ["SPY", "QQQ"]  # buy & hold comparisons, tracked separately

BAR_TIMEFRAME = "5Min"                 # 1Min, 5Min, 15Min, 1Hour...
BARS_SHOWN = 6                         # recent bars sent to the LLM

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
    """ticker -> {qty (exact string from API), qty_f, avg_entry, unrealized_plpc}"""
    out = {}
    for p in _api("GET", TRADE_URL, "/v2/positions"):
        out[p["symbol"]] = {
            "qty": p["qty"],
            "qty_f": float(p["qty"]),
            "avg_entry": round(float(p["avg_entry_price"]), 2),
            "unrealized_plpc": round(float(p["unrealized_plpc"]) * 100, 2),
        }
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
    """Snapshots (price / day open) + recent bars (shape), free IEX feed."""
    syms = ",".join(tickers)
    raw = _api("GET", DATA_URL, "/v2/stocks/snapshots",
               params={"symbols": syms, "feed": FEED})
    snaps = raw.get("snapshots", raw)
    bars = _api("GET", DATA_URL, "/v2/stocks/bars",
                params={"symbols": syms, "timeframe": BAR_TIMEFRAME,
                        "start": session_date, "feed": FEED,
                        "limit": 10000}).get("bars") or {}

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
        data[t] = {
            "last_price": round(float(last), 2),
            "day_open": round(float(day_open), 2),
            "prev_close": round(float(prev_close), 2) if prev_close else None,
            "pct_change_today": round((last - day_open) / day_open * 100, 2),
            "recent_closes": [round(b["c"], 2) for b in bars.get(t, [])[-BARS_SHOWN:]],
        }
    return data

# ----------------------------- LLM ---------------------------------------

def build_prompt(market_data, account, positions):
    return f"""You are a trading research assistant running a SIMULATED
(Alpaca paper trading, no real money) portfolio experiment. Analyze the
data below and return a decision for each ticker.

Account equity: ${float(account['equity']):.2f}
Available cash: ${float(account['cash']):.2f}
Current positions: {json.dumps(positions, indent=2)}

Candidates (today's most-active names and biggest movers, Alpaca IEX feed,
{BAR_TIMEFRAME} bars). {" and ".join(BENCHMARKS)} are included as market
references — you may trade them like anything else:
{json.dumps(market_data, indent=2)}

YOU choose which of these to trade. You are not required to trade any of
them — returning all holds is a valid and often correct answer.

For each ticker you want to act on, give: buy, sell, or hold.
- If buy: specify dollar amount to allocate (must not exceed available cash
  across all buys combined; minimum ${MIN_NOTIONAL:.2f} per order).
- If sell: specify number of shares to sell (must not exceed current holding).
- Include every ticker you currently hold, so each position gets a decision.
- Concentrating everything in one name is allowed but risky; you carry these
  positions into future runs and there are no stop losses.
- Always give brief reasoning (1-2 sentences) grounded in the data provided.
- Do not invent information not present in the data above. The candidate
  list changes every run; you have no memory of previous runs.

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


def call_llm(prompt):
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
        },
        timeout=60,
    )
    resp.raise_for_status()
    return _extract_json(resp.json()["choices"][0]["message"]["content"])

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
    if not clock["is_open"]:
        print(f"Market closed. Next open: {clock['next_open']}. Skipping.")
        return

    account = get_account()
    positions = get_positions()

    candidates = fetch_candidates(positions.keys())
    market_data = fetch_market_data(candidates, clock["timestamp"][:10])
    if not market_data:
        print("No market data returned. Skipping.")
        return
    # drop penny/junk names, but never drop something we hold or the benchmark
    market_data = {t: d for t, d in market_data.items()
                   if d["last_price"] >= MIN_PRICE or t in positions or t in BENCHMARKS}
    print(f"  candidates: {', '.join(t for t in market_data if t not in BENCHMARKS)}")

    baseline = load_baseline(market_data, float(account["equity"]))

    try:
        decisions = call_llm(build_prompt(market_data, account, positions))
    except Exception as e:
        print(f"LLM call failed: {e}")
        return

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

    # re-read the account after fills, else the comparison lags a run behind
    bot_value = float((get_account() if orders else account)["equity"])
    bases = baseline_values(baseline, market_data)
    print(f"Bot equity:               ${bot_value:.2f}")
    for bench, value in bases.items():
        print(f"  vs {bench} buy&hold:       ${value:>10.2f}  "
              f"({bot_value - value:+.2f})")
    log_event({"type": "snapshot", "bot_value": round(bot_value, 2),
               "baselines": {b: round(v, 2) for b, v in bases.items()},
               "held": sorted(positions)})


def run_loop(interval_minutes=15):
    """Poll continuously. Ctrl+C to stop. Closed-market runs no-op cheaply."""
    print(f"Starting loop, polling every {interval_minutes} minutes. Ctrl+C to stop.")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"Unexpected error: {e}")
        time.sleep(interval_minutes * 60)


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

    for reply in ('{"decisions": []}',
                  '```json\n{"decisions": []}\n```',
                  'Sure! Here you go:\n{"decisions": []}\nHope that helps.'):
        assert _extract_json(reply) == {"decisions": []}, reply
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
    else:
        run_once()

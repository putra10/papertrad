"""
Mirror the bot's book into a Dashboard Porto account, so the same positions
show up there next to the human-managed portfolios.

The dashboard keeps one account per 7-character token. This writes the bot's
book into ONE reserved token (BOT-001) on every run: open the dashboard, type
that token, and you are looking at the paper trader.

Run:  python sync_dashboard.py
      python sync_dashboard.py --check   (self-check on the mapping, no I/O)

Where it writes, in order:
  1. Supabase, when SUPABASE_URL + SUPABASE_KEY are set. This is what the
     deployed dashboard reads, so it is the one that matters in CI.
  2. A local accounts.json, when DASHBOARD_ACCOUNTS points at one (or the
     sibling checkout exists). Only useful when running the dashboard locally.
Neither configured is not an error: the bot's own run must not fail over this.
"""

import json
import os
import sys
from pathlib import Path

from build_dashboard import LOG, read_events

TOKEN = "BOT-001"
# The book is a USD paper account at Alpaca, so state it in USD. The dashboard
# converts to whatever base currency the account asks for anyway.
BASE_CURRENCY = "USD"
CASH_ACCOUNT = "Alpaca paper cash"
DUST_SHARES = 1e-6              # same threshold paper_trader.py uses

# Where a local dashboard checkout keeps its account file when Supabase is off.
DEFAULT_LOCAL = (Path(__file__).resolve().parent.parent
                 / "Dashboard Porto" / "stock-analyser" / "data" / "accounts.json")


def latest_snapshot(events):
    snaps = [e for e in events if e.get("type") == "snapshot"]
    return snaps[-1] if snaps else None


def to_portfolio(snap):
    """Map one bot snapshot onto the dashboard's portfolio schema."""
    stocks = []
    for ticker, p in sorted((snap.get("positions") or {}).items()):
        shares = float(p.get("shares") or 0)
        if shares < DUST_SHARES:       # exited names linger as Alpaca residue
            continue
        stocks.append({"ticker": ticker, "shares": shares,
                       "avg_price": float(p.get("avg_entry") or 0)})
    return {
        "base_currency": BASE_CURRENCY,
        "stocks": stocks,
        "crypto": [],
        "gold": [],
        # The dashboard has no "cash" field: cash is a bank account, and that
        # is what keeps the account's total equal to the bot's equity.
        "banks": [{"name": CASH_ACCOUNT,
                   "balance": float(snap.get("cash") or 0),
                   "currency": "USD"}],
    }


def push_supabase(url, key, portfolio, watchlist):
    import requests
    r = requests.post(
        f"{url.rstrip('/')}/rest/v1/accounts",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 # upsert: the row is rewritten every run, never duplicated
                 "Prefer": "resolution=merge-duplicates,return=minimal"},
        json={"token": TOKEN, "portfolio": portfolio, "watchlist": watchlist},
        timeout=30)
    r.raise_for_status()


def push_local(path, portfolio, watchlist):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[TOKEN] = {"portfolio": portfolio, "watchlist": watchlist}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main():
    snap = latest_snapshot(read_events(LOG))
    if not snap:
        print("sync: no snapshot in the log yet, nothing to mirror")
        return
    portfolio = to_portfolio(snap)
    watchlist = [s["ticker"] for s in portfolio["stocks"]]
    held = ", ".join(watchlist) or "cash only"

    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if url and key:
        push_supabase(url, key, portfolio, watchlist)
        print(f"sync: pushed {TOKEN} to Supabase ({held})")
        return

    local = Path(os.getenv("DASHBOARD_ACCOUNTS") or DEFAULT_LOCAL)
    if local.parent.exists():
        push_local(local, portfolio, watchlist)
        print(f"sync: wrote {TOKEN} to {local} ({held})")
        return

    print("sync: no SUPABASE_URL/SUPABASE_KEY and no local dashboard, skipped")


def check():
    snap = {"cash": 1000.5, "positions": {
        "NVDA": {"shares": 10.0, "avg_entry": 220.25},
        "SPY": {"shares": 0.0, "avg_entry": 761.33},        # exited
        "DUST": {"shares": 1e-9, "avg_entry": 14.72},       # Alpaca residue
    }}
    p = to_portfolio(snap)
    assert [s["ticker"] for s in p["stocks"]] == ["NVDA"], p["stocks"]
    assert p["stocks"][0] == {"ticker": "NVDA", "shares": 10.0, "avg_price": 220.25}
    assert p["banks"][0]["balance"] == 1000.5
    assert p["crypto"] == [] and p["gold"] == []
    # every key the dashboard's DEFAULT_PORTFOLIO expects
    assert set(p) == {"base_currency", "stocks", "crypto", "gold", "banks"}
    # a snapshot with no positions at all still yields a valid account
    assert to_portfolio({"cash": 0})["stocks"] == []
    print("sync_dashboard: checks pass")


if __name__ == "__main__":
    check() if "--check" in sys.argv else main()

"""
Build docs/index.html from trade_log.jsonl — a single self-contained page.

Run:  python build_dashboard.py
      python build_dashboard.py --demo   (synthetic data, to preview the
                                          layout before real runs exist;
                                          also doubles as the self-check)

No dependencies, no CDN, no web fonts — GitHub Pages serves the file as-is.
A small inline script adds the theme toggle and chart zoom and filter; the
page is complete and readable without it.
"""

import json
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
LOG = HERE / "trade_log.jsonl"
OUT = HERE / "docs" / "index.html"
WIB = timezone(timedelta(hours=7))       # author is in Indonesia; show both

# The account this bot's book is mirrored into. Read from the same plain
# KEY=VALUE file paper_trader.py uses, or from the environment (the CI secret),
# so the token lives in exactly one place; sync_dashboard.py imports it from
# here. It is printed on the page so the author cannot lose it: the account is
# a mirror, rewritten every cycle, of what that page already shows.
_ENV_FILE = HERE / "secret.env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "").strip().upper()

# ----------------------------- DATA ---------------------------------------

def read_events(path):
    events = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue        # a half-written line from a killed run; skip it
    # A union merge of two sessions' appends interleaves the lines, so file
    # order is not time order. Everything downstream reads the tail as "the
    # latest", so sort. Timestamps are ISO-8601 UTC, hence sorting as text.
    events.sort(key=lambda e: e.get("timestamp") or "")
    return events


def build_series(snapshots):
    """{'bot': [(t, v)...], 'SPY': [...], 'QQQ': [...]} in log order."""
    series = {"bot": []}
    for s in snapshots:
        t = s.get("timestamp")
        if s.get("bot_value") is None:
            continue
        series["bot"].append((t, float(s["bot_value"])))
        for name, value in (s.get("baselines") or {}).items():
            if value is not None:
                series.setdefault(name, []).append((t, float(value)))
    return {k: v for k, v in series.items() if v}


def market_now():
    """Ask Alpaca what the market is doing right now, or None if it cannot.

    The page is static, so on its own it can only report the timestamp of
    the last cycle -- and a stamp that has not moved since Friday looks
    exactly like a bot that died. Reading the market at build time is what
    separates "closed, nothing to do" from "broken".

    All of it is optional. No Alpaca keys in the environment (a local
    --demo preview, or a CI step that was not handed them) or any failure
    reaching Alpaca returns None, and the header renders as it always did.
    A dashboard build must never fail because a quote server is down.
    """
    if not (os.environ.get("APCA_API_KEY_ID")
            and os.environ.get("APCA_API_SECRET_KEY")):
        return None
    try:
        # Imported here rather than at the top: this module is otherwise
        # pure stdlib and runs offline, and paper_trader needs requests.
        # It is also the one place that already knows how to tell a
        # holiday from a weekend, off Alpaca's calendar.
        import paper_trader
        return paper_trader.day_context(paper_trader.get_clock())
    except Exception as e:
        print(f"  [warn] market status unavailable: {e}")
        return None

# ----------------------------- RENDER -------------------------------------

def fmt_money(v):
    return f"${v:,.2f}"


def fmt_delta(v):
    return f"{'+' if v >= 0 else '-'}{abs(v):,.2f}"


def when(iso):
    """UTC ISO string -> 'DD Mon HH:MM WIB' plus the UTC time."""
    if not iso:
        return "n/a"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return f"{dt.astimezone(WIB):%d %b %H:%M} WIB"


def when_full(iso):
    """UTC ISO string -> '27 Aug 2026, 02:05 WIB'."""
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return f"{dt.astimezone(WIB):%d %b %Y, %H:%M} WIB"


def when_day(iso):
    """ISO string with any offset -> 'Tue 08 Sep 20:30 WIB'.

    The weekday earns its space here: the only place this is used is a
    "next open" that can be tomorrow, after a weekend, or after a holiday,
    and a bare date does not say which.
    """
    if not iso:
        return "n/a"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return f"{dt.astimezone(WIB):%a %d %b %H:%M} WIB"


def clean(text):
    """No em or en dashes on the page, including model-written reasoning."""
    return (text or "").replace("—", "-").replace("–", "-")


def market_line(market):
    """A day_context dict -> the market clause of the header, or "".

    Why "next open" is an absolute time and not "closed now": nothing
    rebuilds this page over a weekend, so a Saturday reader is looking at
    Friday evening's build. "Closed" would be stale by then; "next open
    Mon 08 Sep 20:30 WIB" is still true.
    """
    if not market:
        return ""
    status = market.get("status") or ""
    if market.get("market_open"):
        # day_context words this one itself: "open, 84 min to the close".
        return f' &middot; <span class="mkt on">{clean("market " + status)}</span>'
    reason = {"weekend": " for the weekend",
              "market holiday": " for a holiday"}.get(status, "")
    text = "market closed" + reason
    if market.get("next_open"):
        text += f", next open {when_day(market['next_open'])}"
    return f' &middot; <span class="mkt off">{clean(text)}</span>'


SERIES_LABEL = {"bot": "Bot", "SPY": "SPY", "QQQ": "QQQ"}


def esc(t):
    return (clean(str(t if t is not None else "")).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def pct_series(series):
    """Every series as % return from its own first point."""
    out = {}
    for name, pts in series.items():
        base = pts[0][1] or 1
        out[name] = [(t, (v / base - 1) * 100) for t, v in pts]
    return out


def max_drawdown(pts):
    peak, dd = None, 0.0
    for _, v in pts:
        peak = v if peak is None else max(peak, v)
        dd = min(dd, v / peak - 1)
    return dd * 100


def session_closes(series):
    """{name: [(us_date, value)]}, the last reading of each US session.

    Grouped on the UTC date: a US session sits inside one UTC day, while in
    WIB it straddles midnight.
    """
    out = {}
    for name, pts in series.items():
        days = {}
        for t, v in pts:
            days[(t or "")[:10]] = v
        out[name] = sorted(days.items())
    return out


def nice_step(span, most=6):
    for step in (0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50):
        if span / step <= most:
            return step
    return 100


def chart(series, height=300):
    """% return since start, one line per series, labelled at its end.

    No JavaScript and no fixed pixel layout: the lines sit in an SVG that
    stretches both ways (non-scaling strokes keep them crisp), and every
    label is HTML placed by percentage, so it lands on the same spot at any
    width or height the CSS gives the chart.
    """
    pcts = pct_series(series)
    values = [v for pts in pcts.values() for _, v in pts]
    if len(series.get("bot") or []) < 2:
        return ('<p class="empty">Not enough data for a chart yet. It needs '
                'at least two runs.</p>')
    lo, hi = min(values + [0.0]), max(values + [0.0])
    span = (hi - lo) or 1
    lo, hi = lo - span * 0.06, hi + span * 0.06
    step = nice_step(hi - lo)

    def top(v):                        # % from the top of the plot
        return (hi - v) / (hi - lo) * 100

    lines, grid, ends = [], [], []
    tick = math.ceil(lo / step) * step
    while tick <= hi:
        grid.append(
            f'<line x1="0" x2="1000" y1="{top(tick) * height / 100:.1f}" '
            f'y2="{top(tick) * height / 100:.1f}" class="{"zero" if abs(tick) < 1e-9 else "grid"}"/>')
        lines.append(f'<span class="ytick" style="top:{top(tick):.2f}%">'
                     f'{tick:+g}%</span>' if tick else
                     f'<span class="ytick zero" style="top:{top(tick):.2f}%">0%</span>')
        tick += step
    paths = []
    order = ["SPY", "QQQ", "bot"]                  # bot drawn last, on top
    for name in sorted(pcts, key=lambda n: order.index(n) if n in order else 0):
        pts = pcts[name]
        n = len(pts)
        d = " ".join(f"{1000 * i / max(n - 1, 1):.1f},{top(v) * height / 100:.1f}"
                     for i, (_, v) in enumerate(pts))
        paths.append(f'<polyline class="ln ln-{name.lower()}" points="{d}"/>')
        ends.append([top(pts[-1][1]), name, pts[-1][1]])
    # keep end labels from sitting on each other
    ends.sort()
    for i in range(1, len(ends)):
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 9)
    labels = "".join(
        f'<span class="end end-{name.lower()}" style="top:{y:.2f}%">'
        f'<b>{SERIES_LABEL.get(name, name)}</b> {v:+.2f}%</span>'
        for y, name, v in ends)
    stamps = series["bot"]
    mid = stamps[len(stamps) // 2][0]
    return (
        f'<div class="chart" data-chart>'
        f'<div class="plot"><svg viewBox="0 0 1000 {height}" preserveAspectRatio="none" '
        f'role="img" aria-label="Return since start: bot against SPY and QQQ">'
        f'{"".join(grid)}{"".join(paths)}</svg>{"".join(lines)}</div>'
        f'<div class="rail">{labels}</div>'
        f'<div class="xaxis"><span>{when_date(stamps[0][0])}</span>'
        f'<span>{when_date(mid)}</span><span>{when_date(stamps[-1][0])}</span></div>'
        f'</div>')


def when_date(iso):
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).strftime("%d %b")
    except ValueError:
        return ""


def day_strip(closes):
    """One bar per session: the bot's day minus QQQ's day, in points."""
    bot, ref = closes.get("bot") or [], dict(closes.get("QQQ") or [])
    rows = []
    for (d0, b0), (d1, b1) in zip(bot, bot[1:]):
        if d0 in ref and d1 in ref and ref[d0]:
            rows.append((d1, (b1 / b0 - (ref[d1] / ref[d0])) * 100))
    rows = rows[-60:]
    if not rows:
        return '<p class="empty">Fills in after the second trading day.</p>'
    big = max(abs(v) for _, v in rows) or 1
    bars = "".join(
        f'<span class="bar {"win" if v > 0 else "loss"}" '
        f'title="{datetime.fromisoformat(d).strftime("%a %d %b")}: '
        f'{"beat" if v > 0 else "trailed"} QQQ by {abs(v):.2f} pts">'
        f'<i style="height:{max(abs(v) / big * 100, 3):.1f}%"></i></span>'
        for d, v in rows)
    wins = sum(v > 0 for _, v in rows)
    return (f'<div class="strip">{bars}</div>'
            f'<p class="note">Each bar is one US trading session. Up and green: '
            f'the bot beat QQQ that day. Down and red: it trailed. '
            f'<b>{wins} of {len(rows)}</b> sessions won.</p>')


def opened_at(orders, symbols):
    """symbol -> the buy that opened the current position (same rule as the
    trader: any sell restarts the clock)."""
    opened = {}
    for o in orders:
        spec = o.get("order") or {}
        sym = spec.get("symbol")
        if sym not in symbols:
            continue
        if spec.get("side") == "sell":
            opened.pop(sym, None)
        else:
            opened.setdefault(sym, o)
    return opened


def age(iso, now):
    try:
        hours = (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 3600
    except (AttributeError, ValueError):
        return ""
    return f"{hours:.0f}h" if hours < 48 else f"{hours / 24:.0f}d"


def verdict(pcts):
    """The page's answer, in one sentence, from the latest returns."""
    if "bot" not in pcts:
        return ('<p class="verdict">Waiting for the first run.</p>', "")
    bot = pcts["bot"][-1][1]
    others = [(n, pcts[n][-1][1]) for n in ("QQQ", "SPY") if n in pcts]
    ahead = [n for n, v in others if bot > v]
    if others and len(ahead) == len(others):
        head = "Beating the market"
    elif ahead:
        head = f"Ahead of {ahead[0]}, behind {[n for n, _ in others if n not in ahead][0]}"
    elif others:
        head = "Behind the market"
    else:
        head = "Running"
    gaps = " ".join(
        f'<span class="gap {"up" if bot - v > 0 else "down"}">'
        f'{"+" if bot - v >= 0 else "-"}{abs(bot - v):.2f} pts <small>vs {n}</small></span>'
        for n, v in others)
    tone = "up" if others and len(ahead) == len(others) else "down" if not ahead else ""
    return (f'<h1 class="verdict {tone}">{head}.</h1>', gaps)


def is_held(p):
    """A real holding, not the residue a full sell leaves behind.

    Alpaca can leave a few ten-thousandths of a share after selling
    everything: above the trader's 1e-6 dust line, but worth a cent. Older
    snapshots carry no value, so they are judged on shares alone.
    """
    return (float(p.get("shares") or 0) >= 1e-6
            and float(p.get("value", 1) or 0) >= 1)


HOLD_BUCKETS = ((1, "Under 1 hour"), (24, "1 hour to 1 day"),
                (72, "1 to 3 days"), (float("inf"), "3 days or more"))


def ticker_book(events):
    """Replay the logged fills: P&L and holding time per ticker.

    Returns (rows, trips, unlogged). Snapshots are the truth about what is
    held: when one no longer shows a name the replay still carries, that
    exit never reached the log (fills lost to cancelled runs and merge
    conflicts). The leftover is dropped rather than guessed at, and whatever
    it cost shows up in `unlogged`, the part of the equity change that no
    logged trade explains.
    """
    pos, book, trips = {}, {}, []
    latest = None
    for e in events:                                     # already time-sorted
        kind = e.get("type")
        if kind == "snapshot" and "positions" in e:
            latest = e
            held = {s for s, p in e["positions"].items()
                    if is_held(p)}
            for sym in [s for s in pos if s not in held]:
                # unlogged exit: the hold ended within one cycle of this
                # snapshot, so its length is known; its P&L only in part
                p = pos.pop(sym)
                b = book[sym]
                hours = (datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
                         - p["since"]).total_seconds() / 3600
                b["holds"].append(hours)
                b["wins"] += p["pnl"] > 0
                trips.append((hours, p["pnl"]))
            continue
        if kind != "order" or not e.get("filled_avg_price"):
            continue
        spec = e.get("order") or {}
        sym, side = spec.get("symbol"), spec.get("side")
        try:
            qty, px = float(e.get("filled_qty") or 0), float(e["filled_avg_price"])
            ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        b = book.setdefault(sym, {"pnl": 0.0, "holds": [], "wins": 0, "open": None})
        p = pos.get(sym)
        if side == "buy":
            if not p:
                p = pos[sym] = {"sh": 0.0, "cost": 0.0, "since": ts, "pnl": 0.0}
            p["sh"] += qty
            p["cost"] += qty * px
        elif p and p["sh"] > 0:
            qty = min(qty, p["sh"])
            avg = p["cost"] / p["sh"]
            p["pnl"] += (px - avg) * qty
            b["pnl"] += (px - avg) * qty
            p["sh"] -= qty
            p["cost"] -= avg * qty
            if p["sh"] < 1e-4:                          # round trip closed
                hours = (ts - p["since"]).total_seconds() / 3600
                b["holds"].append(hours)
                b["wins"] += p["pnl"] > 0
                trips.append((hours, p["pnl"]))
                del pos[sym]
    if latest:
        now = datetime.fromisoformat(latest["timestamp"].replace("Z", "+00:00"))
        for sym, lp in latest["positions"].items():
            if not is_held(lp):
                continue
            b = book.setdefault(sym, {"pnl": 0.0, "holds": [], "wins": 0, "open": None})
            b["pnl"] += float(lp.get("pl") or 0)
            since = (pos.get(sym) or {}).get("since")
            b["open"] = (now - since).total_seconds() / 3600 if since else 0.0
    rows = sorted(({"sym": s, **b} for s, b in book.items()),
                  key=lambda r: r["pnl"], reverse=True)
    unlogged = None
    if latest:
        start = next((float(e["bot_value"]) for e in events
                      if e.get("type") == "snapshot" and e.get("bot_value")), None)
        if start:
            unlogged = float(latest["bot_value"]) - start - sum(r["pnl"] for r in rows)
    return rows, trips, unlogged


def fmt_hours(h):
    if h is None:
        return "-"
    if h < 1:
        return f"{h * 60:.0f}m"
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.1f}d"


def book_section(events):
    rows, trips, unlogged = ticker_book(events)
    if not rows:
        return '<p class="empty">Fills in once the bot has traded.</p>'
    # how long it holds, and what each length of hold has earned
    brows = []
    for (cap, label), lo in zip(HOLD_BUCKETS, (0,) + tuple(c for c, _ in HOLD_BUCKETS)):
        got = [p for h, p in trips if lo <= h < cap]
        if not got:
            continue
        net = sum(got)
        brows.append(
            f'<tr><th scope="row">{label}</th><td class="num">{len(got)}</td>'
            f'<td class="num">{sum(p > 0 for p in got) / len(got):.0%}</td>'
            f'<td class="num {"up" if net > 0 else "down"}">{fmt_delta(net)}</td></tr>')
    counts = [(label, sum(lo <= h < cap for h, _ in trips))
              for (cap, label), lo in zip(HOLD_BUCKETS, (0,) + tuple(c for c, _ in HOLD_BUCKETS))]
    usual = max(counts, key=lambda c: c[1])[0].lower() if trips else ""
    holds_html = (
        f'<p class="note">{len(trips)} closed trades. Most were held '
        f'<b>{usual}</b>.</p>'
        '<table class="tbl"><thead><tr><th>Held for</th><th class="num">Trades</th>'
        '<th class="num">Won</th><th class="num">Net P&amp;L</th></tr></thead>'
        f'<tbody>{"".join(brows)}</tbody></table>' if trips else "")

    big = max(abs(r["pnl"]) for r in rows) or 1

    def line(r):
        n = len(r["holds"])
        med = sorted(r["holds"])[n // 2] if n else None
        bits = []
        if n:
            bits.append(f'{n} trade{"s" if n != 1 else ""}, typically held {fmt_hours(med)}, '
                        f'{r["wins"]} won')
        if r["open"] is not None:
            bits.append(f'holding now ({fmt_hours(r["open"])})')
        w = abs(r["pnl"]) / big * 50
        side = "win" if r["pnl"] >= 0 else "loss"
        return (f'<li><b class="tk">{esc(r["sym"])}</b>'
                f'<span class="dv"><i class="{side}" style="width:{w:.1f}%"></i></span>'
                f'<span class="num {"up" if r["pnl"] > 0 else "down" if r["pnl"] < 0 else ""}">'
                f'{fmt_delta(r["pnl"])}</span>'
                f'<small>{"; ".join(bits)}</small></li>')

    top = [r for r in rows if r["pnl"] > 0][:6]
    worst = [r for r in reversed(rows) if r["pnl"] < 0][:6]
    rest = [r for r in rows if r not in top and r not in worst]
    html = holds_html
    html += ('<h3>Made the most</h3><ul class="book">' + "".join(map(line, top)) + "</ul>"
             if top else "")
    html += ('<h3>Lost the most</h3><ul class="book">' + "".join(map(line, worst)) + "</ul>"
             if worst else "")
    if rest:
        html += (f'<details><summary>The other {len(rest)} tickers</summary>'
                 f'<ul class="book">{"".join(map(line, rest))}</ul></details>')
    if unlogged is not None and abs(unlogged) >= 1:
        html += (f'<p class="note">Net P&amp;L counts closed trades plus what is open '
                 f'now. Another <b class="{"up" if unlogged > 0 else "down"}">'
                 f'{fmt_delta(unlogged)}</b> of the account&rsquo;s change came from '
                 f'fills the log missed, so it is not pinned on any ticker.</p>')
    return html


def render(events, demo=False, market=None):
    snapshots = [e for e in events if e.get("type") == "snapshot"]
    orders = [e for e in events if e.get("type") == "order"]
    failed = [e for e in events if e.get("type") == "order_failed"]
    decision_events = [e for e in events if e.get("type") == "decisions"]
    errors = [e for e in events if e.get("type") == "llm_error"]
    series = build_series(snapshots)
    pcts = pct_series(series)
    latest = snapshots[-1] if snapshots else {}
    bot = float(latest.get("bot_value") or 0)
    start = series["bot"][0][1] if series.get("bot") else 0
    first_day = when_date(series["bot"][0][0]) if series.get("bot") else ""
    now = datetime.now(timezone.utc)

    head, gaps = verdict(pcts)
    if series.get("bot"):
        lede = (f'Started with {fmt_money(start)} on {first_day}. '
                f'SPY and QQQ show what the same money would be worth bought on '
                f'that day and simply held.')
    else:
        lede = "The page fills in after the bot's first run."

    # scoreboard: one row per contender, bot first
    board = []
    for name in ("bot", "QQQ", "SPY"):
        if name not in series:
            continue
        pts = series[name]
        ret = pcts[name][-1][1]
        board.append(
            f'<tr class="row-{name.lower()}"><th scope="row"><i></i>'
            f'{"The bot" if name == "bot" else name + " held"}</th>'
            f'<td class="num big {"up" if ret > 0 else "down" if ret < 0 else ""}">{ret:+.2f}%</td>'
            f'<td class="num">{fmt_money(pts[-1][1])}</td>'
            f'<td class="num opt">{max_drawdown(pts):.2f}%</td></tr>')
    board_html = (
        '<table class="board"><thead><tr><th></th><th class="num">Return</th>'
        '<th class="num">Worth now</th><th class="num opt">Worst drop</th></tr></thead>'
        f'<tbody>{"".join(board)}</tbody></table>' if board else "")

    # holdings
    cash = latest.get("cash")
    positions = {s: p for s, p in (latest.get("positions") or {}).items()
                 if is_held(p)}
    opened = opened_at(orders, set(positions))
    if positions:
        hrows = []
        for sym, p in sorted(positions.items(), key=lambda kv: -float(kv[1].get("value") or 0)):
            value = float(p.get("value") or 0)
            w = value / bot * 100 if bot else 0
            pl, plp = float(p.get("pl") or 0), float(p.get("pl_pct") or 0)
            o = opened.get(sym) or {}
            why = esc((o.get("reasoning") or "").strip())
            held = age(o.get("timestamp"), now) if o else ""
            hrows.append(
                f'<li><div class="h-top"><b class="tk">{esc(sym)}</b>'
                f'<span class="h-val">{fmt_money(value)}</span>'
                f'<span class="h-pl {"up" if pl > 0 else "down" if pl < 0 else ""}">'
                f'{plp:+.2f}%<small> {fmt_delta(pl)}</small></span></div>'
                f'<div class="weight"><i style="width:{min(w, 100):.1f}%"></i>'
                f'<span>{w:.0f}% of the account</span></div>'
                f'<p class="h-meta">{float(p.get("shares") or 0):,.2f} sh at '
                f'{fmt_money(float(p.get("avg_entry") or 0))}, now '
                f'{fmt_money(float(p.get("last") or 0))}'
                f'{f" &middot; held {held}" if held else ""}</p>'
                f'{f"<p class=why>Bought because: {why}</p>" if why else ""}</li>')
        if cash is not None:
            cw = float(cash) / bot * 100 if bot else 0
            hrows.append(
                f'<li class="cash"><div class="h-top"><b class="tk">Cash</b>'
                f'<span class="h-val">{fmt_money(float(cash))}</span>'
                f'<span class="h-pl">idle</span></div>'
                f'<div class="weight"><i style="width:{min(cw, 100):.1f}%"></i>'
                f'<span>{cw:.0f}% of the account</span></div></li>')
        held_html = f'<ul class="holdings">{"".join(hrows)}</ul>'
    elif latest.get("held"):
        # snapshots logged before position detail existed: tickers only
        held_html = ('<ul class="held">'
                     + "".join(f"<li>{esc(h)}</li>" for h in latest["held"]) + "</ul>")
    else:
        held_html = '<p class="empty">No open positions. Everything is in cash.</p>'

    # last LLM failure, if newer than the last decision
    err_html = ""
    if errors and (not decision_events
                   or errors[-1].get("timestamp", "") > decision_events[-1].get("timestamp", "")):
        err = errors[-1]
        err_html = (f'<section class="alert"><h2>Last run failed</h2>'
                    f'<p class="note">{when(err.get("timestamp"))} &middot; '
                    f'{esc(err.get("model", "?"))}</p>'
                    f'<pre>{esc(err.get("error", ""))}</pre></section>')

    # the most recent decision, every name it looked at
    last_call = decision_events[-1] if decision_events else None
    if last_call:
        calls = last_call.get("decisions") or []
        crow = "".join(
            f'<li><span class="act {esc(d.get("action") or "hold")}">'
            f'{esc((d.get("action") or "?").upper())}</span>'
            f'<b class="tk">{esc(d.get("ticker", "?"))}</b>'
            f'<p>{esc((d.get("reasoning") or "").strip()) or "-"}</p></li>'
            for d in calls)
        acted = sum(1 for d in calls if d.get("action") in ("buy", "sell"))
        usage = last_call.get("usage") or {}
        toks = (f' &middot; {usage.get("prompt_tokens", "?")} in / '
                f'{usage.get("completion_tokens", "?")} out' if usage else "")
        headlines = (last_call.get("news") or []) + (last_call.get("market_news") or [])
        extra = ""
        if headlines:
            items = "".join(
                f'<li><b>{esc(", ".join(n.get("symbols") or []) or "market")}</b> '
                f'{esc(n.get("headline", ""))} '
                f'<small>{esc(n.get("source", ""))} &middot; {esc(n.get("when", ""))}</small></li>'
                for n in headlines)
            extra += (f'<details><summary>Headlines it read ({len(headlines)})</summary>'
                      f'<ul class="news">{items}</ul></details>')
        if last_call.get("thinking"):
            extra += (f'<details><summary>Model thinking '
                      f'({len(last_call["thinking"]):,} chars)</summary>'
                      f'<pre>{esc(last_call["thinking"])}</pre></details>')
        if last_call.get("raw"):
            extra += (f'<details><summary>Raw model response</summary>'
                      f'<pre>{esc(last_call["raw"])}</pre></details>')
        day = last_call.get("day") or {}
        day_line = (f'{esc(day.get("weekday", ""))} {esc(day.get("date", ""))}, '
                    f'{esc(day.get("status", ""))}. ' if day else "")
        think_html = (
            f'<p class="note"><b>{when(last_call.get("timestamp"))}</b>. {day_line}'
            f'Looked at {len(last_call.get("candidates") or [])} candidates and '
            f'acted on {acted}.<br><small>{esc(last_call.get("model", "?"))}{toks}</small></p>'
            f'<ul class="calls">{crow}</ul>{extra}')
    else:
        think_html = '<p class="empty">No decisions logged yet. This fills in on the next run.</p>'

    # trade log, newest session first
    sessions = {}
    for o in reversed(orders[-40:]):
        sessions.setdefault((o.get("timestamp") or "")[:10], []).append(o)
    log_parts = []
    for d, items in sessions.items():
        try:
            label = datetime.fromisoformat(d).strftime("%a %d %b")
        except ValueError:
            label = d or "undated"
        rows = []
        for o in items:
            spec = o.get("order") or {}
            side = (spec.get("side") or "").lower()
            rows.append(
                f'<li><span class="t">{when_time(o.get("timestamp"))}</span>'
                f'<span class="act {esc(side)}">{esc(side.upper())}</span>'
                f'<b class="tk">{esc(spec.get("symbol", "?"))}</b>'
                f'<span class="sz">{trade_size(o)}</span>'
                f'<p>{esc((o.get("reasoning") or "").strip()) or "-"}</p></li>')
        log_parts.append(f'<h3>{label} <small>({len(rows)})</small></h3>'
                         f'<ul class="trades">{"".join(rows)}</ul>')
    log_html = "".join(log_parts) or '<p class="empty">No orders yet.</p>'

    closes = session_closes(series)
    # the chart's raw readings, for the zoom and filter script; aligned by
    # snapshot, null where a series has no reading
    snaps = [s for s in snapshots if s.get("bot_value") is not None]
    chart_data = json.dumps({
        "t": [s.get("timestamp") for s in snaps],
        **{name: [(s.get("baselines") or {}).get(name) if name != "bot"
                  else s.get("bot_value") for s in snaps]
           for name in series},
    }, separators=(",", ":")).replace("</", "<\\/")
    token_line = (f' &middot; token <b>{DASHBOARD_TOKEN}</b>' if DASHBOARD_TOKEN else "")
    banner = ('<div class="banner">DEMO DATA. Synthetic, generated by '
              '<code>--demo</code> to preview the layout. Not real results.</div>'
              if demo else "")
    counts = (f'{len(snapshots):,} runs &middot; {len(orders):,} orders filled'
              + (f' &middot; {len(failed)} rejected' if failed else ""))

    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Bot vs Market</title>
<script>{HEAD_JS}</script>
<style>{CSS}</style>
<main>
  {banner}
  <header class="mast">
    <p class="kicker">LLM paper trader &middot; scored against buy-and-hold
      <button type="button" class="theme" id="theme" aria-label="Colour theme">Theme: auto</button></p>
    <p class="stamp">Updated {when_full(latest.get('timestamp'))}{market_line(market)}{token_line}</p>
  </header>

  <section class="hero">
    {head}
    <p class="gaps">{gaps}</p>
    <p class="lede">{lede}</p>
  </section>

  {err_html}

  <section>
    {board_html}
  </section>

  <section>
    <h2>Return since the start</h2>
    <div class="controls" role="group" aria-label="Chart range and lines">
      <span class="seg" id="range"><button data-days="1" aria-pressed="false">1D</button><button data-days="7" aria-pressed="false">1W</button><button data-days="30" aria-pressed="false">1M</button><button data-days="0" aria-pressed="true">All</button></span>
      <span class="seg" id="lines"><button data-s="bot" aria-pressed="true" class="k-bot">Bot</button><button data-s="QQQ" aria-pressed="true" class="k-qqq">QQQ</button><button data-s="SPY" aria-pressed="true" class="k-spy">SPY</button></span>
    </div>
    {chart(series)}
    <p class="note hint">Drag across the chart to zoom in, tap or hover for values. Returns
    are measured from the left edge of whatever range is showing.</p>
    <script type="application/json" id="chart-data">{chart_data}</script>
  </section>

  <section>
    <h2>Day by day against QQQ</h2>
    {day_strip(closes)}
  </section>

  <section>
    <h2>What it holds now</h2>
    {held_html}
  </section>

  <section><details class="fold" open>
    <summary><h2>Where the money came from</h2></summary>
    {book_section(events)}
  </details></section>

  <section><details class="fold" open>
    <summary><h2>Its latest decision</h2></summary>
    {think_html}
  </details></section>

  <section><details class="fold" open>
    <summary><h2>Trades</h2></summary>
    <p class="note">Most recent 40, grouped by US trading day. Times in WIB.</p>
    {log_html}
  </details></section>

  <footer>
    <p>{counts}</p>
  </footer>
</main>
<script>{CHART_JS}</script>
</html>
"""


def fmt_shares(q):
    """176.49 sh, but 0.000412 sh: two decimals would show residue as 0.00."""
    return f"{q:,.2f}" if q >= 1 else f"{q:.6g}"


def trade_size(o):
    """Total, shares and price per share, the same three for every trade.

    Read off the fill, not the request: a buy asks for dollars and a sell
    for shares, and showing whichever was asked made the rows unalike.
    """
    spec = o.get("order") or {}
    try:
        qty, px = float(o["filled_qty"]), float(o["filled_avg_price"])
    except (KeyError, TypeError, ValueError):
        asked = (fmt_money(float(spec["notional"])) if spec.get("notional") is not None
                 else f'{fmt_shares(float(spec.get("qty") or 0))} sh')
        return f'{asked} <i>not filled</i>'
    return (f'<b>{fmt_money(qty * px)}</b>'
            f'<span>{fmt_shares(qty)} sh &times; {fmt_money(px)}</span>')


def when_time(iso):
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(WIB).strftime("%H:%M")
    except ValueError:
        return ""


# Light is the default. Dark follows the device unless the reader picked a
# theme with the toggle, which sets data-theme on <html>.
_DARK = """
  --paper:#14130f; --sheet:#1b1a15; --ink:#ece7da; --soft:#b7b0a2; --faint:#857f73;
  --rule:#36332b; --up:#62c48a; --down:#ef7a6c; --bot:#ff7f45; --qqq:#82a6ff; --spy:#a49e91;
  color-scheme:dark;"""

CSS = """
:root{
  --paper:#f3efe6; --sheet:#faf8f2; --ink:#1c1a16; --soft:#57524a; --faint:#8a8478;
  --rule:#d6cfbf; --up:#1e6b3c; --down:#b02a1f; --bot:#c4410f; --qqq:#2a55b8; --spy:#8b8577;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Consolas,"Liberation Mono",monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){""" + _DARK + """}}
:root[data-theme=dark]{""" + _DARK + """}
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--sans);
  -webkit-text-size-adjust:100%}
main{max-width:860px;margin:0 auto;padding:28px 16px 48px}
.up{color:var(--up)} .down{color:var(--down)}
.banner{background:var(--ink);color:var(--paper);padding:10px 14px;font-weight:600;
  font-size:14px;margin-bottom:20px}
.banner code{font-family:var(--mono)}
.mast{border-top:3px solid var(--ink);border-bottom:1px solid var(--ink);padding:8px 0;
  display:flex;flex-wrap:wrap;justify-content:space-between;gap:4px 16px}
.kicker{margin:0;font:600 12px/1.4 var(--sans);letter-spacing:.08em;text-transform:uppercase}
.stamp{margin:0;font-size:13px;color:var(--soft)}
.mkt{font-weight:600}.mkt.on{color:var(--up)}.mkt.off{color:var(--ink)}
.hero{padding:30px 0 22px;border-bottom:1px solid var(--rule)}
.verdict{font:600 clamp(34px,7vw,58px)/1.02 var(--serif);margin:0 0 14px;letter-spacing:-.01em}
.gaps{margin:0 0 14px;display:flex;flex-wrap:wrap;gap:6px 22px}
.gap{font:600 22px/1.2 var(--mono);font-variant-numeric:tabular-nums}
.gap small{font:500 13px var(--sans);color:var(--soft);margin-left:2px}
.lede{margin:0;color:var(--soft);max-width:60ch}
section{padding:22px 0;border-bottom:1px solid var(--rule)}
h2{font:600 13px/1.3 var(--sans);letter-spacing:.08em;text-transform:uppercase;margin:0 0 14px}
h3{font:600 15px/1.3 var(--serif);margin:18px 0 6px}
h3 small{font:400 13px var(--sans);color:var(--faint)}
.note{color:var(--soft);font-size:14px;margin:0 0 12px}
.empty{color:var(--soft);font-style:italic;margin:0}
.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.board{width:100%;border-collapse:collapse}
.board th,.board td{padding:10px 0 10px 12px;border-top:1px solid var(--rule)}
.board thead th{border-top:0;padding-top:0;font:600 11px var(--sans);letter-spacing:.08em;
  text-transform:uppercase;color:var(--faint)}
.board tbody th{text-align:left;padding-left:0;font-weight:600;white-space:nowrap}
.board tbody th i{display:inline-block;width:14px;height:3px;margin:0 9px 4px 0;vertical-align:middle}
.row-bot th i{background:var(--bot);height:4px}.row-qqq th i{background:var(--qqq)}.row-spy th i{background:var(--spy)}
.row-bot{background:var(--sheet)}
.big{font-size:20px;font-weight:600}
.chart{display:grid;grid-template-columns:1fr 92px;grid-template-rows:auto auto;column-gap:8px}
.plot{position:relative;height:300px;margin-left:44px;border-left:1px solid var(--rule)}
.plot svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.grid{stroke:var(--rule);stroke-width:1;vector-effect:non-scaling-stroke}
.zero{stroke:var(--faint);stroke-width:1;stroke-dasharray:4 4;vector-effect:non-scaling-stroke}
.ln{fill:none;stroke-width:1.6;stroke-linejoin:round;vector-effect:non-scaling-stroke}
.ln-bot{stroke:var(--bot);stroke-width:2.6}.ln-qqq{stroke:var(--qqq)}.ln-spy{stroke:var(--spy)}
.ytick{position:absolute;left:-46px;width:40px;text-align:right;transform:translateY(-50%);font:12px var(--mono);color:var(--faint)}
.ytick.zero{color:var(--soft)}
.rail{position:relative;height:300px}
.end{position:absolute;left:0;transform:translateY(-50%);font:12px/1.2 var(--mono);white-space:nowrap}
.end b{font:600 12px var(--sans);display:block}
.end-bot{color:var(--bot)}.end-qqq{color:var(--qqq)}.end-spy{color:var(--soft)}
.xaxis{display:flex;justify-content:space-between;font:12px var(--mono);color:var(--faint);padding:6px 0 0 44px}
.strip{display:flex;align-items:stretch;gap:2px;height:120px;position:relative;margin-bottom:10px}
.strip::after{content:"";position:absolute;left:0;right:0;top:50%;border-top:1px solid var(--faint)}
.bar{flex:1;display:flex;flex-direction:column;min-width:3px}
.bar i{display:block}
.bar.win{justify-content:flex-end;height:50%}.bar.win i{background:var(--up)}
.bar.loss{justify-content:flex-start;height:50%;margin-top:auto}.bar.loss i{background:var(--down)}
ul{list-style:none;margin:0;padding:0}
.holdings li{padding:14px 0;border-top:1px solid var(--rule)}
.holdings li:first-child{border-top:0;padding-top:0}
.h-top{display:flex;align-items:baseline;gap:12px}
.tk{font:700 16px var(--mono);letter-spacing:.02em}
.h-val{margin-left:auto;font-family:var(--mono);font-variant-numeric:tabular-nums}
.h-pl{font:600 15px var(--mono);font-variant-numeric:tabular-nums;min-width:92px;text-align:right}
.h-pl small{display:block;font-weight:400;font-size:12px;color:var(--soft)}
.weight{position:relative;height:18px;background:var(--sheet);margin:8px 0 6px;border:1px solid var(--rule)}
.weight i{position:absolute;inset:0 auto 0 0;background:var(--ink);opacity:.82}
.cash .weight i{opacity:.25}
.weight span{position:absolute;right:6px;top:0;font:11px/16px var(--mono);color:var(--soft)}
.h-meta{margin:0;font-size:13px;color:var(--soft)}
.why{margin:6px 0 0;font:15px/1.45 var(--serif);color:var(--ink)}
ul.held{display:flex;flex-wrap:wrap;gap:8px}
ul.held li{border:1px solid var(--ink);padding:2px 10px;font:700 14px var(--mono)}
.act{display:inline-block;min-width:44px;font:700 11px/18px var(--mono);letter-spacing:.06em;
  text-align:center;border:1px solid currentColor;margin-right:10px}
.act.buy{color:var(--up)}.act.sell{color:var(--down)}.act.hold{color:var(--faint)}
.calls li,.trades li{padding:10px 0;border-top:1px solid var(--rule)}
.calls li:first-child,.trades li:first-child{border-top:0}
.calls p,.trades p{margin:4px 0 0;color:var(--soft);font-size:14px;line-height:1.5}
.trades li{display:grid;grid-template-columns:44px auto auto 1fr;align-items:baseline;column-gap:0}
.trades .t{font:12px var(--mono);color:var(--faint)}
.trades .sz{font:13px var(--mono);color:var(--soft);text-align:right}
.trades .sz b{color:var(--ink);font-weight:600}
.trades .sz span{margin-left:10px}
.trades .sz i{font-style:normal;color:var(--down);margin-left:6px}
.trades p{grid-column:2/-1}
details{margin-top:14px;border-top:1px solid var(--rule);padding-top:10px}
summary{cursor:pointer;font-size:14px;color:var(--soft)}
summary:hover{color:var(--ink)}
.news li{padding:6px 0;font-size:14px;border-top:1px dotted var(--rule)}
.news small{color:var(--faint);white-space:nowrap}
pre{font:12px/1.5 var(--mono);white-space:pre-wrap;word-break:break-word;background:var(--sheet);
  border:1px solid var(--rule);padding:12px;margin:8px 0 0;max-height:420px;overflow:auto}
.alert{border:2px solid var(--down);padding:14px;margin-top:22px}
.alert h2{color:var(--down)}
.theme{float:right;font:500 12px var(--sans);letter-spacing:0;text-transform:none;color:var(--ink);
  background:none;border:1px solid var(--rule);padding:1px 8px;cursor:pointer;display:none}
.js .theme{display:inline-block}
.controls{display:none;flex-wrap:wrap;gap:8px 14px;margin:0 0 12px}
.js .controls{display:flex}
.seg{display:inline-flex;border:1px solid var(--ink)}
.seg button{font:600 12px var(--mono);background:none;color:var(--soft);border:0;
  border-left:1px solid var(--rule);padding:5px 11px;cursor:pointer;min-height:30px}
.seg button:first-child{border-left:0}
.seg button[aria-pressed=true]{background:var(--ink);color:var(--paper)}
#lines button[aria-pressed=false]{text-decoration:line-through;opacity:.6}
#lines button.k-bot[aria-pressed=true]{background:var(--bot);color:var(--paper)}
#lines button.k-qqq[aria-pressed=true]{background:var(--qqq);color:var(--paper)}
#lines button.k-spy[aria-pressed=true]{background:var(--spy);color:var(--paper)}
.hint{display:none;margin-top:10px}.js .hint{display:block}
.plot{touch-action:pan-y;cursor:crosshair;user-select:none;-webkit-user-select:none}
.cur{position:absolute;top:0;bottom:0;width:0;border-left:1px solid var(--soft);pointer-events:none}
.tip{position:absolute;top:6px;background:var(--paper);border:1px solid var(--ink);padding:6px 9px;
  font:12px/1.5 var(--mono);white-space:nowrap;pointer-events:none;z-index:2}
.tip b{font-family:var(--sans)}
.brush{position:absolute;top:0;bottom:0;background:var(--ink);opacity:.1;pointer-events:none}
.tbl{width:100%;border-collapse:collapse;margin-bottom:6px}
.tbl th,.tbl td{padding:8px 0 8px 12px;border-top:1px solid var(--rule);text-align:left}
.tbl thead th{border-top:0;padding-top:0;font:600 11px var(--sans);letter-spacing:.08em;
  text-transform:uppercase;color:var(--faint)}
.tbl th:first-child{padding-left:0;font-weight:500}
.tbl .num{text-align:right}
.book li{display:grid;grid-template-columns:64px 1fr 96px;align-items:center;column-gap:10px;
  padding:8px 0;border-top:1px solid var(--rule)}
.book li:first-child{border-top:0}
.book small{grid-column:1/-1;color:var(--soft);font-size:13px;margin-top:2px}
.dv{position:relative;height:12px}
.dv::before{content:"";position:absolute;left:50%;top:-4px;bottom:-4px;border-left:1px solid var(--faint)}
.dv i{position:absolute;top:0;bottom:0}
.dv i.win{left:50%;background:var(--up)}.dv i.loss{right:50%;background:var(--down)}
/* the lower sections fold away under their own heading */
details.fold{margin:0;border:0;padding:0}
.fold>summary{list-style:none;display:flex;align-items:center;justify-content:space-between;
  color:var(--ink);padding:2px 0}
.fold>summary::-webkit-details-marker{display:none}
.fold>summary h2{margin:0}
.fold>summary::after{content:"Hide";font:500 12px var(--sans);color:var(--soft);
  border:1px solid var(--rule);padding:1px 8px}
.fold:not([open])>summary::after{content:"Show"}
.fold[open]>summary{margin-bottom:14px}
.fold>summary:focus-visible{outline:2px solid var(--ink);outline-offset:3px}
footer{padding-top:20px;font-size:13px;color:var(--soft)}
footer p{margin:0 0 8px;max-width:66ch}
@media (max-width:600px){
  main{padding-top:16px}
  .hero{padding:22px 0 18px}
  .gap{font-size:19px}
  .opt{display:none}
  .board th,.board td{padding-left:8px}
  .big{font-size:18px}
  .chart{grid-template-columns:1fr 74px}
  .plot,.rail{height:220px}
  .strip{height:90px}
  .h-top{flex-wrap:wrap}
  .trades li{grid-template-columns:40px auto 1fr}
  .trades .sz{grid-column:2/-1;text-align:left;margin-top:2px}
}
"""



# Runs before first paint: apply a saved theme so the page never flashes the
# wrong colours, and mark that scripts work so the controls show.
HEAD_JS = """
try{var t=localStorage.getItem('theme');if(t)document.documentElement.dataset.theme=t}catch(e){}
document.documentElement.classList.add('js');
"""

# The static page is complete without this. It only adds the theme toggle,
# and range, line and drag-to-zoom controls on the return chart, redrawing
# the same markup chart() writes.
CHART_JS = r"""
(function(){
var root=document.documentElement, btn=document.getElementById('theme');
var modes=['auto','light','dark'];
function showTheme(){btn.textContent='Theme: '+(root.dataset.theme||'auto')}
btn.onclick=function(){
  var m=modes[(modes.indexOf(root.dataset.theme||'auto')+1)%3];
  if(m==='auto')delete root.dataset.theme;else root.dataset.theme=m;
  try{m==='auto'?localStorage.removeItem('theme'):localStorage.setItem('theme',m)}catch(e){}
  showTheme();
};
showTheme();

var src=document.getElementById('chart-data'), box=document.querySelector('[data-chart]');
if(!src||!box)return;
var D=JSON.parse(src.textContent), N=D.t.length;
if(N<2)return;
var H=300, ORDER=['SPY','QQQ','bot'], LABEL={bot:'Bot',QQQ:'QQQ',SPY:'SPY'};
var on={bot:true,QQQ:true,SPY:true}, i0=0, i1=N-1, view=null;
var T=D.t.map(function(s){return Date.parse(s)});
var MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
// axis dates are US session dates (UTC), the same as the static chart
function day(i){var d=new Date(T[i]);return ('0'+d.getUTCDate()).slice(-2)+' '+MON[d.getUTCMonth()]}
function stamp(i){var d=new Date(T[i]+7*36e5);                 // WIB is UTC+7, no DST
  return ('0'+d.getUTCDate()).slice(-2)+' '+MON[d.getUTCMonth()]+', '+('0'+d.getUTCHours()).slice(-2)+':'+('0'+d.getUTCMinutes()).slice(-2)+' WIB'}
function pct(v){return (v>=0?'+':'')+v.toFixed(2)+'%'}
function niceStep(span){var s=[.05,.1,.25,.5,1,2,2.5,5,10,20,25,50];for(var k=0;k<s.length;k++)if(span/s[k]<=6)return s[k];return 100}

function draw(){
  var S={}, vals=[0];
  ORDER.forEach(function(n){
    if(!on[n]||!D[n])return;
    var base=null, out=[];
    for(var i=i0;i<=i1;i++){var v=D[n][i];if(v!=null&&base==null)base=v;out.push(v==null||!base?null:(v/base-1)*100)}
    S[n]=out; out.forEach(function(v){if(v!=null)vals.push(v)});
  });
  var lo=Math.min.apply(null,vals), hi=Math.max.apply(null,vals), sp=(hi-lo)||1;
  lo-=sp*.06; hi+=sp*.06;
  var st=niceStep(hi-lo), m=Math.max(i1-i0,1);
  function y(v){return (hi-v)/(hi-lo)*100}
  var svg='', ticks='', ends=[];
  for(var k=Math.ceil(lo/st);k*st<=hi;k++){
    var t=k*st, yy=y(t), yp=(yy*H/100).toFixed(1), z=Math.abs(t)<1e-9;
    svg+='<line x1="0" x2="1000" y1="'+yp+'" y2="'+yp+'" class="'+(z?'zero':'grid')+'"/>';
    ticks+='<span class="ytick'+(z?' zero':'')+'" style="top:'+yy.toFixed(2)+'%">'+(z?'0%':(t>0?'+':'')+(+t.toFixed(2))+'%')+'</span>';
  }
  Object.keys(S).forEach(function(n){
    var pts=[], last=null;
    S[n].forEach(function(v,j){if(v!=null){pts.push((1000*j/m).toFixed(1)+','+(y(v)*H/100).toFixed(1));last=v}});
    svg+='<polyline class="ln ln-'+n.toLowerCase()+'" points="'+pts.join(' ')+'"/>';
    if(last!=null)ends.push([y(last),n,last]);
  });
  ends.sort(function(a,b){return a[0]-b[0]});
  for(var e=1;e<ends.length;e++)ends[e][0]=Math.max(ends[e][0],ends[e-1][0]+9);
  var plot=box.querySelector('.plot');
  plot.innerHTML='<svg viewBox="0 0 1000 '+H+'" preserveAspectRatio="none" role="img" aria-label="Return over the selected range">'+svg+'</svg>'+ticks;
  box.querySelector('.rail').innerHTML=ends.map(function(e){
    return '<span class="end end-'+e[1].toLowerCase()+'" style="top:'+e[0].toFixed(2)+'%"><b>'+LABEL[e[1]]+'</b> '+pct(e[2])+'</span>'}).join('');
  box.querySelector('.xaxis').innerHTML='<span>'+day(i0)+'</span><span>'+day(Math.round((i0+i1)/2))+'</span><span>'+day(i1)+'</span>';
  view={S:S,m:m};
  cur=tip=null;
}

function setRange(days){
  i1=N-1; i0=0;
  if(days){var cut=T[N-1]-days*864e5;while(i0<N-2&&T[i0]<cut)i0++}
  document.querySelectorAll('#range button').forEach(function(b){b.setAttribute('aria-pressed',String(+b.dataset.days===days))});
  draw();
}
document.querySelectorAll('#range button').forEach(function(b){b.onclick=function(){setRange(+b.dataset.days)}});
document.querySelectorAll('#lines button').forEach(function(b){b.onclick=function(){
  var n=b.dataset.s, left=Object.keys(on).filter(function(k){return on[k]&&D[k]}).length;
  if(on[n]&&left===1)return;                       // keep at least one line
  on[n]=!on[n]; b.setAttribute('aria-pressed',String(on[n])); draw();
}});

// hover or tap for values, drag across to zoom
var plotEl=box.querySelector('.plot'), drag=null, brush=null, cur=null, tip=null;
function frac(ev){var r=plotEl.getBoundingClientRect();return Math.min(Math.max((ev.clientX-r.left)/r.width,0),1)}
function clearHover(){if(cur){cur.remove();cur=null}if(tip){tip.remove();tip=null}}
function hover(f){
  if(!view)return;
  var j=Math.round(f*view.m), i=i0+j, x=j/view.m*100;
  if(!cur){cur=document.createElement('div');cur.className='cur';tip=document.createElement('div');tip.className='tip';
    plotEl.appendChild(cur);plotEl.appendChild(tip)}
  cur.style.left=x+'%';
  tip.innerHTML=stamp(i)+Object.keys(view.S).slice().reverse().map(function(n){
    var v=view.S[n][j];return v==null?'':'<br><b>'+LABEL[n]+'</b> '+pct(v)}).join('');
  tip.style.left=f>0.55?'':(x+2)+'%';
  tip.style.right=f>0.55?(100-x+2)+'%':'';
}
plotEl.addEventListener('pointerdown',function(ev){if(!ev.button)drag={f:frac(ev),x:ev.clientX}});
plotEl.addEventListener('pointermove',function(ev){
  var f=frac(ev);
  if(drag&&Math.abs(ev.clientX-drag.x)>8){
    clearHover();
    if(!brush){brush=document.createElement('div');brush.className='brush';plotEl.appendChild(brush)}
    brush.style.left=Math.min(f,drag.f)*100+'%'; brush.style.width=Math.abs(f-drag.f)*100+'%';
  }else hover(f);
});
function endDrag(ev){
  if(!drag)return;
  if(brush){
    var a=Math.min(drag.f,frac(ev)), b=Math.max(drag.f,frac(ev)), m=i1-i0;
    var n0=i0+Math.round(a*m), n1=i0+Math.round(b*m);
    brush.remove();brush=null;
    if(n1-n0>=3){i0=n0;i1=n1;
      document.querySelectorAll('#range button').forEach(function(x){x.setAttribute('aria-pressed','false')});
      draw()}
  }
  drag=null;
}
plotEl.addEventListener('pointerup',endDrag);
plotEl.addEventListener('pointercancel',function(){drag=null;if(brush){brush.remove();brush=null}});
plotEl.addEventListener('pointerleave',function(ev){if(ev.pointerType==='mouse'){endDrag(ev);clearHover()}});
draw();
})();
"""

# ----------------------------- ENTRY --------------------------------------

def demo_events(runs=40):
    """Synthetic log so the layout can be checked before real data exists."""
    random.seed(7)
    now = datetime.now(timezone.utc) - timedelta(hours=4 * runs)
    bot = spy = qqq = 100_000.0
    names = ["NVDA", "TSLA", "AMD", "PLTR", "SMCI"]
    events = []
    for i in range(runs):
        stamp = (now + timedelta(hours=4 * i)).isoformat()
        bot *= 1 + random.gauss(0.0004, 0.004)
        spy *= 1 + random.gauss(0.0002, 0.0015)
        qqq *= 1 + random.gauss(0.0003, 0.0022)
        picks = random.sample(names, 4)
        events.append({
            "type": "decisions", "timestamp": stamp,
            "candidates": names + ["SPY", "QQQ"],
            "day": {"weekday": "Thursday", "date": stamp[:10],
                    "status": "open, 122 min to the close",
                    "next_trading_day": "2026-08-31",
                    "days_until_next_session": 3},
            "news": [{"when": stamp[:10], "symbols": [picks[0]],
                      "source": "benzinga",
                      "headline": f"{picks[0]} guides above consensus for Q3"}],
            "model": "deepseek/deepseek-v4-flash",
            "usage": {"prompt_tokens": 1840, "completion_tokens": 412},
            "thinking": "Scanning the pool for anything with a real intraday "
                        "trend. Most names are flat. Checking volume next.",
            "raw": '{"decisions": [{"ticker": "NVDA", "action": "hold", '
                   '"reasoning": "flat tape"}]}',
            "decisions": [
                {"ticker": n, "action": "hold",
                 "reasoning": f"{n} is drifting sideways on thinning volume; "
                              f"no edge worth paying the spread for."}
                for n in picks],
        })
        if i % 5 == 2:
            sym = random.choice(names)
            events.append({
                "type": "order", "timestamp": stamp, "status": "filled",
                "order": {"symbol": sym, "side": "buy", "notional": 2500.0},
                "filled_avg_price": round(random.uniform(20, 400), 2),
                "reasoning": f"{sym} is up on heavy volume and holding its "
                             f"intraday range; adding a small position.",
            })
        held = sorted(random.sample(names, 3))
        events.append({
            "type": "snapshot", "timestamp": stamp, "bot_value": round(bot, 2),
            "cash": round(bot * 0.38, 2),
            "baselines": {"SPY": round(spy, 2), "QQQ": round(qqq, 2)},
            "positions": {
                h: {"shares": round(random.uniform(5, 90), 4),
                    "avg_entry": round(random.uniform(20, 400), 2),
                    "last": round(random.uniform(20, 400), 2),
                    "value": round(bot * 0.62 / 3, 2),
                    "pl": round(random.uniform(-800, 900), 2),
                    "pl_pct": round(random.uniform(-6, 7), 2)}
                for h in held},
            "held": held,
        })
    return events


def selfcheck():
    """Renders both empty and populated states without blowing up."""
    assert read_events(HERE / "does_not_exist.jsonl") == []
    empty = render([])
    assert "No orders yet" in empty and "Not enough data" in empty
    assert "Waiting for the first run" in empty
    page = render(demo_events(), demo=True)
    # one stretching chart, one line per series, labels in HTML so they keep
    # their size and place at any width
    markup = page.split(CHART_JS)[0]          # the script quotes the same tags
    assert page.count(CHART_JS) == 1 and page.count(HEAD_JS) == 1
    assert markup.count("<polyline") == 3, "expected one line per series"
    assert markup.count("<svg") == 1 and 'preserveAspectRatio="none"' in markup
    # the zoom script gets every reading, and nothing in it can close the tag
    data = json.loads(markup.split('id="chart-data">')[1].split("</script>")[0])
    assert len(data["t"]) == len(data["bot"]) == 40 and {"SPY", "QQQ"} <= set(data)
    for must in ("Theme: auto", 'data-days="7"', 'data-s="QQQ"', "Drag across",
                 'data-theme=dark', "Where the money came from"):
        assert must in page, must
    # everything below the holdings folds, and starts open
    lower = markup.split("What it holds now")[1]
    assert lower.count('<details class="fold" open>') == 3
    assert markup.split("What it holds now")[0].count('class="fold"') == 0
    assert "@media (max-width:600px)" in page and 'name="viewport"' in page
    for must in ("DEMO DATA", "vs SPY", "vs QQQ", "NVDA", "HOLD",
                 "Its latest decision", "Raw model response", "Model thinking",
                 "1840 in / 412 out", "candidates",
                 # the scoreboard, the day strip, holdings with their thesis
                 "The bot", "QQQ held", "Worst drop", "Day by day against QQQ",
                 "sessions won", "of the account", "Bought because:", "Cash",
                 "Headlines it read", "guides above consensus",
                 "min to the close"):
        assert must in page, must
    # the verdict follows the numbers
    def snap(t, b, s, q):
        return {"type": "snapshot", "timestamp": t, "bot_value": b,
                "baselines": {"SPY": s, "QQQ": q}}
    behind = render([snap("2026-01-01T15:00:00+00:00", 100, 100, 100),
                     snap("2026-01-02T15:00:00+00:00", 99, 101, 106)])
    assert "Behind the market." in behind and "-7.00 pts" in behind, behind
    split = render([snap("2026-01-01T15:00:00+00:00", 100, 100, 100),
                    snap("2026-01-02T15:00:00+00:00", 103, 101, 106)])
    assert "Ahead of SPY, behind QQQ." in split
    ahead = render([snap("2026-01-01T15:00:00+00:00", 100, 100, 100),
                    snap("2026-01-02T15:00:00+00:00", 110, 101, 106)])
    assert "Beating the market." in ahead and "1 of 1</b> sessions won" in ahead
    # per-ticker replay: P&L, hold time, and the exit the log never saw
    def ev(t, kind, **kw):
        return {"type": kind, "timestamp": f"2026-01-0{t}T15:00:00+00:00", **kw}
    def fill(t, sym, side, qty, px, hh="15"):
        return {"type": "order", "timestamp": f"2026-01-0{t}T{hh}:00:00+00:00",
                "order": {"symbol": sym, "side": side}, "filled_qty": str(qty),
                "filled_avg_price": px}
    log = [ev(1, "snapshot", bot_value=1000.0, positions={}),
           fill(1, "WIN", "buy", 10, 10.0, "16"), fill(3, "WIN", "sell", 10, 12.0, "16"),
           fill(1, "GONE", "buy", 5, 20.0, "16"),
           # GONE was sold, but the sell never reached the log
           ev(2, "snapshot", bot_value=990.0, positions={"WIN": {"shares": 10}}),
           fill(4, "OPEN", "buy", 2, 50.0, "14"),
           ev(4, "snapshot", bot_value=1015.0, positions={
               "OPEN": {"shares": 2, "pl": -3.0}})]
    log.sort(key=lambda e: e["timestamp"])    # as read_events delivers it
    rows, trips, unlogged = ticker_book(log)
    by = {r["sym"]: r for r in rows}
    assert by["WIN"]["pnl"] == 20 and by["WIN"]["holds"] == [48.0], by["WIN"]
    assert by["OPEN"]["pnl"] == -3 and by["OPEN"]["open"] == 1.0, by["OPEN"]
    # GONE's hold ends at the snapshot that no longer shows it; its P&L is
    # the part the log cannot see
    assert by["GONE"]["pnl"] == 0 and by["GONE"]["holds"] == [23.0], by["GONE"]
    assert sorted(trips) == [(23.0, 0.0), (48.0, 20.0)], trips
    assert round(unlogged, 2) == -2.0, unlogged
    shown = book_section(log)
    assert "Made the most" in shown and "WIN" in shown and "1 to 3 days" in shown
    assert "fills the log missed" in shown, shown
    # every trade shows total, shares and price per share, from the fill
    buy = trade_size({"order": {"notional": 20000.0}, "filled_qty": "176.4916",
                      "filled_avg_price": 113.32})
    sell = trade_size({"order": {"qty": "176.49"}, "filled_qty": "176.49",
                       "filled_avg_price": 112.64})
    assert buy == "<b>$20,000.03</b><span>176.49 sh &times; $113.32</span>", buy
    assert "<b>$19,879.83</b>" in sell and "176.49 sh &times; $112.64" in sell, sell
    assert "0.000412 sh" in trade_size({"order": {}, "filled_qty": "0.000412",
                                        "filled_avg_price": 15.63})
    assert "not filled" in trade_size({"order": {"notional": 50.0}})
    assert not is_held({"shares": 0.0001, "value": 0.01})
    assert is_held({"shares": 3}) and is_held({"shares": 0.5, "value": 40})
    assert fmt_hours(0.5) == "30m" and fmt_hours(5) == "5h" and fmt_hours(72) == "3.0d"
    # old snapshots carry tickers only; they must still render as chips
    legacy = render([{"type": "snapshot", "timestamp": "2026-01-01T00:00:00+00:00",
                      "bot_value": 100.0, "held": ["ZZZ"]}])
    assert 'class="held"' in legacy and "ZZZ" in legacy
    # the market clause: the whole point is that a holiday reads as a
    # holiday and not as a dead bot, so check each shape day_context emits
    assert market_line(None) == "", "no status must leave the stamp untouched"
    holiday = market_line({"market_open": False, "status": "market holiday",
                           "next_open": "2026-09-08T09:30:00-04:00"})
    assert "market closed for a holiday" in holiday, holiday
    assert "next open Tue 08 Sep 20:30 WIB" in holiday, holiday
    weekend = market_line({"market_open": False, "status": "weekend",
                           "next_open": "2026-09-08T09:30:00-04:00"})
    assert "market closed for the weekend" in weekend, weekend
    after = market_line({"market_open": False, "status": "outside regular hours",
                         "next_open": "2026-09-08T09:30:00-04:00"})
    assert "market closed, next open" in after, after
    live = market_line({"market_open": True, "status": "open, 84 min to the close"})
    assert "market open, 84 min to the close" in live and "next open" not in live
    # and it has to land in the header, next to the stamp it explains
    stamped = render([], market={"market_open": False, "status": "weekend",
                                 "next_open": "2026-09-08T09:30:00-04:00"})
    assert "market closed for the weekend" in stamped
    # a failed call must be visible, and its text escaped not executed
    broke = render([{"type": "llm_error", "timestamp": "2026-01-02T00:00:00+00:00",
                     "model": "x/y", "error": "boom <script>alert(1)</script>"}])
    assert "Last run failed" in broke and "&lt;script&gt;" in broke
    assert "<script>alert" not in broke
    # holds must survive even when nothing was traded
    holds_only = render([{"type": "decisions", "timestamp": "2026-01-01T00:00:00+00:00",
                          "candidates": ["AAA"], "decisions": [
                              {"ticker": "AAA", "action": "hold",
                               "reasoning": "flat and boring"}]}])
    assert "AAA" in holds_only and "flat and boring" in holds_only
    # no em/en dashes anywhere, including ones a model wrote
    dashy = render([{"type": "decisions", "timestamp": "2026-01-01T00:00:00+00:00",
                     "candidates": ["BBB"], "decisions": [
                         {"ticker": "BBB", "action": "hold",
                          "reasoning": "choppy — and thin – so passing"}]}])
    for rendered in (empty, page, holds_only, dashy, stamped):
        assert "—" not in rendered and "–" not in rendered, "dash leaked" 
    # a truncated final line must not kill the parse
    tmp = HERE / "_tmp_log.jsonl"
    tmp.write_text('{"type":"snapshot","bot_value":1}\n{"type":"snap', encoding="utf-8")
    assert len(read_events(tmp)) == 1
    tmp.unlink()
    print("dashboard selfcheck OK")


if __name__ == "__main__":
    demo = "--demo" in sys.argv
    if "--selfcheck" in sys.argv:
        selfcheck()
        sys.exit(0)
    events = demo_events() if demo else read_events(LOG)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Looked up here, not inside render(), so render() stays offline and
    # pure: the selfcheck calls it a dozen times and must not hit the wire.
    market = None if demo else market_now()
    OUT.write_text(render(events, demo=demo, market=market), encoding="utf-8")
    print(f"wrote {OUT}" + ("  (DEMO DATA)" if demo else
                            f"  ({len(events)} events)"))

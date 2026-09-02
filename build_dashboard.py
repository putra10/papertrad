"""
Build docs/index.html from trade_log.jsonl — a single self-contained page.

Run:  python build_dashboard.py
      python build_dashboard.py --demo   (synthetic data, to preview the
                                          layout before real runs exist;
                                          also doubles as the self-check)

No dependencies, no CDN, no JavaScript — GitHub Pages serves the file as-is.
"""

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
LOG = HERE / "trade_log.jsonl"
OUT = HERE / "docs" / "index.html"
WIB = timezone(timedelta(hours=7))       # author is in Indonesia; show both
SERIES_COLORS = {"bot": "#2f81f7", "SPY": "#8b949e", "QQQ": "#d29922"}

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


def clean(text):
    """No em or en dashes on the page, including model-written reasoning."""
    return (text or "").replace("—", "-").replace("–", "-")


def svg_chart(series, height=300, pad=34, foot=24, vbw=1000):
    """Multi-line equity chart as inline SVG. Absolute dollars, every
    series starts from the same equity, so they are directly comparable.

    Gridlines and lines live in a nested viewport that is allowed to
    stretch; the labels stay in the outer one, where they keep their size
    at any screen width. That is what makes one chart serve a 360px phone
    and a 1000px desktop: stretch a whole SVG instead and it squashes its
    own text, scale it uniformly and it collapses to about 100px tall.
    """
    points = [v for pts in series.values() for _, v in pts]
    if len(points) < 2:
        return ('<p class="empty">Not enough data for a chart yet. Needs at '
                'least two runs.</p>')

    lo, hi = min(points), max(points)
    span = (hi - lo) or 1
    lo, hi = lo - span * 0.08, hi + span * 0.08
    span = hi - lo
    top, bottom = pad, height - pad - foot
    fracs = (0, 0.25, 0.5, 0.75, 1)

    def row(frac):
        return top + (bottom - top) * frac

    def xy(i, v, count):
        x = vbw * (i / max(count - 1, 1))
        y = bottom - (bottom - top) * ((v - lo) / span)
        return f"{x:.1f},{y:.1f}"

    parts = [f'<svg class="chart" width="100%" height="{height}" role="img" '
             f'aria-label="Equity over time">',
             f'<svg width="100%" height="{height}" viewBox="0 0 {vbw} {height}" '
             f'preserveAspectRatio="none">']
    for frac in fracs:
        parts.append(f'<line class="grid" x1="0" y1="{row(frac):.1f}" '
                     f'x2="{vbw}" y2="{row(frac):.1f}"/>')
    for name, pts in series.items():
        path = " ".join(xy(i, v, len(pts)) for i, (_, v) in enumerate(pts))
        colour = SERIES_COLORS.get(name, "#a371f7")
        # non-scaling-stroke, or the horizontal stretch thins the lines
        parts.append(f'<polyline points="{path}" fill="none" stroke="{colour}" '
                     f'stroke-width="2" stroke-linejoin="round" '
                     f'vector-effect="non-scaling-stroke"/>')
    parts.append("</svg>")

    # labels sit just above their gridline, so no left gutter is needed and
    # the lines get the full width on a narrow screen
    for frac in fracs:
        parts.append(f'<text class="tick" x="2" y="{row(frac) - 5:.1f}">'
                     f'${hi - span * frac:,.0f}</text>')
    stamps = next(iter(series.values()))
    parts.append(f'<text class="tick" x="2" y="{height - 6}">'
                 f'{when(stamps[0][0])}</text>')
    parts.append(f'<text class="tick" x="100%" dx="-2" y="{height - 6}" '
                 f'text-anchor="end">{when(stamps[-1][0])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render(events, demo=False):
    snapshots = [e for e in events if e.get("type") == "snapshot"]
    orders = [e for e in events if e.get("type") == "order"]
    failed = [e for e in events if e.get("type") == "order_failed"]
    series = build_series(snapshots)
    latest = snapshots[-1] if snapshots else {}
    bot = float(latest.get("bot_value") or 0)
    bases = latest.get("baselines") or {}
    start = float(series["bot"][0][1]) if series.get("bot") else 0

    # headline tiles
    tiles = [("Bot equity", fmt_money(bot),
              f"{((bot / start - 1) * 100):+.2f}% since start" if start else "n/a",
              bot - start)]
    cash = latest.get("cash")
    if cash is not None:
        deployed = (1 - float(cash) / bot) * 100 if bot else 0
        tiles.append(("Cash in hand", fmt_money(float(cash)),
                      f"{deployed:.0f}% of equity deployed", 0))
    for name, value in bases.items():
        tiles.append((f"vs {name}", fmt_delta(bot - float(value)),
                      f"{name} at {fmt_money(float(value))}", bot - float(value)))
    tiles.append(("Runs / orders", f"{len(snapshots)} / {len(orders)}",
                  f"{len(failed)} rejected" if failed else "none rejected", 0))

    tile_html = "".join(
        f'<div class="tile"><h2>{label}</h2>'
        f'<p class="value {"up" if delta > 0 else "down" if delta < 0 else ""}">{value}</p>'
        f'<p class="sub">{sub}</p></div>'
        for label, value, sub, delta in tiles)

    def cls(v):
        return "up" if v > 0 else "down" if v < 0 else ""

    positions = latest.get("positions") or {}
    if positions:
        prows = "".join(
            f'<tr><td class="sym">{sym}</td>'
            f'<td class="num">{float(p.get("shares") or 0):,.4f}</td>'
            f'<td class="num opt">{fmt_money(float(p.get("avg_entry") or 0))}</td>'
            f'<td class="num opt">{fmt_money(float(p.get("last") or 0))}</td>'
            f'<td class="num">{fmt_money(float(p.get("value") or 0))}</td>'
            f'<td class="num {cls(float(p.get("pl") or 0))}">'
            f'<span class="opt">{fmt_delta(float(p.get("pl") or 0))} </span>'
            f'({float(p.get("pl_pct") or 0):+.2f}%)</td></tr>'
            for sym, p in sorted(positions.items()))
        cash_row = ""
        if cash is not None:
            pct = float(cash) / bot * 100 if bot else 0
            cash_row = (f'<tr><td class="sym">CASH</td>'
                        f'<td colspan="3" class="empty">uninvested</td>'
                        f'<td class="num">{fmt_money(float(cash))}</td>'
                        f'<td class="num empty">{pct:.1f}%'
                        f'<span class="opt"> of equity</span></td></tr>')
        held_html = (
            '<div class="scroll"><table><thead><tr><th>Symbol</th>'
            '<th class="num">Shares</th><th class="num opt">Avg entry</th>'
            '<th class="num opt">Last</th><th class="num">Value</th>'
            '<th class="num">Unrealized</th></tr></thead>'
            f'<tbody>{prows}{cash_row}</tbody></table></div>')
    elif latest.get("held"):
        # snapshots logged before position detail existed: tickers only
        held_html = ('<ul class="held">'
                     + "".join(f"<li>{h}</li>" for h in latest["held"]) + "</ul>")
    else:
        held_html = '<p class="empty">no open positions</p>'

    rows = []
    for o in reversed(orders[-30:]):
        spec = o.get("order") or {}
        side = (spec.get("side") or "").upper()
        size = spec.get("notional")
        size = f"${float(size):,.2f}" if size is not None else f"{spec.get('qty')} sh"
        price = o.get("filled_avg_price")
        rows.append(
            f'<tr><td class="nowrap">{when(o.get("timestamp"))}</td>'
            f'<td><span class="side {side.lower()}">{side}</span></td>'
            f'<td class="sym">{spec.get("symbol", "?")}</td>'
            f'<td class="nowrap">{size}</td>'
            f'<td class="nowrap">{fmt_money(float(price)) if price else o.get("status", "-")}</td>'
            f'<td class="why">{clean(o.get("reasoning")).strip() or "-"}</td></tr>')
    rows_html = "".join(rows) or ('<tr><td colspan="6" class="empty">'
                                  'no orders yet</td></tr>')

    def esc(t):
        return (clean(t).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    # surface the last LLM failure, if it is more recent than the last success
    errors = [e for e in events if e.get("type") == "llm_error"]
    decision_events = [e for e in events if e.get("type") == "decisions"]
    err_html = ""
    if errors and (not decision_events
                   or errors[-1].get("timestamp", "") > decision_events[-1].get("timestamp", "")):
        err = errors[-1]
        err_html = (f'<div class="card err"><h3>Last run failed</h3>'
                    f'<p class="sub">{when(err.get("timestamp"))} · '
                    f'{esc(err.get("model", "?"))}</p>'
                    f'<pre>{esc(err.get("error", ""))}</pre></div>')

    last_call = decision_events[-1] if decision_events else None
    if last_call:
        calls = last_call.get("decisions") or []
        drows = "".join(
            f'<tr><td class="sym">{d.get("ticker", "?")}</td>'
            f'<td><span class="side {(d.get("action") or "hold")}">'
            f'{(d.get("action") or "?").upper()}</span></td>'
            f'<td class="why">{clean(d.get("reasoning")).strip() or "-"}</td></tr>'
            for d in calls)
        acted = sum(1 for d in calls if d.get("action") in ("buy", "sell"))
        usage = last_call.get("usage") or {}
        toks = (f' · {usage.get("prompt_tokens", "?")} in / '
                f'{usage.get("completion_tokens", "?")} out' if usage else "")
        raw = last_call.get("raw") or ""
        thinking = last_call.get("thinking") or ""
        blocks = ""
        if thinking:
            blocks += (f'<details><summary>Model thinking '
                       f'({len(thinking)} chars)</summary>'
                       f'<pre>{esc(thinking)}</pre></details>')
        if raw:
            blocks += (f'<details><summary>Raw model response</summary>'
                       f'<pre>{esc(raw)}</pre></details>')
        headlines = last_call.get("news") or []
        if headlines:
            items = "".join(
                f'<li><b>{esc(", ".join(n.get("symbols") or []) or "market")}</b> '
                f'<span class="sub">{esc(n.get("when", ""))} · '
                f'{esc(n.get("source", ""))}</span><br>'
                f'{esc(n.get("headline", ""))}</li>' for n in headlines)
            blocks += (f'<details><summary>Headlines it read '
                       f'({len(headlines)})</summary>'
                       f'<ul class="news">{items}</ul></details>')
        day = last_call.get("day") or {}
        day_line = (f'{esc(day.get("weekday", ""))} {esc(day.get("date", ""))}, '
                    f'{esc(day.get("status", ""))}<br>' if day else "")
        think_html = (
            f'<p class="sub">{day_line}'
            f'{len(last_call.get("candidates") or [])} candidates '
            f'screened · {acted} acted on · {len(headlines)} '
            f'headline{"" if len(headlines) == 1 else "s"} '
            f'· {when(last_call.get("timestamp"))}'
            f'<br>{esc(last_call.get("model", "?"))}{toks}</p>'
            f'<div class="scroll"><table><thead><tr><th>Symbol</th><th>Call</th>'
            f'<th>Reasoning</th></tr></thead><tbody>{drows or ""}</tbody>'
            f'</table></div>{blocks}')
    else:
        think_html = ('<p class="empty">no decisions logged yet. This fills in '
                      'on the next run</p>')

    banner = ('<div class="banner">DEMO DATA: this is synthetic, generated by '
              '<code>--demo</code> to preview the layout. Not real results.</div>'
              if demo else "")
    legend = "".join(
        f'<span class="key"><i style="background:{SERIES_COLORS.get(k, "#a371f7")}"></i>'
        f'{"Bot" if k == "bot" else k}</span>' for k in series)

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLM Paper Trading</title>
<style>
  :root {{
    --bg:#ffffff; --panel:#f6f8fa; --line:#d8dee4;
    --fg:#1f2328; --muted:#636c76; --up:#1a7f37; --down:#cf222e;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg:#0d1117; --panel:#161b22; --line:#30363d;
      --fg:#e6edf3; --muted:#8b949e; --up:#3fb950; --down:#f85149;
    }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:24px; background:var(--bg); color:var(--fg);
    font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  .wrap {{ max-width:1040px; margin:0 auto; }}
  h1 {{ font-size:20px; margin:0 0 2px; }}
  .stamp {{ color:var(--muted); font-size:13px; margin:0 0 20px; }}
  .banner {{ background:#bf8700; color:#fff; padding:10px 14px;
    border-radius:8px; margin-bottom:18px; font-weight:600; font-size:14px; }}
  .banner code {{ background:rgba(0,0,0,.2); padding:1px 5px; border-radius:4px; }}
  .tiles {{ display:grid; gap:12px; margin-bottom:22px;
    grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); }}
  .tile {{ background:var(--panel); border:1px solid var(--line);
    border-radius:10px; padding:14px 16px; }}
  .tile h2 {{ font-size:12px; text-transform:uppercase; letter-spacing:.05em;
    color:var(--muted); margin:0 0 6px; font-weight:600; }}
  .value {{ font-size:24px; font-weight:650; margin:0;
    font-variant-numeric:tabular-nums; }}
  .value.up {{ color:var(--up); }} .value.down {{ color:var(--down); }}
  .sub {{ color:var(--muted); font-size:12px; margin:4px 0 0; }}
  .card {{ background:var(--panel); border:1px solid var(--line);
    border-radius:10px; padding:16px; margin-bottom:22px; }}
  .card h3 {{ font-size:13px; text-transform:uppercase; letter-spacing:.05em;
    color:var(--muted); margin:0 0 12px; font-weight:600; }}
  svg {{ display:block; }}
  .grid {{ stroke:var(--line); stroke-width:1; }}
  .tick {{ fill:var(--muted); font-size:11px; }}
  .key {{ display:inline-flex; align-items:center; gap:6px; margin-right:14px;
    font-size:13px; color:var(--muted); }}
  .key i {{ width:11px; height:3px; border-radius:2px; display:inline-block; }}
  ul.held {{ list-style:none; margin:0; padding:0; display:flex;
    flex-wrap:wrap; gap:8px; }}
  ul.held li {{ background:var(--bg); border:1px solid var(--line);
    border-radius:6px; padding:4px 10px; font-weight:600; font-size:14px; }}
  .scroll {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th {{ text-align:left; color:var(--muted); font-size:11px;
    text-transform:uppercase; letter-spacing:.05em; padding:0 10px 8px 0; }}
  td {{ padding:8px 10px 8px 0; border-top:1px solid var(--line);
    vertical-align:top; }}
  .nowrap {{ white-space:nowrap; }}
  .num {{ text-align:right; white-space:nowrap;
    font-variant-numeric:tabular-nums; padding-right:16px; }}
  td.up {{ color:var(--up); }} td.down {{ color:var(--down); }}
  ul.news {{ margin:6px 0 0; padding-left:18px; }}
  ul.news li {{ margin-bottom:10px; }}
  .sym {{ font-weight:650; }}
  .side {{ font-weight:700; font-size:11px; padding:2px 6px; border-radius:4px; }}
  .side.buy {{ background:rgba(63,185,80,.15); color:var(--up); }}
  .side.sell {{ background:rgba(248,81,73,.15); color:var(--down); }}
  .side.hold {{ background:rgba(139,148,158,.18); color:var(--muted); }}
  .why {{ color:var(--muted); min-width:280px; }}
  .empty {{ color:var(--muted); font-style:italic; }}
  .card.err {{ border-color:var(--down); }}
  .card.err h3 {{ color:var(--down); }}
  details {{ margin-top:12px; }}
  summary {{ cursor:pointer; color:var(--muted); font-size:13px;
    padding:6px 0; user-select:none; }}
  summary:hover {{ color:var(--fg); }}
  pre {{ background:var(--bg); border:1px solid var(--line); border-radius:6px;
    padding:12px; overflow-x:auto; font-size:12px; line-height:1.45;
    white-space:pre-wrap; word-break:break-word; margin:6px 0 0; }}
  @media (max-width:640px) {{
    body {{ padding:14px; }}
    h1 {{ font-size:18px; }}
    .stamp {{ margin-bottom:16px; }}
    /* 140px lets two tiles sit side by side on a phone instead of five
       full-width slabs the reader has to scroll past to reach the chart */
    .tiles {{ gap:10px; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); }}
    .value {{ font-size:20px; }}
    .card {{ padding:12px; margin-bottom:16px; }}
    table {{ font-size:12px; }}
    td, th {{ padding-right:8px; }}
    .num {{ padding-right:8px; }}
    .opt {{ display:none; }}
    /* let reasoning wrap into a readable column instead of forcing the
       whole table sideways; the .scroll wrapper still catches the rest */
    .why {{ min-width:150px; }}
    pre {{ font-size:11px; }}
  }}
</style>
<div class="wrap">
  {banner}
  <h1>LLM Paper Trading</h1>
  <p class="stamp">Updated {when_full(latest.get('timestamp'))}</p>

  <div class="tiles">{tile_html}</div>

  {err_html}

  <div class="card">
    <h3>Equity vs benchmarks</h3>
    {svg_chart(series)}
    <div>{legend}</div>
  </div>

  <div class="card">
    <h3>Portfolio</h3>
    {held_html}
  </div>

  <div class="card">
    <h3>Last run: every candidate it looked at</h3>
    {think_html}
  </div>

  <div class="card">
    <h3>Orders placed</h3>
    <div class="scroll"><table>
      <thead><tr><th>When</th><th>Side</th><th>Symbol</th><th>Size</th>
      <th>Fill</th><th>Reasoning</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table></div>
  </div>

</div>
"""

# ----------------------------- ENTRY --------------------------------------

def demo_events(runs=40):
    """Synthetic log so the layout can be checked before real data exists."""
    random.seed(7)
    now = datetime.now(timezone.utc) - timedelta(minutes=15 * runs)
    bot = spy = qqq = 100_000.0
    names = ["NVDA", "TSLA", "AMD", "PLTR", "SMCI"]
    events = []
    for i in range(runs):
        stamp = (now + timedelta(minutes=15 * i)).isoformat()
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
    assert "no orders yet" in empty and "Not enough data" in empty
    page = render(demo_events(), demo=True)
    # one chart, drawn once: lines in a stretching nested viewport, labels
    # outside it so they keep their size at any width
    assert page.count("<polyline") == 3, "expected one line per series"
    assert page.count("<svg") == 2 and 'preserveAspectRatio="none"' in page
    assert 'x="100%"' in page and "@media (max-width:640px)" in page
    for must in ("DEMO DATA", "vs SPY", "vs QQQ", "<polyline", "NVDA",
                 "candidates", "HOLD", "every candidate it looked at",
                 "Raw model response", "Model thinking", "1840 in / 412 out",
                 # portfolio detail, cash, day awareness and news
                 "Avg entry", "Unrealized", "Cash in hand", "CASH",
                 "of equity deployed", "Headlines it read",
                 "guides above consensus", "min to the close"):
        assert must in page, must
    # old snapshots carry tickers only; they must still render as chips
    legacy = render([{"type": "snapshot", "timestamp": "2026-01-01T00:00:00+00:00",
                      "bot_value": 100.0, "held": ["ZZZ"]}])
    assert 'class="held"' in legacy and "ZZZ" in legacy
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
    for rendered in (empty, page, holds_only, dashy):
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
    OUT.write_text(render(events, demo=demo), encoding="utf-8")
    print(f"wrote {OUT}" + ("  (DEMO DATA)" if demo else
                            f"  ({len(events)} events)"))

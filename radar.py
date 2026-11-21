#!/usr/bin/env python3
"""
Watchlist and market-news add-on. This is the former "Indian Stock Radar" project, folded into the NSE screener.

* Watchlist: your own symbols (watchlist.json, or a published Google Sheet CSV) analysed with the SAME indicators and
  level maths as the price-bracket report (screener.compute_features / compute_levels), so a stock shows the same buy
  zone, stop-loss and targets wherever it appears on the site.
* News: latest Indian market headlines from public RSS feeds (parsed with the standard library, no extra dependency).

Everything is derived from the previous session's close. Nothing here sees live prices.
Educational / research use only - not investment advice.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd

log = logging.getLogger("screener.radar")

WATCHLIST_FILE = Path("watchlist.json")
CHART_BARS = 60                      # closes shipped to the browser for each watchlist chart
MODES = ("CNC", "INTRADAY")
RSS_FEEDS = [
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Mint", "https://www.livemint.com/rss/markets"),
]
NEWS_MAX_AGE_H = 72.0
NEWS_MAX_ITEMS = 14
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_TONE_LABEL = {"pos": "Bullish", "neg": "Bearish", "mixed": "Mixed", "neutral": "Neutral"}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _num(x, nd: int = 2):
    """Round to nd places; None for missing / NaN / inf so the output is always valid JSON."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def normalise_symbol(raw) -> str:
    s = str(raw or "").strip().upper()
    s = re.sub(r"^(NSE|BSE)[:\s]+", "", s)
    s = re.sub(r"\.(NS|BO)$", "", s)
    return "" if s in ("NAN", "NONE") else s


def normalise_mode(raw) -> str:
    m = str(raw or "CNC").strip().upper()
    return "INTRADAY" if m in ("INTRADAY", "MIS", "INTRA") else "CNC"


def tick(x: float) -> float:
    return round(round(x / 0.05) * 0.05, 2)


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #
def _items_from_rows(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        sym = normalise_symbol(r.get("symbol"))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        buy = _num(r.get("custom_buy"))
        out.append({
            "symbol": sym,
            "name": str(r.get("name") or sym).strip() or sym,
            "sector": str(r.get("sector") or "").strip(),
            "mode": normalise_mode(r.get("mode")),
            "custom_buy": buy if (buy and buy > 0) else None,
        })
    return out


def load_watchlist(path: Path = WATCHLIST_FILE, sheet_url: str | None = None,
                   read_csv=pd.read_csv) -> tuple[list[dict], str]:
    """(items, source). A published Google Sheet CSV (GOOGLE_SHEET_CSV_URL) wins; watchlist.json is the fallback.

    Sheet columns: Symbol, Name, Sector, Mode (CNC or INTRADAY), My_Buy_Price (all but Symbol optional)."""
    sheet_url = (os.environ.get("GOOGLE_SHEET_CSV_URL", "") if sheet_url is None else sheet_url).strip()
    if sheet_url:
        try:
            df = read_csv(sheet_url)
            rows = [{"symbol": r.get("Symbol"), "name": r.get("Name"), "sector": r.get("Sector"),
                     "mode": r.get("Mode"), "custom_buy": r.get("My_Buy_Price")} for _, r in df.iterrows()]
            items = _items_from_rows(rows)
            if items:
                return items, "google-sheet"
            log.warning("Google Sheet had no usable rows - using %s", path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Google Sheet unavailable (%s) - using %s", exc, path)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("No readable watchlist (%s)", exc)
        return [], "none"
    rows = [{"symbol": r.get("symbol"), "name": r.get("name"), "sector": r.get("sector"),
             "mode": r.get("mode"), "custom_buy": r.get("custom_buy")} for r in raw if isinstance(r, dict)]
    return _items_from_rows(rows), "watchlist.json"


def classify_setup(f: dict) -> str:
    c, e20, e50, e200 = f["close"], f["ema20"], f["ema50"], f["ema200"]
    if f["rsi"] < 42:
        return "Oversold dip: accumulation zone"
    if c > e20 and f["vol_ratio"] >= 1.4:
        return "Volume breakout above 20-EMA"
    if f["hi52"] and c >= 0.97 * f["hi52"]:
        return "Near 52-week high"
    if c > e20 > e50 and (math.isnan(e200) or c > e200):
        return "Uptrend: above all EMAs"
    if c > e20 and e20 > e50:
        return "Bullish trend continuation"
    return "Consolidation: watch the 20-EMA"


def _series(values, nd: int = 2) -> list:
    return [_num(v, nd) for v in values]


def analyze_stock(meta: dict, f: dict, lv: dict, df: pd.DataFrame, metrics: dict | None,
                  industry: str = "") -> dict:
    """One watchlist row: indicators, levels, optional fundamentals and chart series."""
    close = float(f["close"])
    buy_low, buy_high = lv["buy_low"], lv["buy_high"]
    custom = meta.get("custom_buy")
    if custom:                                  # your own buy price overrides the computed zone
        buy_low, buy_high = tick(custom * 0.98), tick(custom)
    risk = max(lv["entry"] - lv["sl"], 1e-9)
    entry = (buy_low + buy_high) / 2 if custom else lv["entry"]
    cl = df["Close"].dropna()
    e20s = cl.ewm(span=20, adjust=False).mean()
    e50s = cl.ewm(span=50, adjust=False).mean()
    n = min(CHART_BARS, len(cl))
    m = metrics or {}
    eps = m.get("eps")
    pe = (close / eps) if (eps and eps > 0) else None
    in_zone = close <= buy_high * 1.01          # same rule as the CNC shortlist in desk.py
    return {
        "symbol": meta["symbol"], "name": meta["name"],
        "sector": meta.get("sector") or m.get("sector") or industry or "",
        "mode": meta["mode"],
        "cmp": _num(close), "chg_pct": _num(f["chg_pct"]), "date": str(f["date"]),
        "hi52": _num(f["hi52"]), "lo52": _num(df["Low"].iloc[-252:].min()),
        "rsi": _num(f["rsi"], 1),
        "ema20": _num(f["ema20"]), "ema50": _num(f["ema50"]), "ema200": _num(f["ema200"]),
        "vol_ratio": _num(f["vol_ratio"]),
        "turnover_cr": _num(f["turnover_cr"], 1),
        "setup": classify_setup(f),
        "buy_low": _num(buy_low), "buy_high": _num(buy_high), "custom_buy": custom,
        "in_zone": bool(in_zone),
        "breakout": _num(tick(f["hi20"] * 1.005)),
        "sl": _num(lv["sl"]), "t1": _num(lv["st"]), "t2": _num(lv["mt"]),
        "sl_pct": _num((lv["sl"] / close - 1) * 100, 1),
        "t1_pct": _num((lv["st"] / close - 1) * 100, 1),
        "t2_pct": _num((lv["mt"] / close - 1) * 100, 1),
        "rr": _num((lv["st"] - entry) / risk, 2),
        "fund": {
            "pe": _num(pe, 1), "roe": _num(m.get("roe"), 1), "roce": _num(m.get("roce_latest"), 1),
            "roce_avg": _num(m.get("roce_avg"), 1), "de": _num(m.get("de"), 2),
            "sales3": _num(m.get("sales_cagr3"), 1), "profit3": _num(m.get("profit_cagr3"), 1),
            "op_margin": _num(m.get("op_margin"), 1), "mcap_cr": _num(m.get("mcap_cr"), 0),
            "promoter": _num(m.get("promoter"), 1), "qtr_growth": _num(m.get("qtr_growth"), 1),
        } if m else None,
        "chart": {
            "dates": [d.strftime("%d %b") for d in cl.index[-n:]],
            "close": _series(cl.iloc[-n:]), "ema20": _series(e20s.iloc[-n:]), "ema50": _series(e50s.iloc[-n:]),
        },
    }


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #
def parse_rss(xml_text: str, source: str, now_utc: datetime) -> list[dict]:
    """RSS 2.0 <item> list -> headline dicts (newest first is decided by the caller)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        title = re.sub(r"\s+", " ", (it.findtext("title") or "")).strip()
        if not title:
            continue
        ts = None
        raw = (it.findtext("pubDate") or "").strip()
        if raw:
            try:
                ts = parsedate_to_datetime(raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                ts = None
        out.append({"title": title, "url": (it.findtext("link") or "").strip(), "source": source,
                    "ts": ts, "age_h": ((now_utc - ts).total_seconds() / 3600.0) if ts else None})
    return out


def _tone(title: str) -> str:
    try:
        import desk                                  # same folder; reuse the headline classifier
        return desk.headline_tone(title)
    except Exception:  # noqa: BLE001
        return "neutral"


def tag_headline(title: str, watchlist: list[dict]) -> str:
    """Watchlist symbol(s) mentioned in a headline, else the broad-market tag."""
    low = title.lower()
    hits = []
    for w in watchlist:
        short = re.sub(r"\b(ltd|limited|industries|infrastructure|dev|corporation)\b\.?", "", w["name"], flags=re.I).strip()
        keys = {w["symbol"].lower()} | ({short.lower()} if len(short) >= 5 else set())
        if any(re.search(rf"\b{re.escape(k)}\b", low) for k in keys):
            hits.append(w["symbol"])
    if hits:
        return ", ".join(hits[:2])
    return "NIFTY" if re.search(r"\b(nifty|sensex)\b", low) else "MARKET"


def fetch_news(watchlist: list[dict], now: datetime, get=None, feeds=RSS_FEEDS) -> list[dict]:
    """Latest headlines (last 72 h) from the public RSS feeds; a feed that fails is simply skipped."""
    if get is None:
        import requests

        def get(url):
            r = requests.get(url, headers=_UA, timeout=15)
            r.raise_for_status()
            return r.text
    now_utc = now.astimezone(timezone.utc)
    items: list[dict] = []
    for source, url in feeds:
        try:
            items += parse_rss(get(url), source, now_utc)
        except Exception as exc:  # noqa: BLE001
            log.warning("News feed %s skipped: %s", source, exc)
    seen, out = set(), []
    for it in sorted(items, key=lambda i: i["ts"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        key = re.sub(r"\W+", "", it["title"].lower())[:80]
        if key in seen or (it["age_h"] is not None and it["age_h"] > NEWS_MAX_AGE_H):
            continue
        seen.add(key)
        ist = it["ts"].astimezone(now.tzinfo) if it["ts"] else None
        out.append({
            "title": it["title"], "url": it["url"], "source": it["source"],
            "time": ist.strftime("%d %b, %H:%M IST") if ist else "",
            "tone": _TONE_LABEL[_tone(it["title"])],
            "tag": tag_headline(it["title"], watchlist),
        })
        if len(out) >= NEWS_MAX_ITEMS:
            break
    return out


# --------------------------------------------------------------------------- #
# Entry point used by screener.main()
# --------------------------------------------------------------------------- #
def build_radar(hist: dict[str, pd.DataFrame], industries: dict[str, str], *, now: datetime,
                compute_features, compute_levels, fetch_missing=None, fetch_metrics=None,
                path: Path = WATCHLIST_FILE, sheet_url: str | None = None, get=None, with_news: bool = True) -> dict:
    """Watchlist analysis + headlines.

    hist             price histories already downloaded for the screen (watchlist names are looked up here first)
    compute_features df -> feature dict | None        (screener.compute_features bound to its Config)
    compute_levels   feature dict -> levels dict      (screener.compute_levels)
    fetch_missing    symbols -> {symbol: df}          (downloads watchlist names that were not in the screen)
    fetch_metrics    symbols -> {symbol: metrics}     (quality.fetch_metrics bound to its config); optional
    """
    items, source = load_watchlist(path, sheet_url)
    frames = {it["symbol"]: hist[it["symbol"]] for it in items if it["symbol"] in hist}
    lacking = [it["symbol"] for it in items if it["symbol"] not in frames]
    if lacking and fetch_missing:
        try:
            frames.update(fetch_missing(lacking))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not download %s: %s", ", ".join(lacking), exc)
    metrics: dict[str, dict] = {}
    if fetch_metrics and items:
        try:
            metrics = fetch_metrics([it["symbol"] for it in items]) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("Watchlist fundamentals unavailable: %s", exc)
    rows, missing = [], []
    for it in items:
        df = frames.get(it["symbol"])
        f = compute_features(df) if df is not None else None
        if f is None:
            missing.append(it["symbol"])
            continue
        try:
            rows.append(analyze_stock(it, f, compute_levels(f), df, metrics.get(it["symbol"]),
                                      industries.get(it["symbol"], "")))
        except Exception as exc:  # noqa: BLE001  one bad symbol must not drop the rest
            log.warning("Watchlist %s skipped: %s", it["symbol"], exc)
            missing.append(it["symbol"])
    news = fetch_news(items, now, get) if with_news else []
    log.info("Radar: %d watchlist stocks analysed (%d without data), %d headlines", len(rows), len(missing), len(news))
    return {"source": source, "watchlist": rows, "missing": missing, "news": news}


# --------------------------------------------------------------------------- #
# Classic report / email sections (same CSS classes as screener.CSS)
# --------------------------------------------------------------------------- #
def _inr(x) -> str:
    return "-" if x is None else f"₹{x:,.2f}"


def _pc(x) -> str:
    return "-" if x is None else f"{x:+.1f}%"


def render_html_section(rd: dict) -> str:
    from html import escape
    rows = rd.get("watchlist") or []
    out = ['<h2 id="watchlist">My watchlist <span class="sm">· your own symbols, analysed with the same rules as the '
           "lists above · CMP = last close</span></h2>"]
    if not rows:
        out.append('<div class="banner">No watchlist stocks could be analysed today'
                   + (f" (no data for {escape(', '.join(rd['missing']))})" if rd.get("missing") else "") + ".</div>")
    else:
        body = []
        for r in rows:
            cls = "up" if (r["chg_pct"] or 0) >= 0 else "dn"
            zone = '<span class="b bf">In buy zone</span>' if r["in_zone"] else '<span class="b bp">Wait for pullback</span>'
            body.append(
                f'<tr><td class="nm">{escape(r["name"])}{zone}<div class="tk">NSE: {escape(r["symbol"])} · {r["mode"]}</div></td>'
                f'<td class="n">{_inr(r["cmp"])}<div class="sm {cls}">{_pc(r["chg_pct"])}</div></td>'
                f'<td class="n">{_inr(r["buy_low"])} – {_inr(r["buy_high"])}</td>'
                f'<td class="n dn">{_inr(r["sl"])}<div class="sm">{_pc(r["sl_pct"])}</div></td>'
                f'<td class="n up">{_inr(r["t1"])}<div class="sm">{_pc(r["t1_pct"])}</div></td>'
                f'<td class="n up">{_inr(r["t2"])}<div class="sm">{_pc(r["t2_pct"])}</div></td>'
                f'<td class="rat">{escape(r["setup"])}<div class="sm">RSI {r["rsi"]:.0f} · volume {r["vol_ratio"]:.1f}×</div></td></tr>')
        out.append('<div class="scroll"><table><tr><th>Stock</th><th class="n">CMP</th><th class="n">Buy zone</th>'
                   '<th class="n">Stop-loss</th><th class="n">Target 1</th><th class="n">Target 2</th><th>Setup</th></tr>'
                   + "".join(body) + "</table></div>")
        if rd.get("missing"):
            out.append(f'<div class="meth">No usable price history for: {escape(", ".join(rd["missing"]))}.</div>')
    news = rd.get("news") or []
    if news:
        items = "".join(
            f'<li><a href="{escape(n["url"], quote=True)}">{escape(n["title"])}</a> '
            f'<span class="sm">{escape(n["source"])} · {escape(n["time"])} · {escape(n["tone"])}</span></li>'
            for n in news[:6] if n.get("url"))
        if items:
            out.append(f'<h2 id="news">Market headlines</h2><ul style="margin:0;padding-left:18px">{items}</ul>')
    return "".join(out)


def render_text_section(rd: dict) -> str:
    out = ["== My watchlist =="]
    for r in rd.get("watchlist") or []:
        out.append(f"{r['symbol']:<12} CMP {r['cmp']:.2f} ({r['chg_pct']:+.1f}%) | "
                   f"{'in buy zone' if r['in_zone'] else 'wait for pullback'} | buy {r['buy_low']:.2f}-{r['buy_high']:.2f}"
                   f" | SL {r['sl']:.2f} | T1 {r['t1']:.2f} | T2 {r['t2']:.2f}")
    if not rd.get("watchlist"):
        out.append("No watchlist stocks could be analysed today.")
    out.append("")
    return "\n".join(out)

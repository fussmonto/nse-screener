#!/usr/bin/env python3
"""
Trading-desk add-ons for the NSE screener: intraday (MIS) watchlist, CNC delivery shortlist, market pulse,
news / results-date flags and risk-based position sizing.

Everything is derived from data that already exists before the 9:15 AM open (previous session's OHLCV,
overnight global closes, Asian markets that have already opened, recent headlines). Nothing here sees live
prices, so intraday rows are *trigger levels to watch*, not signals that have fired.

Educational / research use only - not investment advice.
"""
from __future__ import annotations

import logging
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from urllib.parse import quote

import pandas as pd

log = logging.getLogger("screener.desk")
TICK = 0.05


def tick(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if str(raw).strip() else default
    except ValueError:
        return default


@dataclass
class DeskConfig:
    # --- intraday (MIS) universe and output
    min_turnover_cr: float = 25.0      # 20-day average traded value; intraday needs depth
    min_price: float = 20.0
    max_price: float = 5000.0
    atr_pct_min: float = 1.5           # need room to move ...
    atr_pct_max: float = 6.0           # ... but not wild
    long_n: int = 8
    short_n: int = 5
    cand_long: int = 12                # candidates examined for news/results before the final cut
    cand_short: int = 8
    # --- levels (multiples of the 14-day ATR measured from the trigger)
    sl_atr: float = 0.4
    t1_atr: float = 0.6
    t2_atr: float = 1.0
    skip_atr: float = 0.35             # do not chase if the stock opens this far beyond the trigger
    # --- risk / sizing (override with environment variables / GitHub repo variables)
    intraday_capital: float = 100_000.0
    intraday_risk_pct: float = 1.0     # % of capital risked per trade
    intraday_leverage: float = 1.0     # broker MIS leverage; 1 = no leverage assumed
    intraday_max_pos_frac: float = 0.34
    cnc_capital: float = 500_000.0
    cnc_risk_pct: float = 0.75
    cnc_max_pos_pct: float = 12.0      # max % of capital in one stock
    # --- CNC shortlist
    cnc_n: int = 10
    cnc_min_rr: float = 1.5
    cnc_per_bracket: int = 2
    cnc_cand: int = 25
    cnc_result_blackout_days: int = 3  # skip CNC ideas with results this close
    # --- events
    news_max_age_h: float = 72.0
    events_budget_s: int = 90

    @classmethod
    def from_env(cls) -> "DeskConfig":
        c = cls()
        c.intraday_capital = _envf("INTRADAY_CAPITAL", c.intraday_capital)
        c.intraday_risk_pct = _envf("INTRADAY_RISK_PCT", c.intraday_risk_pct)
        c.intraday_leverage = max(1.0, _envf("INTRADAY_LEVERAGE", c.intraday_leverage))
        c.cnc_capital = _envf("CNC_CAPITAL", c.cnc_capital)
        c.cnc_risk_pct = _envf("CNC_RISK_PCT", c.cnc_risk_pct)
        c.cnc_max_pos_pct = _envf("CNC_MAX_POSITION_PCT", c.cnc_max_pos_pct)
        return c


# --------------------------------------------------------------------------- #
# Market pulse (overnight US close, Asia already open, India VIX, crude, USD/INR)
# --------------------------------------------------------------------------- #
PULSE_TICKERS = [
    ("Nifty 50", "^NSEI"), ("Bank Nifty", "^NSEBANK"), ("India VIX", "^INDIAVIX"),
    ("S&P 500", "^GSPC"), ("Nasdaq", "^IXIC"), ("Nikkei 225", "^N225"), ("Hang Seng", "^HSI"),
    ("Brent crude", "BZ=F"), ("USD/INR", "INR=X"),
]


def pulse_from_series(label: str, close: pd.Series) -> dict | None:
    close = close.dropna()
    if len(close) < 2:
        return None
    last, prev = float(close.iloc[-1]), float(close.iloc[-2])
    if prev <= 0:
        return None
    return {"label": label, "last": last, "chg_pct": (last / prev - 1) * 100,
            "date": str(pd.Timestamp(close.index[-1]).date())}


def fetch_pulse() -> list[dict]:
    rows: list[dict] = []
    try:
        import yfinance as yf
    except Exception as exc:  # noqa: BLE001
        log.warning("Market pulse unavailable (yfinance import): %s", exc)
        return rows
    for label, sym in PULSE_TICKERS:
        try:
            d = yf.download(sym, period="10d", interval="1d", auto_adjust=False, progress=False)
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            r = pulse_from_series(label, d["Close"])
            if r:
                rows.append(r)
        except Exception as exc:  # noqa: BLE001
            log.warning("Pulse %s unavailable: %s", label, exc)
    return rows


def global_bias(pulse: list[dict]) -> dict:
    """One-line read of overnight / Asian cues. Descriptive, not a forecast."""
    by = {r["label"]: r for r in pulse}
    us = [by[k]["chg_pct"] for k in ("S&P 500", "Nasdaq") if k in by]
    asia = [by[k]["chg_pct"] for k in ("Nikkei 225", "Hang Seng") if k in by]
    vix = by.get("India VIX")
    notes, tone = [], "neutral"
    if us and max(us) <= -1.0:
        notes.append("US closed weak (both indices down 1% or more)")
        tone = "weak"
    elif us and min(us) >= 1.0:
        notes.append("US closed strong (both indices up 1% or more)")
        tone = "strong"
    elif us:
        notes.append("US closed mixed / flat")
    if asia and max(asia) <= -1.0:
        notes.append("Asian markets are trading lower")
        tone = "weak"
    elif asia and min(asia) >= 1.0:
        notes.append("Asian markets are trading higher")
        tone = "strong" if tone != "weak" else tone
    if vix and vix["last"] >= 20:
        notes.append(f"India VIX is {vix['last']:.1f} (elevated: expect wider swings, use smaller size)")
    if not notes:
        return {"tone": "unknown", "text": "Global cues unavailable."}
    hint = {"weak": " Gap-down opens are more likely than usual; longs need extra confirmation.",
            "strong": " Gap-up opens are more likely than usual; avoid chasing opening spikes.",
            "neutral": ""}.get(tone, "")
    return {"tone": tone, "text": "; ".join(notes) + "." + hint}


# --------------------------------------------------------------------------- #
# Position sizing (risk manager)
# --------------------------------------------------------------------------- #
def size_position(entry: float, stop: float, capital: float, risk_pct: float,
                  leverage: float = 1.0, max_pos_frac: float = 1.0) -> dict:
    """Shares such that hitting the stop loses about risk_pct of capital, capped by position size."""
    per_share = abs(entry - stop)
    if not (entry > 0 and per_share > 0 and capital > 0):
        return {"qty": 0, "value": 0.0, "margin": 0.0, "risk": 0.0, "capped": False}
    risk_amt = capital * risk_pct / 100.0
    q_risk = math.floor(risk_amt / per_share)
    q_cap = math.floor(capital * leverage * max_pos_frac / entry)
    qty = max(0, min(q_risk, q_cap))
    return {"qty": qty, "value": qty * entry, "margin": qty * entry / max(leverage, 1.0),
            "risk": qty * per_share, "capped": q_cap < q_risk}


# --------------------------------------------------------------------------- #
# Intraday (MIS) watchlist
# --------------------------------------------------------------------------- #
def close_loc(f: dict) -> float:
    rng = f["high"] - f["low"]
    return (f["close"] - f["low"]) / rng if rng > 0 else 0.5


def cpr_width_pct(f: dict) -> float:
    """Central pivot range width as % of pivot, from the previous session's H/L/C."""
    p = (f["high"] + f["low"] + f["close"]) / 3
    bc = (f["high"] + f["low"]) / 2
    tc = 2 * p - bc
    return abs(tc - bc) / p * 100 if p > 0 else 99.0


def intraday_eligible(f: dict, dc: DeskConfig) -> bool:
    if not all(k in f for k in ("high", "low", "open")):
        return False
    atr_pct = f["atr"] / f["close"] * 100
    return (dc.min_price <= f["close"] <= dc.max_price
            and f["turnover_cr"] >= dc.min_turnover_cr
            and f["zero_vol_days"] == 0
            and dc.atr_pct_min <= atr_pct <= dc.atr_pct_max
            and f["vol_ratio"] >= 0.8
            and f["max_abs_ret60"] <= 0.30)


def intraday_side(f: dict) -> str | None:
    loc, rsi = close_loc(f), f["rsi"]
    if f["close"] > f["ema20"] >= f["ema50"] and 50 <= rsi <= 75 and f["chg_pct"] > 0 and loc >= 0.6:
        return "long"
    if f["close"] < f["ema20"] <= f["ema50"] and 25 <= rsi <= 50 and f["chg_pct"] < 0 and loc <= 0.4:
        return "short"
    return None


def intraday_score(f: dict, side: str) -> tuple[float, list[str]]:
    loc = close_loc(f)
    strength = loc if side == "long" else 1 - loc          # how decisively it closed in the trade direction
    why: list[str] = []
    s = 25.0 if (f["close"] > f["ema20"] > f["ema50"] if side == "long" else f["close"] < f["ema20"] < f["ema50"]) else 14.0
    s += 20.0 * min(1.0, max(0.0, (strength - 0.5) / 0.5))
    if strength >= 0.8:
        why.append("closed near the " + ("high" if side == "long" else "low") + " of its range")
    vr = f["vol_ratio"]
    s += 20.0 * min(1.0, max(0.0, (vr - 0.8) / 1.2))
    if vr >= 1.3:
        why.append(f"volume {vr:.1f}× 20d avg")
    atr_pct = f["atr"] / f["close"] * 100
    s += 15.0 if 2.0 <= atr_pct <= 4.0 else 9.0
    why.append(f"ATR {atr_pct:.1f}%")
    s += 10.0 * min(1.0, max(0.0, math.log10(max(f["turnover_cr"], 1.0) / 25.0) / math.log10(8)))
    w = cpr_width_pct(f)
    s += 10.0 if w < 0.4 else (6.0 if w < 0.8 else 2.0)
    if w < 0.4:
        why.append(f"narrow CPR ({w:.2f}%): trend-day potential")
    return round(s, 1), why


def intraday_levels(f: dict, side: str, dc: DeskConfig) -> dict:
    a = f["atr"]
    if side == "long":
        trig = f["high"] + 0.05 * a
        sl, t1, t2 = trig - dc.sl_atr * a, trig + dc.t1_atr * a, trig + dc.t2_atr * a
        skip = trig + dc.skip_atr * a
    else:
        trig = f["low"] - 0.05 * a
        sl, t1, t2 = trig + dc.sl_atr * a, trig - dc.t1_atr * a, trig - dc.t2_atr * a
        skip = trig - dc.skip_atr * a
    sl = max(sl, 0.05)
    risk = abs(trig - sl)
    return {"trigger": tick(trig), "sl": tick(sl), "t1": tick(t1), "t2": tick(t2), "skip": tick(skip),
            "rr1": abs(t1 - trig) / risk if risk > 0 else 0.0,
            "sl_pct": abs(sl / trig - 1) * 100, "t1_pct": abs(t1 / trig - 1) * 100}


def intraday_candidates(feats: dict[str, dict], names: dict[str, str], dc: DeskConfig) -> dict:
    longs, shorts, n_elig = [], [], 0
    for sym, f in feats.items():
        if not intraday_eligible(f, dc):
            continue
        n_elig += 1
        side = intraday_side(f)
        if side is None:
            continue
        score, why = intraday_score(f, side)
        row = {"symbol": sym, "name": names.get(sym, sym), "side": side, "cmp": f["close"],
               "chg_pct": f["chg_pct"], "score": score, "why": ", ".join(why[:4]),
               "turnover_cr": f["turnover_cr"], **intraday_levels(f, side, dc)}
        (longs if side == "long" else shorts).append(row)
    key = lambda r: (r["score"], r["turnover_cr"])  # noqa: E731
    longs.sort(key=key, reverse=True)
    shorts.sort(key=key, reverse=True)
    return {"long": longs[: dc.cand_long], "short": shorts[: dc.cand_short],
            "eligible": n_elig, "long_total": len(longs), "short_total": len(shorts)}


# --------------------------------------------------------------------------- #
# News headlines and results dates
# --------------------------------------------------------------------------- #
NEG = re.compile(r"\b(fraud|sebi|probe|raid|penalty|penalised|default|downgrade[sd]?|resign\w*|pledg\w*|insolvency|nclt|"
                 r"scam|plunge[sd]?|slump\w*|tumble[sd]?|crash\w*|loss(?:es)?|halt\w*|show[- ]cause|ban(?:ned)?|"
                 r"cuts? guidance|misses?)\b", re.I)
POS = re.compile(r"\b(order win|bags?|wins?|secures?|upgrade[sd]?|record|surge[sd]?|jumps?|beats?|acquisition|buyback|"
                 r"dividend|approval|approved|contract|expansion|rall(?:y|ies))\b", re.I)


def headline_tone(title: str) -> str:
    neg, pos = bool(NEG.search(title)), bool(POS.search(title))
    if neg and not pos:
        return "neg"
    if pos and not neg:
        return "pos"
    return "mixed" if (neg and pos) else "neutral"


def parse_news_item(item: dict, now_utc: datetime) -> dict | None:
    """Handle both yfinance news layouts (old flat dicts and the newer {'content': {...}} form)."""
    try:
        c = item.get("content") if isinstance(item.get("content"), dict) else item
        title = (c.get("title") or "").strip()
        if not title:
            return None
        url = ""
        for k in ("canonicalUrl", "clickThroughUrl"):
            v = c.get(k)
            if isinstance(v, dict) and v.get("url"):
                url = v["url"]
                break
        url = url or c.get("link") or ""
        ts = None
        if c.get("pubDate"):
            ts = datetime.fromisoformat(str(c["pubDate"]).replace("Z", "+00:00"))
        elif c.get("providerPublishTime"):
            ts = datetime.fromtimestamp(int(c["providerPublishTime"]), tz=timezone.utc)
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return {"headline": title, "url": url, "age_h": (now_utc - ts).total_seconds() / 3600.0,
                "tone": headline_tone(title)}
    except Exception:  # noqa: BLE001
        return None


def parse_earnings_date(cal, ref: date) -> date | None:
    """Next results date on/after ref from Ticker.calendar (dict in current yfinance, DataFrame in old ones)."""
    try:
        vals = None
        if isinstance(cal, dict):
            vals = cal.get("Earnings Date")
        elif isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
            vals = list(cal.loc["Earnings Date"].dropna())
        if vals is None:
            return None
        if not isinstance(vals, (list, tuple)):
            vals = [vals]
        days = []
        for v in vals:
            d = pd.Timestamp(v).date()
            if d >= ref:
                days.append(d)
        return min(days) if days else None
    except Exception:  # noqa: BLE001
        return None


def _events_for(sym: str, ref: date, now_utc: datetime, max_age_h: float) -> dict:
    import yfinance as yf

    out: dict = {}
    t = yf.Ticker(f"{sym}.NS")
    try:
        items = [parse_news_item(i, now_utc) for i in (t.news or [])]
        items = [i for i in items if i and 0 <= i["age_h"] <= max_age_h]
        if items:
            best = min(items, key=lambda i: i["age_h"])
            out.update(best)
    except Exception as exc:  # noqa: BLE001
        log.debug("news %s: %s", sym, exc)
    try:
        d = parse_earnings_date(t.calendar, ref)
        if d:
            out["earn_date"] = str(d)
            out["earn_days"] = (d - ref).days
    except Exception as exc:  # noqa: BLE001
        log.debug("calendar %s: %s", sym, exc)
    return out


def fetch_events(symbols: list[str], ref: date, dc: DeskConfig, now_utc: datetime | None = None) -> dict[str, dict]:
    """{symbol: {headline,url,age_h,tone,earn_date,earn_days}} for whichever symbols answered in time."""
    now_utc = now_utc or datetime.now(timezone.utc)
    res: dict[str, dict] = {}
    if not symbols:
        return res
    ex = ThreadPoolExecutor(max_workers=6)
    futs = {ex.submit(_events_for, s, ref, now_utc, dc.news_max_age_h): s for s in symbols}
    try:
        for fu in as_completed(futs, timeout=dc.events_budget_s):
            try:
                ev = fu.result()
                if ev:
                    res[futs[fu]] = ev
            except Exception as exc:  # noqa: BLE001
                log.debug("events %s: %s", futs[fu], exc)
    except FutTimeout:
        log.warning("News/results lookup stopped after %ss (%d of %d symbols done)",
                    dc.events_budget_s, len(res), len(symbols))
    ex.shutdown(wait=False, cancel_futures=True)
    return res


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def cnc_candidates(report: dict, dc: DeskConfig) -> list[dict]:
    rows = []
    for b in report["brackets"]:
        for r in b["rows"]:
            if r["strict"] and r["rr"] >= dc.cnc_min_rr:
                rows.append({**r, "bracket": b["label"]})
    rows.sort(key=lambda r: r["rank_score"], reverse=True)
    return rows[: dc.cnc_cand]


def build_cnc(cands: list[dict], events: dict[str, dict], ref: date, dc: DeskConfig) -> tuple[list[dict], list[str]]:
    out, skipped, per = [], [], {}
    for r in cands:
        ev = events.get(r["symbol"], {})
        ed = ev.get("earn_days")
        if ed is not None and 0 <= ed <= dc.cnc_result_blackout_days:
            skipped.append(f"{r['symbol']} (results {ev['earn_date']})")
            continue
        if per.get(r["bracket"], 0) >= dc.cnc_per_bracket:
            continue
        per[r["bracket"]] = per.get(r["bracket"], 0) + 1
        sz = size_position(r["entry"], r["sl"], dc.cnc_capital, dc.cnc_risk_pct, 1.0, dc.cnc_max_pos_pct / 100.0)
        in_zone = r["cmp"] <= r["buy_high"] * 1.01
        out.append({"symbol": r["symbol"], "name": r["name"], "bracket": r["bracket"], "cmp": r["cmp"],
                    "chg_pct": r["chg_pct"], "buy_low": r["buy_low"], "buy_high": r["buy_high"],
                    "sl": r["sl"], "sl_pct": r["sl_pct"], "st": r["st"], "st_pct": r["st_pct"],
                    "mt": r["mt"], "mt_pct": r["mt_pct"], "rr": r["rr"], "score": r["score"],
                    "status": "In buy zone" if in_zone else "Wait for pullback",
                    "why": r["rationale"], "size": sz, "event": ev or None})
        if len(out) >= dc.cnc_n:
            break
    return out, skipped


def build_desk(feats: dict[str, dict], names: dict[str, str], report: dict, dc: DeskConfig,
               events_fn=None, pulse_fn=None) -> dict:
    ref = report["data_date"] if isinstance(report["data_date"], date) else date.fromisoformat(str(report["data_date"]))
    session = ref + timedelta(days=1)
    while session.weekday() >= 5:
        session += timedelta(days=1)

    pulse = (pulse_fn or fetch_pulse)()
    ic = intraday_candidates(feats, names, dc)
    cnc_c = cnc_candidates(report, dc)

    syms = sorted({r["symbol"] for r in ic["long"] + ic["short"]} | {r["symbol"] for r in cnc_c})
    events = (events_fn or (lambda s: fetch_events(s, session, dc)))(syms)

    skipped_i = []
    long_rows, short_rows = [], []
    for side, src, dst, n in (("long", ic["long"], long_rows, dc.long_n), ("short", ic["short"], short_rows, dc.short_n)):
        for r in src:
            ev = events.get(r["symbol"], {})
            if ev.get("earn_days") is not None and ev["earn_days"] == 0:
                skipped_i.append(f"{r['symbol']} (results due {ev['earn_date']})")
                continue
            if len(dst) >= n:
                continue
            sz = size_position(r["trigger"], r["sl"], dc.intraday_capital, dc.intraday_risk_pct,
                               dc.intraday_leverage, dc.intraday_max_pos_frac)
            gain = sz["qty"] * abs(r["t1"] - r["trigger"])
            dst.append({**r, "size": {**sz, "gain_t1": gain}, "event": ev or None})
    cnc, skipped_c = build_cnc(cnc_c, events, ref, dc)

    return {
        "session": str(session), "pulse": pulse, "bias": global_bias(pulse),
        "intraday": {"long": long_rows, "short": short_rows, "eligible": ic["eligible"],
                     "long_total": ic["long_total"], "short_total": ic["short_total"], "skipped": skipped_i},
        "cnc": {"rows": cnc, "skipped": skipped_c, "candidates": len(cnc_c)},
        "events_found": len(events),
        "params": {"intraday_capital": dc.intraday_capital, "intraday_risk_pct": dc.intraday_risk_pct,
                   "intraday_leverage": dc.intraday_leverage, "cnc_capital": dc.cnc_capital,
                   "cnc_risk_pct": dc.cnc_risk_pct, "cnc_max_pos_pct": dc.cnc_max_pos_pct,
                   "cnc_blackout_days": dc.cnc_result_blackout_days},
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
CSS = """
.pulse{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
.pc{background:#fff;border:1px solid #dfe3ea;border-radius:8px;padding:6px 10px;min-width:104px}
.pc .l{font-size:11px;color:#6b7587}.pc .v{font-size:14px;font-weight:600}
.bs2{display:inline-block;padding:0 7px;border-radius:9px;font-size:10px;font-weight:700;margin-right:4px}
.sbuy{background:#e3f4e8;color:#1b6b34}.ssell{background:#fbe3e1;color:#a3201a}
.ev{font-size:11px;margin-top:2px}.ev a{color:#2b4a7a;text-decoration:none}
.evneg{color:#a3201a}.evpos{color:#1b6b34}.evwarn{color:#8a5a00;font-weight:600}
.zone{background:#e3f4e8;color:#1b6b34}.wait{background:#fdf0d5;color:#8a5a00}
"""


def _inr(x: float) -> str:
    return f"₹{x:,.2f}"


def _inr0(x: float) -> str:
    return f"₹{x:,.0f}"


def _pct(x: float) -> str:
    return f"{x:+.1f}%"


def _event_html(ev: dict | None, ref_session: str) -> str:
    if not ev:
        return ""
    bits = []
    if ev.get("earn_date") is not None:
        d = ev.get("earn_days")
        if d is not None and d <= 7:
            when = "today" if d == 0 else ("tomorrow" if d == 1 else f"in {d} days")
            bits.append(f'<span class="evwarn">⚠ Results {escape(ev["earn_date"])} ({when})</span>')
    if ev.get("headline"):
        cls = {"neg": "evneg", "pos": "evpos"}.get(ev.get("tone", ""), "")
        mark = {"neg": "▼ ", "pos": "▲ "}.get(ev.get("tone", ""), "")
        age = ev.get("age_h", 0)
        ago = f"{age:.0f}h ago" if age < 48 else f"{age / 24:.0f}d ago"
        title = escape(ev["headline"][:110])
        link = f'<a href="{escape(ev["url"])}">{title}</a>' if ev.get("url") else title
        bits.append(f'<span class="{cls}">{mark}{link} <span class="sm">({ago})</span></span>')
    return f'<div class="ev">{"<br>".join(bits)}</div>' if bits else ""


def render_pulse_html(d: dict) -> str:
    cards = []
    for r in d["pulse"]:
        cls = "up" if r["chg_pct"] >= 0 else "dn"
        if r["label"] == "India VIX":
            cls = "dn" if r["last"] >= 20 else ""
        fmt = f"{r['last']:,.2f}" if r["last"] < 1000 else f"{r['last']:,.0f}"
        cards.append(f'<div class="pc"><div class="l">{escape(r["label"])}</div><div class="v">{fmt}</div>'
                     f'<div class="sm {cls}">{_pct(r["chg_pct"])}</div></div>')
    b = d["bias"]
    cls = {"weak": "warn", "strong": "ok"}.get(b["tone"], "")
    return ('<h2 id="pulse">Market pulse <span class="sm">· cues available before the open</span></h2>'
            + (f'<div class="pulse">{"".join(cards)}</div>' if cards else "")
            + f'<div class="banner {cls}">{escape(b["text"])}</div>')


def render_intraday_html(d: dict) -> str:
    it, p = d["intraday"], d["params"]
    head = (f'<h2 id="intraday">Intraday (MIS) watchlist for {escape(d["session"])} <span class="sm">· trigger levels from the '
            f'last session; nothing has fired yet</span></h2>')

    def table(rows: list[dict], side: str) -> str:
        if not rows:
            return f'<div class="banner">No {side} candidates passed the filters today.</div>'
        body = []
        for i, r in enumerate(rows, 1):
            buy = side == "long"
            url = f"https://www.screener.in/company/{quote(r['symbol'])}/"
            sz = r["size"]
            qty = (f'{sz["qty"]:,} sh<div class="sm">margin {_inr0(sz["margin"])} · risk {_inr0(sz["risk"])}'
                   f' · T1 gain {_inr0(sz["gain_t1"])}</div>') if sz["qty"] > 0 else '<span class="sm">too small to size</span>'
            body.append(
                f'<tr><td class="n">{i}</td><td class="nm"><a href="{url}">{escape(r["name"])}</a>'
                f'<div class="tk">NSE: {escape(r["symbol"])} · score {r["score"]:.0f}/100 · prev close {_inr(r["cmp"])} '
                f'({_pct(r["chg_pct"])})</div>{_event_html(r.get("event"), d["session"])}</td>'
                f'<td class="n"><span class="bs2 {"sbuy" if buy else "ssell"}">{"BUY above" if buy else "SELL below"}</span>'
                f'<br><strong>{_inr(r["trigger"])}</strong><div class="sm">skip if opens beyond {_inr(r["skip"])}</div></td>'
                f'<td class="n dn">{_inr(r["sl"])}<div class="sm">-{r["sl_pct"]:.1f}%</div></td>'
                f'<td class="n up">{_inr(r["t1"])}<div class="sm">+{r["t1_pct"]:.1f}% · R:R {r["rr1"]:.1f}</div></td>'
                f'<td class="n up">{_inr(r["t2"])}</td><td class="n">{qty}</td>'
                f'<td class="rat">{escape(r["why"])}</td></tr>')
        return ('<div class="scroll"><table><tr><th class="n">#</th><th>Stock</th><th class="n">Trigger</th>'
                '<th class="n">Stop-loss</th><th class="n">Target 1</th><th class="n">Target 2</th>'
                '<th class="n">Qty (sized)</th><th>Why on the list</th></tr>' + "".join(body) + "</table></div>")

    skipped = (f'<div class="sm">Skipped because results are due on the session date: {escape(", ".join(it["skipped"]))}</div>'
               if it["skipped"] else "")
    note = (f'<div class="meth"><strong>How to use.</strong> {it["eligible"]} stocks passed the intraday liquidity filters '
            f'(20-day turnover ≥ ₹25 cr, ATR 1.5-6% of price, no zero-volume days); {it["long_total"]} lean long and '
            f'{it["short_total"]} lean short. A <em>long</em> needs a close above its 20 &amp; 50 EMA, RSI 50-75, up day, close in the top '
            f'40% of the range; a <em>short</em> is the mirror image. Trigger = previous high (long) or low (short) ± 0.05 ATR. '
            f'Stop = 0.4 ATR, Target 1 = 0.6 ATR, Target 2 = 1.0 ATR from the trigger. Only act if price crosses the trigger '
            f'after the first 15-minute candle, ideally with volume, and not if it opens beyond the "skip" level. '
            f'<strong>Square off before about 3:15 PM</strong> (brokers auto-square-off MIS positions shortly after). '
            f'Short-selling is intraday-only and some stocks are not shortable or are in trade-to-trade: check your broker\'s MIS list. '
            f'Quantity assumes ₹{p["intraday_capital"]:,.0f} capital, {p["intraday_risk_pct"]:g}% risk per trade, '
            f'{p["intraday_leverage"]:g}× leverage and at most 34% of buying power in one stock (change via the '
            f'INTRADAY_CAPITAL / INTRADAY_RISK_PCT / INTRADAY_LEVERAGE settings). Stops can slip in fast markets. '
            f'Mechanical estimates, not forecasts.</div>')
    return (head + '<h3 style="font-size:14px;margin:12px 0 6px">Long candidates</h3>' + table(it["long"], "long")
            + '<h3 style="font-size:14px;margin:14px 0 6px">Short candidates (MIS only)</h3>' + table(it["short"], "short")
            + skipped + note)


def render_cnc_html(d: dict) -> str:
    c, p = d["cnc"], d["params"]
    head = ('<h2 id="cnc">CNC delivery shortlist <span class="sm">· best full-match ideas across all price brackets, '
            "swing to 3 months</span></h2>")
    if not c["rows"]:
        return head + '<div class="banner">No full-match delivery idea cleared the reward-to-risk and results-date checks today.</div>'
    body = []
    for i, r in enumerate(c["rows"], 1):
        url = f"https://www.screener.in/company/{quote(r['symbol'])}/"
        sz = r["size"]
        cls = "zone" if r["status"] == "In buy zone" else "wait"
        qty = (f'{sz["qty"]:,} sh<div class="sm">{_inr0(sz["value"])} · risk {_inr0(sz["risk"])}'
               f'{" · capped by max position" if sz["capped"] else ""}</div>') if sz["qty"] > 0 else '<span class="sm">too small to size</span>'
        body.append(
            f'<tr><td class="n">{i}</td><td class="nm"><a href="{url}">{escape(r["name"])}</a>'
            f'<span class="b {cls}">{escape(r["status"])}</span>'
            f'<div class="tk">NSE: {escape(r["symbol"])} · {escape(r["bracket"])} · score {r["score"]:.0f}/100</div>'
            f'{_event_html(r.get("event"), d["session"])}</td>'
            f'<td class="n">{_inr(r["cmp"])}<div class="sm {"up" if r["chg_pct"] >= 0 else "dn"}">{_pct(r["chg_pct"])}</div></td>'
            f'<td class="n">{_inr(r["buy_low"])} – {_inr(r["buy_high"])}</td>'
            f'<td class="n up">{_inr(r["st"])}<div class="sm">{_pct(r["st_pct"])}</div></td>'
            f'<td class="n up">{_inr(r["mt"])}<div class="sm">{_pct(r["mt_pct"])}</div></td>'
            f'<td class="n dn">{_inr(r["sl"])}<div class="sm">{_pct(r["sl_pct"])}</div></td>'
            f'<td class="n">{r["rr"]:.1f}</td><td class="n">{qty}</td><td class="rat">{escape(r["why"])}</td></tr>')
    skipped = (f'<div class="sm">Held back because results are within {p["cnc_blackout_days"]} days: {escape(", ".join(c["skipped"]))}</div>'
               if c["skipped"] else "")
    note = (f'<div class="meth">Picked from the {c["candidates"]} best Full-match names in the price-bracket tables '
            f'(above 50 &amp; 200 EMA, RSI 50-70, volume ≥ average, no fundamental red flag, reward-to-risk ≥ 1.5), max 2 per '
            f'bracket. "Wait for pullback" means the last close is above the buy zone: use a limit order inside the zone rather than '
            f'chasing. Quantity assumes ₹{p["cnc_capital"]:,.0f} capital, {p["cnc_risk_pct"]:g}% risk to the stop-loss and at most '
            f'{p["cnc_max_pos_pct"]:g}% of capital per stock (CNC_CAPITAL / CNC_RISK_PCT / CNC_MAX_POSITION_PCT settings). '
            f'Review the stop-loss on a closing basis and re-check news before buying.</div>')
    return (head + '<div class="scroll"><table><tr><th class="n">#</th><th>Stock</th><th class="n">CMP</th>'
            '<th class="n">Buy zone</th><th class="n">Target 1-4 wk</th><th class="n">Target 1-3 mo</th>'
            '<th class="n">Stop-loss</th><th class="n">R:R</th><th class="n">Qty (sized)</th><th>Why</th></tr>'
            + "".join(body) + "</table></div>" + skipped + note)


def render_html(d: dict) -> str:
    return render_pulse_html(d) + render_intraday_html(d) + render_cnc_html(d)


def nav_links() -> str:
    return ('<a href="#pulse">Market pulse</a><a href="#intraday">Intraday</a><a href="#cnc">CNC delivery</a>')


def render_text(d: dict) -> str:
    out = [f"== Market pulse ==", d["bias"]["text"]]
    out.append("  " + " | ".join(f"{r['label']} {r['last']:,.2f} ({r['chg_pct']:+.1f}%)" for r in d["pulse"]))
    out.append("")
    out.append(f"== Intraday (MIS) watchlist for {d['session']} ==")
    for side, label in (("long", "BUY above"), ("short", "SELL below")):
        for i, r in enumerate(d["intraday"][side], 1):
            q = r["size"]["qty"]
            out.append(f"{label[:4]} {i:>2}. {r['symbol']:<12} {label} {r['trigger']:.2f} | SL {r['sl']:.2f} | "
                       f"T1 {r['t1']:.2f} | T2 {r['t2']:.2f} | qty {q}")
    out.append("Square off before ~3:15 PM. Act only if the trigger breaks after the first 15-min candle.")
    out.append("")
    out.append("== CNC delivery shortlist ==")
    for i, r in enumerate(d["cnc"]["rows"], 1):
        out.append(f"{i:>2}. {r['symbol']:<12} CMP {r['cmp']:.2f} | buy {r['buy_low']:.2f}-{r['buy_high']:.2f} "
                   f"({r['status']}) | T1 {r['st']:.2f} | T2 {r['mt']:.2f} | SL {r['sl']:.2f} | qty {r['size']['qty']}")
    out.append("")
    return "\n".join(out)

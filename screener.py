#!/usr/bin/env python3
"""
Daily NSE price-bracket stock screener.

Pipeline
  1. Load the NSE universe: every NSE EQ stock (default), or Nifty Total Market / Nifty 500 constituents.
  2. Download ~1 year of daily OHLCV from Yahoo Finance (yfinance).
  3. Compute EMA / RSI / ATR / pivots / Fibonacci levels (pure pandas).
  4. Bucket stocks by last close into 10 price brackets, filter illiquid names,
     rank with a transparent 100-point score (trend, RSI, volume, momentum,
     fundamentals) and keep the top N per bracket.
  5. Derive buy zone, 1-4 week target, 1-3 month target and stop-loss.
  6. Flag fresh breakouts / volume surges, and rank stocks already listed on earlier days lower
     (data/pick_history.json) so the lists rotate instead of repeating.
  7. Run the 22-filter quality small-cap screen (quality.py) on liquid, trending names.
  8. Render an HTML dashboard (docs/index.html) and email it.

IMPORTANT: at 08:30 IST the market has not opened, so "CMP" is the previous
session's closing price.  Educational / research use only - not investment advice.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import math
import os
import smtplib
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

try:  # 22-filter quality small-cap screen (quality.py, same folder). Optional: the daily report
    import quality  # must still go out if that file was not uploaded.
except ImportError:  # pragma: no cover
    quality = None

try:  # Telegram delivery (notify_telegram.py). Optional.
    import notify_telegram
except ImportError:  # pragma: no cover
    notify_telegram = None

try:  # intraday watchlist, CNC shortlist, market pulse, news flags, position sizing (desk.py). Optional too.
    import desk
except ImportError:  # pragma: no cover
    desk = None

log = logging.getLogger("screener")
IST = timezone(timedelta(hours=5, minutes=30))
TICK = 0.05  # NSE tick size for most equities

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BRACKET_EDGES = [0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]


def bracket_label(lo: int, hi: int) -> str:
    return "Below ₹100" if lo == 0 else f"₹{lo + 1:,} – ₹{hi:,}"


BRACKETS = [(lo, hi, bracket_label(lo, hi)) for lo, hi in zip(BRACKET_EDGES[:-1], BRACKET_EDGES[1:])]

UNIVERSE_URLS = {
    "total": [
        "https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv",
        "https://niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
    ],
    "500": [
        "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
        "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    ],
    # Every equity listed on NSE (~2,000+). Used for UNIVERSE=all, on top of the Total Market list.
    "all": [
        "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
        "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    ],
}

# Last-resort fallback so the job never dies if NSE blocks the download and no cache exists.
SEED_SYMBOLS = (
    "RELIANCE TCS HDFCBANK INFY ICICIBANK SBIN ITC LT AXISBANK KOTAKBANK BHARTIARTL "
    "HINDUNILVR MARUTI SUNPHARMA TATAMOTORS TATASTEEL NTPC POWERGRID ONGC COALINDIA "
    "BPCL IOC GAIL SAIL NMDC BANKBARODA PNB CANBK UNIONBANK IDFCFIRSTB FEDERALBNK "
    "RBLBANK YESBANK IRFC RVNL IRCON NHPC SJVN SUZLON BEL HAL BHEL RECLTD PFC "
    "VEDL HINDALCO JINDALSTEL NATIONALUM MOTHERSON ASHOKLEY TATAPOWER ADANIPOWER "
    "IDEA ZOMATO PAYTM NYKAA POLICYBZR DELHIVERY"
).split()


@dataclass
class Config:
    top_n: int = 10
    min_price: float = 10.0            # drop sub-₹10 stocks
    min_turnover_cr: float = 3.0       # 20-day avg traded value, ₹ crore
    min_bars: int = 120                # need enough history for EMA50 / RSI / ATR
    fundamentals_pool: int = 30        # per bracket, fetch fundamentals only for the best N technical names
    universe: str = "all"              # "all" (every NSE EQ stock), "total" (≈750) or "500"
    history_period: str = "1y"
    chunk: int = 100
    history_budget_s: int = 600        # stop downloading price history after this; the rest is skipped
    max_stale_days: int = 6            # data older than this => abort
    fundamentals_ttl_days: int = 7
    fundamentals_budget_s: int = 420
    # variety: stocks that stay on the list day after day are ranked lower, and breakouts get their own section
    rotation_pts: float = 3.0          # score points deducted per consecutive prior day on the list
    rotation_max_days: int = 5         # ... up to this many days (max 15 points)
    fresh_top_n: int = 15
    pick_history_days: int = 15
    data_dir: Path = Path("data")
    out_dir: Path = Path("docs")
    keep_archive_days: int = 45


# --------------------------------------------------------------------------- #
# Universe
# --------------------------------------------------------------------------- #
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,*/*;q=0.8",
    "Referer": "https://www.niftyindices.com/",
}


def _load_all(cfg: Config) -> pd.DataFrame:
    """Total Market list first (it carries industries), then every other NSE EQ symbol."""
    base = load_universe(replace(cfg, universe="total"))
    cache = cfg.data_dir / "universe_all_extra.csv"
    extra = None
    for url in UNIVERSE_URLS["all"]:
        for attempt in range(2):
            try:
                r = requests.get(url, headers=_HEADERS, timeout=30)
                r.raise_for_status()
                df = _normalise_universe(pd.read_csv(io.StringIO(r.text)))
                if len(df) >= 1000:
                    df.to_csv(cache, index=False)
                    extra = df
                    break
            except Exception as exc:  # noqa: BLE001
                log.warning("Full NSE list download failed (%s, try %d): %s", url, attempt + 1, exc)
                time.sleep(2)
        if extra is not None:
            break
    if extra is None and cache.exists():
        extra = pd.read_csv(cache)
        log.warning("Using cached full NSE list (%d symbols)", len(extra))
    if extra is None:
        log.warning("Full NSE list unavailable - using the %d-symbol Total Market list", len(base))
        return base
    extra = extra[~extra["symbol"].isin(set(base["symbol"]))]
    out = pd.concat([base, extra], ignore_index=True)
    log.info("Universe: %d symbols (%d Total Market + %d other NSE EQ)", len(out), len(base), len(extra))
    return out


def load_universe(cfg: Config) -> pd.DataFrame:
    """Return DataFrame[symbol, name, industry]. Live -> cache -> seed list."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if cfg.universe == "all":
        return _load_all(cfg)
    cache = cfg.data_dir / f"universe_{cfg.universe}.csv"
    urls = UNIVERSE_URLS.get(cfg.universe, []) + (UNIVERSE_URLS["500"] if cfg.universe != "500" else [])
    for url in urls:
        for attempt in range(2):
            try:
                r = requests.get(url, headers=_HEADERS, timeout=30)
                r.raise_for_status()
                raw = pd.read_csv(io.StringIO(r.text))
                df = _normalise_universe(raw)
                if len(df) >= 100:
                    df.to_csv(cache, index=False)
                    log.info("Universe: %d symbols from %s", len(df), url)
                    return df
            except Exception as exc:  # noqa: BLE001
                log.warning("Universe download failed (%s, try %d): %s", url, attempt + 1, exc)
                time.sleep(2)
    if cache.exists():
        df = pd.read_csv(cache)
        log.warning("Using cached universe (%d symbols) from %s", len(df), cache)
        return df
    log.error("No live or cached universe - falling back to %d-symbol seed list", len(SEED_SYMBOLS))
    return pd.DataFrame({"symbol": SEED_SYMBOLS, "name": SEED_SYMBOLS, "industry": ""})


def _normalise_universe(raw: pd.DataFrame) -> pd.DataFrame:
    cols = {c.strip().lower(): c for c in raw.columns}
    sym, name = cols.get("symbol"), cols.get("company name") or cols.get("name of company")
    if sym is None:
        raise ValueError(f"Unexpected universe columns: {list(raw.columns)}")
    df = pd.DataFrame(
        {
            "symbol": raw[sym].astype(str).str.strip(),
            "name": raw[name].astype(str).str.strip() if name else raw[sym].astype(str).str.strip(),
            "industry": raw[cols["industry"]].astype(str).str.strip() if "industry" in cols else "",
        }
    )
    if "series" in cols:
        df = df[raw[cols["series"]].astype(str).str.strip().eq("EQ").values]
    return df.drop_duplicates("symbol").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
def _chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def fetch_history(symbols: list[str], cfg: Config) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    deadline = time.time() + cfg.history_budget_s
    for chunk in _chunks(symbols, cfg.chunk):
        if time.time() > deadline:
            log.warning("Price-history budget (%ds) used up - skipping the remaining symbols", cfg.history_budget_s)
            break
        tickers = [f"{s}.NS" for s in chunk]
        raw = None
        for attempt in range(3):
            try:
                raw = yf.download(
                    tickers, period=cfg.history_period, interval="1d", group_by="ticker",
                    auto_adjust=False, threads=True, progress=False,
                )
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("yfinance chunk failed (try %d): %s", attempt + 1, exc)
                time.sleep(3 * (attempt + 1))
        if raw is None or raw.empty:
            continue
        for s in chunk:
            t = f"{s}.NS"
            try:
                d = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
            except KeyError:
                continue
            if not d.empty:
                out[s] = d
        time.sleep(1)
    log.info("Downloaded history for %d / %d symbols", len(out), len(symbols))
    return out


def market_context(cfg: Config) -> dict:
    """Nifty 50 trend snapshot used for the report header."""
    try:
        import yfinance as yf

        d = yf.download("^NSEI", period="1y", interval="1d", auto_adjust=False, progress=False)
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        return nifty_snapshot(d["Close"].dropna())
    except Exception as exc:  # noqa: BLE001
        log.warning("Nifty context unavailable: %s", exc)
        return {}


def nifty_snapshot(close: pd.Series) -> dict:
    if len(close) < 50:
        return {}
    last, prev = float(close.iloc[-1]), float(close.iloc[-2])
    d50 = float(close.tail(50).mean())
    d200 = float(close.tail(200).mean()) if len(close) >= 200 else math.nan
    return {
        "close": last,
        "chg_pct": (last / prev - 1) * 100,
        "above_50": last > d50,
        "above_200": (last > d200) if not math.isnan(d200) else None,
        "date": str(close.index[-1].date()),
    }


# --------------------------------------------------------------------------- #
# Indicators (pure pandas - no pandas_ta dependency)
# --------------------------------------------------------------------------- #
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi_wilder(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(~((ad == 0) & (au > 0)), 100.0)


def atr_wilder(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def tick(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


def compute_features(df: pd.DataFrame, cfg: Config, today: datetime | None = None) -> dict | None:
    """Indicators + support/resistance reference levels for one stock (last bar)."""
    df = df.copy()
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df = df.dropna(subset=["Close", "High", "Low"])
    # Drop a placeholder bar for "today" with no volume (pre-open Yahoo artefact)
    today_d = (today or datetime.now(IST)).date()
    if len(df) and df.index[-1].date() == today_d and (df["Volume"].iloc[-1] or 0) == 0:
        df = df.iloc[:-1]
    if len(df) < cfg.min_bars:
        return None

    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"].fillna(0)
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    rsi, atr = rsi_wilder(c), atr_wilder(h, l, c)
    close = float(c.iloc[-1])
    a = float(atr.iloc[-1])
    if not (close > 0 and a > 0) or math.isnan(float(rsi.iloc[-1])):
        return None

    vol_avg20 = float(v.iloc[-21:-1].mean()) if len(v) > 21 else float(v.mean())
    vol_avg5 = float(v.iloc[-5:].mean())
    turnover_cr = float((c.iloc[-20:] * v.iloc[-20:]).mean() / 1e7)

    # 5-session pivot points
    h5, l5 = float(h.iloc[-5:].max()), float(l.iloc[-5:].min())
    pv = (h5 + l5 + close) / 3
    # 60-session swing for Fibonacci
    hi60, lo60 = float(h.iloc[-60:].max()), float(l.iloc[-60:].min())
    rng = max(hi60 - lo60, 1e-9)

    rets = c.pct_change().iloc[-60:]
    return {
        "date": df.index[-1].date(),
        "close": close,
        "open": float(df["Open"].iloc[-1]) if "Open" in df and not math.isnan(float(df["Open"].iloc[-1])) else close,
        "high": float(h.iloc[-1]), "low": float(l.iloc[-1]),
        "chg_pct": float((c.iloc[-1] / c.iloc[-2] - 1) * 100),
        "ema20": float(e20.iloc[-1]), "ema50": float(e50.iloc[-1]),
        "ema200": float(e200.iloc[-1]) if not math.isnan(float(e200.iloc[-1])) else math.nan,
        "sma200": float(c.iloc[-200:].mean()) if len(c) >= 200 else math.nan,
        "rsi": float(rsi.iloc[-1]), "atr": a,
        "vol": float(v.iloc[-1]), "vol_avg20": vol_avg20,
        "vol_ratio": float(v.iloc[-1] / vol_avg20) if vol_avg20 > 0 else 0.0,
        "vol_ratio5": float(vol_avg5 / vol_avg20) if vol_avg20 > 0 else 0.0,
        "turnover_cr": turnover_cr,
        "zero_vol_days": int((v.iloc[-20:] == 0).sum()),
        "ret20": float(c.iloc[-1] / c.iloc[-21] - 1) if len(c) > 21 else 0.0,
        "hi52": float(h.iloc[-252:].max()), "lo20": float(l.iloc[-20:].min()),
        "hi20": float(h.iloc[-20:].max()),
        # highs BEFORE the last session: a close above these is a fresh breakout
        "prior_hi20": float(h.iloc[-21:-1].max()), "prior_hi52": float(h.iloc[-253:-1].max()),
        "max_abs_ret60": float(rets.abs().max()) if len(rets.dropna()) else 0.0,
        # pivots
        "S1": 2 * pv - h5, "S2": pv - (h5 - l5), "R1": 2 * pv - l5, "R2": pv + (h5 - l5),
        # fibonacci (60-session swing)
        "hi60": hi60, "lo60": lo60,
        "fib382": hi60 - 0.382 * rng, "fib50": hi60 - 0.5 * rng, "fib618": hi60 - 0.618 * rng,
        "ext1272": lo60 + 1.272 * rng, "ext1618": lo60 + 1.618 * rng,
    }


# --------------------------------------------------------------------------- #
# Fundamentals (only for each bracket's best technical candidates)
# --------------------------------------------------------------------------- #
FUND_KEYS = ["debtToEquity", "earningsQuarterlyGrowth", "revenueGrowth", "trailingEps",
             "returnOnEquity", "sector"]


def fetch_fundamentals(symbols: list[str], cfg: Config) -> dict[str, dict]:
    path = cfg.data_dir / "fundamentals_cache.json"
    cache: dict[str, dict] = {}
    if path.exists():
        try:
            cache = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            cache = {}
    now = time.time()
    ttl = cfg.fundamentals_ttl_days * 86400
    todo = [s for s in symbols if now - cache.get(s, {}).get("_ts", 0) > ttl]
    log.info("Fundamentals: %d cached, %d to fetch", len(symbols) - len(todo), len(todo))
    deadline = now + cfg.fundamentals_budget_s

    def one(sym: str):
        if time.time() > deadline:
            return sym, None
        try:
            import yfinance as yf

            info = yf.Ticker(f"{sym}.NS").get_info()
            rec = {k: info.get(k) for k in FUND_KEYS}
            rec["_ts"] = time.time()
            time.sleep(0.2)
            return sym, rec
        except Exception as exc:  # noqa: BLE001
            log.debug("fundamentals %s failed: %s", sym, exc)
            return sym, None

    if todo:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for sym, rec in ex.map(one, todo):
                if rec:
                    cache[sym] = rec
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache))
    return {s: cache[s] for s in symbols if s in cache}


def _num(x):
    try:
        x = float(x)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def fundamentals_assessment(f: dict | None) -> dict:
    """Score (0-20) + red-flag + readable facts."""
    f = f or {}
    de, eg, rg = _num(f.get("debtToEquity")), _num(f.get("earningsQuarterlyGrowth")), _num(f.get("revenueGrowth"))
    eps, roe = _num(f.get("trailingEps")), _num(f.get("returnOnEquity"))
    financial = "financial" in str(f.get("sector") or "").lower()
    known = sum(x is not None for x in (de, eg, eps, roe))

    # Earnings momentum (max 8)
    g = eg if eg is not None else rg
    s_earn = 3 if g is None else (8 if g > 0.15 else 5 if g > 0 else 0)
    # Balance sheet (max 6) - yfinance reports D/E in percent (45 == 0.45x)
    if de is None or financial:
        s_bs = 3
    else:
        s_bs = 6 if de < 50 else 4 if de < 100 else 2 if de < 200 else 0
    # Profitability (max 6)
    if eps is None and roe is None:
        s_prof = 2
    else:
        s_prof = (3 if (eps or 0) > 0 else 0) + (3 if (roe or 0) > 0.12 else 0)

    red_flag = (eps is not None and eps < 0) or (de is not None and de > 200 and not financial)
    return {"score": s_earn + s_bs + s_prof, "red_flag": red_flag, "known": known,
            "de": None if de is None else de / 100, "growth": g, "eps": eps, "roe": roe,
            "financial": financial}


# --------------------------------------------------------------------------- #
# Scoring, selection, levels
# --------------------------------------------------------------------------- #
def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def technical_score(f: dict) -> dict:
    """0-80 from trend (30), RSI (15), volume (20), momentum (15)."""
    c = f["close"]
    e200 = f["ema200"]
    trend = (8 * (c > f["ema50"]) + 8 * (not math.isnan(e200) and c > e200)
             + 6 * (not math.isnan(e200) and f["ema50"] > e200)
             + 4 * (c > f["ema20"]) + 4 * (f["ema20"] > f["ema50"]))
    r = f["rsi"]
    if 50 <= r <= 70:
        rsi_s = 15 - 3 * abs(r - 60) / 10
    elif 45 <= r < 50 or 70 < r <= 75:
        rsi_s = 6
    else:
        rsi_s = 0
    vr = 0.6 * f["vol_ratio"] + 0.4 * f["vol_ratio5"]
    vol_s = 20 * clip01((vr - 0.8) / 1.2)
    below_hi = 1 - c / f["hi52"] if f["hi52"] > 0 else 1
    mom = 8 * clip01(f["ret20"] / 0.10) + 7 * clip01(1 - below_hi / 0.25)
    return {"trend": trend, "rsi": rsi_s, "volume": vol_s, "momentum": mom,
            "total": trend + rsi_s + vol_s + mom}


def criteria(f: dict) -> dict:
    """The user's explicit screening rules."""
    return {
        "Above 50-EMA": f["close"] > f["ema50"],
        "Above 200-EMA": (not math.isnan(f["ema200"])) and f["close"] > f["ema200"],
        "RSI 50-70": 50 <= f["rsi"] <= 70,
        "Volume > 20d avg": f["vol_ratio"] >= 1.0,
    }


def compute_levels(f: dict) -> dict:
    """Buy zone / targets / stop from ATR, pivots and Fibonacci."""
    cmp_, atr = f["close"], f["atr"]
    supports = [f["S1"], f["ema20"], f["ema50"], f["fib382"], f["fib50"], f["lo20"]]
    near = [s for s in supports if not math.isnan(s) and s < cmp_ and cmp_ - s <= 1.5 * atr]
    support = max(near) if near else cmp_ - 1.0 * atr
    buy_low = support
    buy_high = min(cmp_, support + 0.6 * atr)
    entry = (buy_low + buy_high) / 2

    sl = buy_low - 1.0 * atr
    sl = max(sl, entry * 0.90)            # never risk more than 10% of entry
    sl = max(sl, 0.05)

    ups = sorted({x for x in (f["R1"], f["R2"], f["hi60"], f["ext1272"], f["ext1618"]) if x > entry})
    st_c = [x for x in ups if entry + 1.5 * atr <= x <= entry + 3.0 * atr]
    st = st_c[0] if st_c else entry + 2.0 * atr
    mt_c = [x for x in ups if st + 1.0 * atr <= x <= entry + 7.0 * atr]
    mt = mt_c[0] if mt_c else max(entry + 4.5 * atr, st + 1.5 * atr)

    risk = max(entry - sl, 1e-9)
    return {
        "buy_low": tick(buy_low), "buy_high": tick(buy_high), "entry": entry,
        "sl": tick(sl), "st": tick(st), "mt": tick(mt),
        "rr": (st - entry) / risk,
        "st_pct": (st / cmp_ - 1) * 100, "mt_pct": (mt / cmp_ - 1) * 100,
        "sl_pct": (sl / cmp_ - 1) * 100,
    }


def rationale(f: dict, fa: dict) -> str:
    bits: list[str] = []
    c = criteria(f)
    if f["vol_ratio"] >= 1.2:
        bits.append(f"Volume {f['vol_ratio']:.1f}× 20d avg")
    if c["Above 50-EMA"] and c["Above 200-EMA"]:
        bits.append("above 50 & 200 EMA")
    elif c["Above 50-EMA"]:
        bits.append("above 50 EMA")
    bits.append(f"RSI {f['rsi']:.0f}")
    if f["close"] >= 0.97 * f["hi52"]:
        bits.append("near 52-wk high")
    elif f["close"] >= 0.98 * f["hi20"]:
        bits.append("20-day breakout zone")
    if fa["growth"] is not None and fa["growth"] > 0:
        bits.append(f"earnings/sales growth +{fa['growth'] * 100:.0f}%")
    if fa["de"] is not None and fa["de"] < 1.0 and not fa["financial"]:
        bits.append(f"D/E {fa['de']:.2f}")
    text = ", ".join(bits[:5])
    return text[0].upper() + text[1:]


# --------------------------------------------------------------------------- #
# Variety: pick history, rotation and fresh signals
# --------------------------------------------------------------------------- #
def load_pick_history(cfg: Config) -> dict[str, list[str]]:
    """{data_date: [symbols shown in the bracket tables]} from earlier runs."""
    p = cfg.data_dir / "pick_history.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def save_pick_history(cfg: Config, hist: dict[str, list[str]], data_date, symbols: list[str]) -> None:
    hist = dict(hist)
    hist[str(data_date)] = sorted(set(symbols))
    keep = sorted(hist)[-cfg.pick_history_days:]
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "pick_history.json").write_text(json.dumps({d: hist[d] for d in keep}))


def compute_streaks(hist: dict[str, list[str]], before: str) -> dict[str, int] | None:
    """Consecutive earlier sessions (most recent first) each symbol was on the list.
    Returns None when there is no earlier day, so first runs show no New / Day-N badges."""
    days = sorted((d for d in hist if d < before), reverse=True)
    if not days:
        return None
    alive = set(hist[days[0]])
    streaks = {s: 1 for s in alive}
    for d in days[1:]:
        alive &= set(hist[d])
        if not alive:
            break
        for s in alive:
            streaks[s] += 1
    return streaks


def rotation_penalty(sym: str, streaks: dict[str, int] | None, cfg: Config) -> float:
    if not streaks:
        return 0.0
    return cfg.rotation_pts * min(streaks.get(sym, 0), cfg.rotation_max_days)


def fresh_signal(f: dict) -> tuple[str, int] | None:
    """Event-type signals that change day to day (unlike EMA position). Returns (label, priority)."""
    c, e200 = f["close"], f["ema200"]
    if not (c > f["ema50"] and not math.isnan(e200) and c > e200):
        return None
    if f["rsi"] > 80:                      # already stretched
        return None
    vr = f["vol_ratio"]
    if c > f["prior_hi52"] and vr >= 1.2:
        return "New 52-week high", 3
    if c > f["prior_hi20"] and vr >= 1.5:
        return "20-day breakout on volume", 2
    if vr >= 2.5 and f["chg_pct"] >= 2.0:
        return "Volume surge (+2% or more)", 1
    return None


def build_bracket(feats: dict[str, dict], names: dict[str, str], cfg: Config,
                  fundamentals_fn, streaks: dict[str, int] | None = None) -> list[dict]:
    """Rank one bracket's eligible stocks and return the final rows."""
    cand = []
    for sym, f in feats.items():
        crit = criteria(f)
        # Minimum quality gate: never recommend a stock below its 50- or 200-EMA.
        # (also excludes names with <200 bars of history, where the 200-EMA is undefined)
        if not (crit["Above 50-EMA"] and crit["Above 200-EMA"]):
            continue
        ts = technical_score(f)
        cand.append({"sym": sym, "f": f, "ts": ts, "crit": crit,
                     "strict_tech": all(crit.values()),
                     "pen": rotation_penalty(sym, streaks, cfg)})
    cand.sort(key=lambda x: (x["strict_tech"], x["ts"]["total"] - x["pen"]), reverse=True)
    pool = cand[: cfg.fundamentals_pool]

    funds = fundamentals_fn([p["sym"] for p in pool])
    rows = []
    for p in pool:
        fa = fundamentals_assessment(funds.get(p["sym"]))
        total = p["ts"]["total"] + fa["score"]
        strict = p["strict_tech"] and not fa["red_flag"]
        missed = [k for k, ok in p["crit"].items() if not ok]
        if fa["red_flag"]:
            missed.append("Fundamental red flag")
        lv = compute_levels(p["f"])
        rows.append({
            "symbol": p["sym"], "name": names.get(p["sym"], p["sym"]),
            "cmp": p["f"]["close"], "chg_pct": p["f"]["chg_pct"],
            "score": round(total, 1), "rank_score": round(total - p["pen"], 1),
            "streak": None if streaks is None else streaks.get(p["sym"], 0),
            "strict": strict, "missed": missed,
            "rationale": rationale(p["f"], fa), "fund_known": fa["known"],
            **lv,
        })
    rows.sort(key=lambda r: (r["strict"], r["rank_score"]), reverse=True)
    rows = rows[: cfg.top_n]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def run_screen(histories: dict[str, pd.DataFrame], names: dict[str, str], cfg: Config,
               fundamentals_fn, today: datetime | None = None,
               streaks: dict[str, int] | None = None) -> dict:
    by_bracket: dict[str, dict[str, dict]] = {b[2]: {} for b in BRACKETS}
    fresh: list[dict] = []
    stats = {"universe": len(histories), "no_features": 0, "illiquid": 0, "anomaly": 0,
             "out_of_range": 0, "eligible": 0}
    latest = None
    for sym, df in histories.items():
        f = compute_features(df, cfg, today)
        if f is None:
            stats["no_features"] += 1
            continue
        latest = max(latest, f["date"]) if latest else f["date"]
        if (f["close"] < cfg.min_price or f["turnover_cr"] < cfg.min_turnover_cr
                or f["zero_vol_days"] > 2):
            stats["illiquid"] += 1
            continue
        if f["max_abs_ret60"] > 0.30:       # likely unadjusted split / bad tick
            stats["anomaly"] += 1
            continue
        placed = False
        for lo, hi, label in BRACKETS:
            if lo < f["close"] <= hi:
                by_bracket[label][sym] = f
                placed = True
                break
        if not placed:
            stats["out_of_range"] += 1
            continue
        stats["eligible"] += 1
        sig = fresh_signal(f)
        if sig:
            # Breakout levels measured from CMP (the pullback-based buy zone used in the tables is not
            # meaningful for a stock already breaking out): stop 1.5 ATR below, target 3 ATR above (2:1).
            c0, a0 = f["close"], f["atr"]
            sl = max(c0 - 1.5 * a0, c0 * 0.90, 0.05)
            st = c0 + 3.0 * a0
            fresh.append({"symbol": sym, "name": names.get(sym, sym), "cmp": c0,
                          "chg_pct": f["chg_pct"], "vol_ratio": f["vol_ratio"], "rsi": f["rsi"],
                          "signal": sig[0], "prio": sig[1], "sl": tick(sl), "sl_pct": (sl / c0 - 1) * 100,
                          "st": tick(st), "st_pct": (st / c0 - 1) * 100})

    brackets = []
    for lo, hi, label in BRACKETS:
        feats = by_bracket[label]
        rows = build_bracket(feats, names, cfg, fundamentals_fn, streaks) if feats else []
        brackets.append({"label": label, "lo": lo, "hi": hi, "eligible": len(feats), "rows": rows})
        log.info("%-14s eligible=%3d picked=%2d (full match=%d)", label, len(feats), len(rows),
                 sum(r["strict"] for r in rows))
    fresh.sort(key=lambda r: (r["prio"], r["vol_ratio"]), reverse=True)
    stats["fresh_total"] = len(fresh)
    return {"data_date": latest, "stats": stats, "brackets": brackets, "fresh": fresh[: cfg.fresh_top_n]}


# --------------------------------------------------------------------------- #
# HTML rendering (web dashboard + email share one template)
# --------------------------------------------------------------------------- #
DISCLAIMER = (
    "<strong>Disclaimer:</strong> This report is generated automatically from free, delayed "
    "market data and simple technical/fundamental rules. It is for <strong>educational and "
    "research purposes only</strong> and does not constitute investment advice, a "
    "recommendation, or an offer to buy or sell any security. The author is not a "
    "SEBI-registered investment adviser or research analyst. Data may be inaccurate or "
    "incomplete. Buy zones, targets and stop-losses are mechanical estimates, not guarantees. "
    "Equity investments are subject to market risk, including loss of capital. Verify all "
    "prices on your broker/exchange and consult a qualified financial adviser before acting."
)

CSS = """
body{margin:0;background:#f4f5f7;color:#1c2330;font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:20px 14px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 8px}
.sub{color:#5b6576;font-size:13px}
.banner{background:#fff;border:1px solid #dfe3ea;border-radius:8px;padding:10px 14px;margin:14px 0;font-size:13px}
.warn{border-left:4px solid #c0392b}.ok{border-left:4px solid #2e7d32}
.nav a{display:inline-block;margin:2px 4px 2px 0;padding:3px 9px;border:1px solid #cfd5df;border-radius:14px;color:#2b4a7a;text-decoration:none;font-size:12px;background:#fff}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #dfe3ea;font-size:13px}
th{background:#eef1f6;text-align:left;padding:7px 8px;font-size:12px;color:#3b4658;border-bottom:1px solid #dfe3ea;white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid #edf0f4;vertical-align:top}
td.n,th.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.nm a{color:#1c2330;font-weight:600;text-decoration:none}.tk{color:#6b7587;font-size:11px}
.sm{color:#6b7587;font-size:11px}.up{color:#1b7a3a}.dn{color:#b3261e}
.b{display:inline-block;padding:0 6px;border-radius:9px;font-size:10px;margin-left:4px}
.bf{background:#e3f4e8;color:#1b6b34}.bp{background:#fdf0d5;color:#8a5a00}
.bn{background:#dcecff;color:#0b4a8f}.bs{background:#eceff3;color:#5b6576}
.rat{color:#3b4658;min-width:230px}
.foot{margin-top:28px;padding:12px 14px;background:#fff;border:1px solid #dfe3ea;border-radius:8px;font-size:12px;color:#4a5466}
.meth{font-size:12px;color:#5b6576;margin-top:14px}
.scroll{overflow-x:auto}
"""


def _inr(x: float) -> str:
    return f"₹{x:,.2f}"


def _pct(x: float) -> str:
    return f"{x:+.1f}%"


def changes_banner(report: dict) -> str:
    rows = [r for b in report["brackets"] for r in b["rows"]]
    if not rows or all(r.get("streak") is None for r in rows):
        return ""                                   # first run: nothing to compare with
    new = sum(1 for r in rows if r.get("streak") == 0)
    return (f'<div class="banner">Price-bracket lists today: {len(rows)} picks, <strong>{new} new</strong> since the '
            f"previous session and {len(rows) - new} carried over. Stocks that stay on the list are ranked lower the longer "
            'they stay (see the "Day N" tags), so new names can come in.</div>')


def render_fresh_html(rows: list[dict] | None, stats: dict) -> str:
    head = ('<h2 id="fresh">Fresh signals today <span class="sm">· breakouts and volume surges - events that change '
            "day to day</span></h2>")
    if not rows:
        return head + ('<div class="banner">No breakout or volume-surge signals today among liquid stocks above their '
                       "50 &amp; 200 EMA.</div>")
    body = []
    for i, r in enumerate(rows, 1):
        url = f"https://www.screener.in/company/{quote(r['symbol'])}/"
        cls = "up" if r["chg_pct"] >= 0 else "dn"
        body.append(
            f'<tr><td class="n">{i}</td><td class="nm"><a href="{url}">{escape(r["name"])}</a>'
            f'<div class="tk">NSE: {escape(r["symbol"])}</div></td>'
            f'<td class="n">{_inr(r["cmp"])}</td><td class="n {cls}">{_pct(r["chg_pct"])}</td>'
            f'<td class="n">{r["vol_ratio"]:.1f}×</td><td class="n">{r["rsi"]:.0f}</td>'
            f'<td>{escape(r["signal"])}</td>'
            f'<td class="n dn">{_inr(r["sl"])}<div class="sm">{_pct(r["sl_pct"])}</div></td>'
            f'<td class="n up">{_inr(r["st"])}<div class="sm">{_pct(r["st_pct"])}</div></td></tr>')
    total = stats.get("fresh_total", len(rows))
    note = (f'<div class="meth">Showing the {len(rows)} strongest of {total} signals. Only liquid stocks priced up to ₹1,000 '
            "that are above both their 50 &amp; 200 EMA with RSI ≤ 80. Breakouts can reverse quickly: consider waiting for a "
            "pullback towards the breakout level and size positions small. Stop-loss = 1.5 × ATR below CMP (max 10%), target = "
            "3 × ATR above CMP (2:1 reward-to-risk). Mechanical estimates, not forecasts.</div>")
    return (head + '<div class="scroll"><table><tr><th class="n">#</th><th>Stock</th><th class="n">CMP</th>'
            '<th class="n">Day</th><th class="n">Volume</th><th class="n">RSI</th><th>Signal</th>'
            '<th class="n">Stop-loss</th><th class="n">Target 1–4 wk</th></tr>' + "".join(body) + "</table></div>" + note)


def render_fresh_text(rows: list[dict] | None) -> str:
    out = ["== Fresh signals today (breakouts / volume surges) =="]
    if not rows:
        out.append("None today.")
    for i, r in enumerate(rows or [], 1):
        out.append(f"{i:>2}. {r['symbol']:<12} CMP {r['cmp']:.2f} ({r['chg_pct']:+.1f}%) | vol {r['vol_ratio']:.1f}x"
                   f" | {r['signal']} | SL {r['sl']:.2f} | T1 {r['st']:.2f}")
    out.append("")
    return "\n".join(out)


def render_html(report: dict, cfg: Config, web: bool = True) -> str:
    gen = report["generated_at"]
    d = report["data_date"]
    mk = report.get("market") or {}
    st = report["stats"]

    if mk:
        trend_ok = mk.get("above_200") is not False and mk.get("above_50")
        txt = (f"Nifty 50 closed {mk['close']:,.0f} ({_pct(mk['chg_pct'])}) on {mk['date']}; "
               f"{'above' if mk['above_50'] else 'below'} its 50-DMA"
               + (f" and {'above' if mk['above_200'] else 'below'} its 200-DMA." if mk.get("above_200") is not None else "."))
        if not trend_ok:
            txt += " <strong>Broad-market trend is weak - consider smaller position sizes and tighter risk control.</strong>"
        market = f'<div class="banner {"ok" if trend_ok else "warn"}">{txt}</div>'
    else:
        market = ""

    nav = ""
    if web:
        nav = '<div class="nav">' + (desk.nav_links() if report.get("desk") else "")
        nav += ('<a href="#fresh">Fresh signals</a>' if "fresh" in report else "")
        if report.get("quality"):
            nav += '<a href="#quality">Quality shortlist</a>'
        nav += "".join(f'<a href="#b{i}">{escape(b["label"])}</a>' for i, b in enumerate(report["brackets"]))
        nav += "</div>"
    quality_html = quality.render_html_section(report["quality"]) if report.get("quality") else ""
    fresh_html = render_fresh_html(report.get("fresh"), st) if "fresh" in report else ""
    desk_html = desk.render_html(report["desk"]) if (desk and report.get("desk")) else ""

    sections = []
    for i, b in enumerate(report["brackets"]):
        head = (f'<h2 id="b{i}">{escape(b["label"])} <span class="sm">· {b["eligible"]} liquid stocks screened'
                f" · showing {len(b['rows'])}</span></h2>")
        if not b["rows"]:
            sections.append(head + '<div class="banner">No stock in this bracket passed the liquidity and '
                                   "trend filters today.</div>")
            continue
        rows_html = []
        for r in b["rows"]:
            badge = ('<span class="b bf">Full match</span>' if r["strict"] else
                     f'<span class="b bp" title="{escape(", ".join(r["missed"]))}">Partial</span>')
            miss = "" if r["strict"] else f'<div class="sm">Missed: {escape(", ".join(r["missed"]))}</div>'
            sk = r.get("streak")
            if sk is not None:
                badge += ('<span class="b bn">New</span>' if sk == 0
                          else f'<span class="b bs">Day {sk + 1}</span>')
            url = f"https://www.screener.in/company/{quote(r['symbol'])}/"
            chg_cls = "up" if r["chg_pct"] >= 0 else "dn"
            rows_html.append(
                f'<tr><td class="n">{r["rank"]}</td>'
                f'<td class="nm"><a href="{url}">{escape(r["name"])}</a>{badge}'
                f'<div class="tk">NSE: {escape(r["symbol"])} · score {r["score"]:.0f}/100</div></td>'
                f'<td class="n">{_inr(r["cmp"])}<div class="sm {chg_cls}">{_pct(r["chg_pct"])}</div></td>'
                f'<td class="n">{_inr(r["buy_low"])} – {_inr(r["buy_high"])}</td>'
                f'<td class="n up">{_inr(r["st"])}<div class="sm">{_pct(r["st_pct"])}</div></td>'
                f'<td class="n up">{_inr(r["mt"])}<div class="sm">{_pct(r["mt_pct"])}</div></td>'
                f'<td class="n dn">{_inr(r["sl"])}<div class="sm">{_pct(r["sl_pct"])}</div></td>'
                f'<td class="n">{r["rr"]:.1f}</td>'
                f'<td class="rat">{escape(r["rationale"])}{miss}</td></tr>')
        sections.append(
            head + '<div class="scroll"><table><tr><th class="n">#</th><th>Stock</th><th class="n">CMP</th>'
            '<th class="n">Buy zone</th><th class="n">Target 1–4 wk</th><th class="n">Target 1–3 mo</th>'
            '<th class="n">Stop-loss</th><th class="n">R:R</th><th>Key rationale</th></tr>'
            + "".join(rows_html) + "</table></div>")

    method = (
        "<div class=\"meth\"><strong>How it works.</strong> Universe: liquid NSE stocks "
        f"(20-day avg turnover ≥ ₹{cfg.min_turnover_cr:g} cr, price ≥ ₹{cfg.min_price:g}). "
        "Ranked by a 100-point score: trend vs 20/50/200 EMA (30), RSI 50–70 sweet spot (15), "
        "volume vs 20-day average (20), 20-day momentum &amp; closeness to 52-wk high (15), "
        "fundamentals - earnings growth, debt/equity, profitability (20). “Full match” = above 50 &amp; 200 EMA, "
        "RSI 50–70, volume above 20-day average and no fundamental red flag; “Partial” names are shown only when "
        "fewer than 10 full matches exist; they still sit above both EMAs but miss the RSI or volume rule "
        "(or carry a fundamental red flag). Stocks below their 50- or 200-EMA are never listed. Buy zone = nearest support "
        "(5-day pivot S1, 20/50 EMA, Fibonacci 38.2/50%, 20-day low) within 1.5×ATR of CMP. "
        "Stop = buy-zone low − 1×ATR (max 10% risk). Targets use pivot R1/R2, 60-day swing high and Fibonacci "
        "extensions, falling back to ATR multiples. R:R is computed to the 1–4 week target from the mid-point of the buy zone. "
        f"To keep the lists fresh, each consecutive day a stock has already been listed deducts {cfg.rotation_pts:g} score "
        f"points (up to {cfg.rotation_max_days} days) from its ranking score; the score shown is the raw score."
        "</div>")

    stats_line = (f"{st['universe']} symbols downloaded · {st['eligible']} passed liquidity/price filters "
                  f"({st['illiquid']} illiquid, {st['anomaly']} data anomalies skipped)")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>NSE Daily Trading Desk - {d}</title><style>{CSS}{desk.CSS if desk else ''}</style></head><body>"
        '<div class="wrap">'
        f"<h1>NSE Daily Trading Desk</h1>"
        f'<div class="sub">Based on closing prices of <strong>{d}</strong> (last completed session) · '
        f"generated {gen} · CMP = last close, not a live quote</div>"
        f"{market}{nav}{desk_html}{changes_banner(report)}{fresh_html}{quality_html}{''.join(sections)}{method}"
        f'<div class="sm" style="margin-top:8px">{stats_line}</div>'
        f'<div class="foot">{DISCLAIMER}</div></div></body></html>'
    )


def render_text(report: dict) -> str:
    out = [f"NSE Daily Trading Desk - data as of {report['data_date']}",
           "(Open the attached HTML for the full tables.)", ""]
    if desk and report.get("desk"):
        out.append(desk.render_text(report["desk"]))
    if "fresh" in report:
        out.append(render_fresh_text(report["fresh"]))
    if report.get("quality"):
        out.append(quality.render_text_section(report["quality"]))
    for b in report["brackets"]:
        out.append(f"== {b['label']} ==")
        for r in b["rows"]:
            out.append(f"{r['rank']:>2}. {r['symbol']:<12} CMP {r['cmp']:.2f} | buy {r['buy_low']:.2f}-{r['buy_high']:.2f}"
                       f" | T1 {r['st']:.2f} | T2 {r['mt']:.2f} | SL {r['sl']:.2f}")
        out.append("")
    out.append("For educational/research purposes only. Not investment advice.")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Output + email
# --------------------------------------------------------------------------- #
def write_outputs(report: dict, html: str, cfg: Config) -> None:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    arch = cfg.out_dir / "archive"
    arch.mkdir(exist_ok=True)
    (cfg.out_dir / "index.html").write_text(html, encoding="utf-8")
    (arch / f"{report['data_date']}.html").write_text(html, encoding="utf-8")
    slim = {**report, "data_date": str(report["data_date"])}
    (cfg.out_dir / "latest.json").write_text(json.dumps(slim, indent=1, default=str), encoding="utf-8")
    # prune old archive
    cutoff = datetime.now(IST).date() - timedelta(days=cfg.keep_archive_days)
    for p in arch.glob("*.html"):
        try:
            if datetime.strptime(p.stem, "%Y-%m-%d").date() < cutoff:
                p.unlink()
        except ValueError:
            pass
    idx = sorted((p.stem for p in arch.glob("*.html")), reverse=True)
    links = "".join(f'<li><a href="{s}.html">{s}</a></li>' for s in idx)
    (arch / "index.html").write_text(
        f'<!doctype html><meta charset="utf-8"><title>Archive</title><h1>Report archive</h1><ul>{links}</ul>'
        '<p><a href="../index.html">Latest report</a></p>', encoding="utf-8")


def send_email(report: dict, html: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user, pwd = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    to = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
    if not (user and pwd and to):
        raise RuntimeError("Email not configured: set SMTP_USER, SMTP_PASS and EMAIL_TO")
    msg = EmailMessage()
    msg["Subject"] = f"NSE Trading Desk - {report['data_date']} (Intraday + CNC picks)"
    msg["From"] = os.environ.get("EMAIL_FROM", user)
    msg["To"] = ", ".join(to)
    msg.set_content(render_text(report))
    msg.add_alternative(html, subtype="html")
    msg.add_attachment(html.encode("utf-8"), maintype="text", subtype="html",
                       filename=f"nse_screener_{report['data_date']}.html")
    size_kb = len(html.encode()) / 1024
    if size_kb > 95:
        log.warning("HTML is %.0f KB - Gmail clips messages above ~102 KB (attachment still has everything)", size_kb)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(user, pwd)
            s.send_message(msg)
    log.info("Email sent to %s", ", ".join(to))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-email", action="store_true", help="generate report but do not send email")
    ap.add_argument("--force", action="store_true", help="run even if no new trading session since last run")
    ap.add_argument("--no-quality", action="store_true", help="skip the 22-filter quality small-cap screen")
    ap.add_argument("--no-desk", action="store_true", help="skip the intraday watchlist, CNC shortlist and market pulse")
    ap.add_argument("--limit", type=int, default=0, help="debug: only screen the first N symbols")
    ap.add_argument("--universe", choices=["all", "total", "500"], default=os.environ.get("UNIVERSE", "all"),
                    help="all = every NSE EQ stock (default), total = Nifty Total Market (~750), 500 = Nifty 500")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = Config(universe=args.universe)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.min_turnover_cr = float(os.environ.get("MIN_TURNOVER_CR", cfg.min_turnover_cr))
    cfg.min_price = float(os.environ.get("MIN_PRICE", cfg.min_price))
    force = args.force or os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")

    uni = load_universe(cfg)
    if args.limit:
        uni = uni.head(args.limit)
    names = dict(zip(uni["symbol"], uni["name"]))

    hist = fetch_history(list(uni["symbol"]), cfg)
    if len(hist) < 20:
        log.error("Too little market data downloaded (%d symbols) - aborting", len(hist))
        return 2

    newest = max(pd.Timestamp(d.index[-1]).tz_localize(None) if pd.Timestamp(d.index[-1]).tzinfo
                 else pd.Timestamp(d.index[-1]) for d in hist.values())
    now = datetime.now(IST)
    if (now.date() - newest.date()).days > cfg.max_stale_days:
        log.error("Latest data is %s - too stale, aborting", newest.date())
        return 2

    state_path = cfg.data_dir / "last_run.json"
    last = json.loads(state_path.read_text()).get("data_date") if state_path.exists() else None
    if last == str(newest.date()) and not force:
        log.info("No new trading session since %s (market holiday?) - skipping", last)
        return 0

    picks_hist = load_pick_history(cfg)
    streaks = compute_streaks(picks_hist, str(newest.date()))
    report = run_screen(hist, names, cfg, lambda syms: fetch_fundamentals(syms, cfg), now, streaks)
    report["market"] = market_context(cfg)
    run_quality = not args.no_quality and os.environ.get("QUALITY_SCREEN", "1").lower() not in ("0", "false", "no")
    run_desk = not args.no_desk and os.environ.get("TRADING_DESK", "1").lower() not in ("0", "false", "no")
    if run_quality and quality is None:
        log.error("quality.py not found next to screener.py - skipping the quality shortlist")
        run_quality = False
    if run_desk and desk is None:
        log.error("desk.py not found next to screener.py - skipping intraday / CNC / market pulse")
        run_desk = False
    feats: dict[str, dict] = {}
    if run_quality or run_desk:
        for sym, df in hist.items():
            f = compute_features(df, cfg, now)
            if f is not None:
                feats[sym] = f
    if run_quality:
        try:   # a failure here must never stop the main report or the email
            qcfg = quality.QualityConfig(data_dir=cfg.data_dir)
            industries = dict(zip(uni["symbol"], uni["industry"].fillna("").astype(str)))
            report["quality"] = quality.screen(
                feats, names, industries, qcfg, compute_levels,
                lambda syms: quality.fetch_metrics(syms, qcfg))
            log.info("Quality screen: %d shortlisted, %d evaluated", len(report["quality"]["rows"]),
                     report["quality"]["stats"]["evaluated"])
        except Exception as exc:  # noqa: BLE001
            log.error("Quality screen failed (report continues without it): %s", exc)
    if run_desk:
        try:   # same rule: intraday / CNC extras are optional, the bracket report must still go out
            report["desk"] = desk.build_desk(feats, names, report, desk.DeskConfig.from_env())
            dk = report["desk"]
            log.info("Desk: %d long / %d short intraday, %d CNC ideas, %d symbols with news/results data",
                     len(dk["intraday"]["long"]), len(dk["intraday"]["short"]), len(dk["cnc"]["rows"]),
                     dk["events_found"])
        except Exception as exc:  # noqa: BLE001
            log.error("Trading desk failed (report continues without it): %s", exc)
    report["generated_at"] = now.strftime("%d %b %Y, %H:%M IST")
    html = render_html(report, cfg, web=True)
    write_outputs(report, html, cfg)
    state_path.write_text(json.dumps({"data_date": str(report["data_date"])}))
    save_pick_history(cfg, picks_hist, report["data_date"],
                      [r["symbol"] for b in report["brackets"] for r in b["rows"]])
    log.info("Report written to %s", cfg.out_dir / "index.html")

    if args.no_email:
        return 0
    rc = 0
    try:
        send_email(report, render_html(report, cfg, web=False))
    except Exception as exc:  # noqa: BLE001
        log.error("Email failed: %s", exc)
        rc = 1
    if notify_telegram and notify_telegram.configured():
        try:   # Telegram is independent of email: either can fail without stopping the other
            pages = os.environ.get("PAGES_URL", "").strip()
            if not notify_telegram.send_report(report, render_html(report, cfg, web=False), pages):
                rc = 1
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram failed: %s", exc)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

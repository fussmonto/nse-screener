#!/usr/bin/env python3
"""
Quality small-cap screen (22 filters).

Runs after the daily price-bracket screen and adds a "Quality small-cap shortlist" to the
dashboard and the email.  It is built for LONG-HORIZON, equal-weight, rules-based research - not
for short-term trading.

Pipeline
  1. Cheap pre-screen from price history already downloaded: liquidity + trend (filters 2, 16).
  2. For the survivors, fetch fundamentals from Yahoo Finance (income statement, balance sheet,
     cash flow, key ratios).  Results are cached in data/quality_cache.json, so coverage grows on
     every run even when the time budget ends before every stock has been fetched.
  3. Evaluate 22 filters per stock: 16 automated "hard" filters, 5 automated "soft" checks and
     1 manual check (promoter pledging - not available in free data).
  4. Shortlist = every hard filter passes (at most N unknown) and at most 2 soft checks fail.
     Ranked by a transparent quality score, max 2 stocks per sector.
  5. Banks / NBFCs / insurers are excluded: ROCE, debt/equity and interest cover mean something
     different for lenders.

Educational / research use only - not investment advice.
"""
from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("screener.quality")
LOSS_PE = 999.0  # sentinel stored instead of infinity so the report stays valid JSON


# --------------------------------------------------------------------------- #
# Configuration - every threshold is editable here
# --------------------------------------------------------------------------- #
@dataclass
class QualityConfig:
    # 1-2: size and tradability
    mcap_min_cr: float = 300.0
    mcap_max_cr: float = 8000.0
    min_turnover_cr: float = 1.0        # 20-day average traded value, ₹ crore
    min_price: float = 10.0
    # 3-5: profitability (percent)
    roce_min: float = 18.0
    roe_min: float = 15.0
    op_margin_min: float = 12.0
    # 6-8: growth (percent, 3-year CAGR)
    sales_cagr_min: float = 15.0
    profit_cagr_min: float = 15.0
    # 9-12: balance sheet and cash
    de_max: float = 0.5
    int_cov_min: float = 4.0
    # 13-14: ownership (percent)
    promoter_min: float = 40.0
    inst_min: float = 5.0
    # 15-17: valuation and trend
    pe_max: float = 25.0
    peg_max: float = 1.5
    # 18-22: soft checks
    cfo_pat_min: float = 70.0           # 3-year operating cash flow as % of net profit
    fcf_pos_years_min: int = 2          # of the last 3 years
    rec_days_jump: float = 1.25         # receivable days may rise at most 25% ...
    rec_days_abs: float = 10.0          # ... unless the rise is under this many days
    dilution_max: float = 5.0           # % rise in share count over 3 years
    roce_avg_min: float = 15.0          # average of the 3-4 years Yahoo provides
    # shortlist rules
    max_unknown_hard: int = 2           # hard filters whose data is missing
    max_soft_fails: int = 2
    top_n: int = 15
    sector_cap: int = 2
    misses_n: int = 10
    # fetching
    cache_ttl_days: int = 10
    budget_s: int = 420
    workers: int = 4
    data_dir: Path = Path("data")


SHORT = {
    1: "market cap", 2: "liquidity", 3: "ROCE", 4: "ROE", 5: "operating margin",
    6: "sales growth", 7: "profit growth", 8: "latest-quarter profit", 9: "debt/equity",
    10: "interest cover", 11: "promoter pledging", 12: "operating cash flow",
    13: "promoter holding", 14: "institutional holding", 15: "P/E", 16: "200-DMA trend",
    17: "PEG", 18: "cash conversion", 19: "free cash flow", 20: "receivable days",
    21: "share dilution", 22: "multi-year ROCE",
}


def filter_labels(c: QualityConfig) -> dict[int, str]:
    return {
        1: f"Market cap ₹{c.mcap_min_cr:,.0f}–{c.mcap_max_cr:,.0f} cr",
        2: f"Avg daily traded value > ₹{c.min_turnover_cr:g} cr",
        3: f"ROCE > {c.roce_min:g}%",
        4: f"ROE > {c.roe_min:g}%",
        5: f"Operating margin > {c.op_margin_min:g}%",
        6: f"Sales growth 3Y CAGR > {c.sales_cagr_min:g}%",
        7: f"Profit growth 3Y CAGR > {c.profit_cagr_min:g}%",
        8: "Latest-quarter profit growth > 0% YoY",
        9: f"Debt/equity < {c.de_max:g}",
        10: f"Interest cover > {c.int_cov_min:g}×",
        11: "Promoter pledging < 5% (manual check)",
        12: "Operating cash flow positive last year",
        13: f"Promoter holding > {c.promoter_min:g}%",
        14: f"FII + DII holding > {c.inst_min:g}%",
        15: f"P/E < {c.pe_max:g}",
        16: "Price above 200-day average",
        17: f"PEG < {c.peg_max:g}",
        18: f"Cash conversion ≥ {c.cfo_pat_min:g}% of profit (3Y)",
        19: f"Free cash flow positive in ≥ {c.fcf_pos_years_min} of last 3 years",
        20: "Receivable days not rising sharply",
        21: f"Share count up < {c.dilution_max:g}% over 3 years",
        22: f"Average ROCE (3-4Y) > {c.roce_avg_min:g}%",
    }


HARD = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 17]
SOFT = [18, 19, 20, 21, 22]
MANUAL = [11]


# --------------------------------------------------------------------------- #
# Metric extraction (pure functions - unit-tested offline)
# --------------------------------------------------------------------------- #
def _f(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(x) or math.isinf(x)) else x


def _series(df, *labels) -> list:
    """Values newest -> oldest for the first matching row label of a yfinance statement."""
    if df is None or getattr(df, "empty", True):
        return []
    idx = {str(i).strip().lower(): i for i in df.index}
    for lab in labels:
        key = idx.get(lab.lower())
        if key is None:
            continue
        row = df.loc[key]
        try:
            cols = sorted(df.columns, reverse=True)
        except TypeError:
            cols = list(df.columns)
        return [_f(row[c]) for c in cols]
    return []


def _at(lst: list, i: int):
    return lst[i] if len(lst) > i else None


def _cagr(series: list, years: int = 3):
    if len(series) <= years:
        return None
    new, old = series[0], series[years]
    if new is None or old is None or new <= 0 or old <= 0:
        return None
    return ((new / old) ** (1 / years) - 1) * 100


def _ratio(a, b, scale: float = 1.0):
    if a is None or b is None or b == 0:
        return None
    return a / b * scale


def extract_metrics(info: dict | None, inc, bs, cf) -> dict:
    """Turn Yahoo info + annual statements into the numbers the filters need."""
    info = info or {}
    rev = _series(inc, "Total Revenue", "Operating Revenue")
    ni = _series(inc, "Net Income", "Net Income Common Stockholders")
    ebit = _series(inc, "EBIT", "Operating Income")
    intexp = _series(inc, "Interest Expense")
    ta = _series(bs, "Total Assets")
    cl = _series(bs, "Current Liabilities", "Total Current Liabilities")
    debt = _series(bs, "Total Debt")
    equity = _series(bs, "Stockholders Equity", "Common Stock Equity")
    recv = _series(bs, "Accounts Receivable", "Receivables", "Gross Accounts Receivable")
    shares = [s for s in _series(bs, "Ordinary Shares Number", "Share Issued") if s is not None]
    cfo = _series(cf, "Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
    fcf = _series(cf, "Free Cash Flow")
    capex = _series(cf, "Capital Expenditure")
    if not fcf and cfo and capex:
        fcf = [None if (a is None or b is None) else a + b for a, b in zip(cfo, capex)]

    roce = []
    for i in range(max(len(ebit), len(ta), len(cl))):
        e, a, c = _at(ebit, i), _at(ta, i), _at(cl, i)
        ce = None if (a is None or c is None) else a - c
        roce.append(_ratio(e, ce, 100) if (ce is not None and ce > 0) else None)
    roce_known = [r for r in roce if r is not None]

    de = _f(info.get("debtToEquity"))
    de = de / 100 if de is not None else _ratio(_at(debt, 0), _at(equity, 0))
    debt0, ebit0 = _at(debt, 0), _at(ebit, 0)
    ie = _at(intexp, 0)
    ie = abs(ie) if ie is not None else None
    if ebit0 is None:
        int_cov = None
    elif ie is None or ie == 0:
        int_cov = 99.0 if ((de is not None and de < 0.05) or debt0 == 0) else None
    else:
        int_cov = min(ebit0 / ie, 99.0)

    roe = _f(info.get("returnOnEquity"))
    roe = roe * 100 if roe is not None else _ratio(_at(ni, 0), _at(equity, 0), 100)
    opm = _f(info.get("operatingMargins"))
    opm = opm * 100 if opm is not None else _ratio(ebit0, _at(rev, 0), 100)
    qg = _f(info.get("earningsQuarterlyGrowth"))
    promoter = _f(info.get("heldPercentInsiders"))
    inst = _f(info.get("heldPercentInstitutions"))
    mc = _f(info.get("marketCap"))

    cfo3, ni3 = [x for x in cfo[:3] if x is not None], [x for x in ni[:3] if x is not None]
    cfo_pat = (sum(cfo3) / sum(ni3) * 100) if (len(cfo3) == 3 and len(ni3) == 3 and sum(ni3) > 0) else None
    fcf3 = [x for x in fcf[:3] if x is not None]

    rec_days = [_ratio(_at(recv, i), _at(rev, i), 365) for i in (0, 1)]
    dil = None
    if len(shares) >= 3:
        j = min(3, len(shares) - 1)
        dil = (shares[0] / shares[j] - 1) * 100 if shares[j] else None

    return {
        "sector": info.get("sector") or "",
        "mcap_cr": None if mc is None else mc / 1e7,
        "shares": _f(info.get("sharesOutstanding")),
        "eps": _f(info.get("trailingEps")),
        "peg_info": _f(info.get("trailingPegRatio")) or _f(info.get("pegRatio")),
        "roe": roe, "op_margin": opm,
        "qtr_growth": None if qg is None else qg * 100,
        "de": de, "int_cov": int_cov,
        "promoter": None if promoter is None else promoter * 100,
        "inst": None if inst is None else inst * 100,
        "roce_latest": _at(roce, 0),
        "roce_avg": (sum(roce_known[:4]) / len(roce_known[:4])) if len(roce_known) >= 3 else None,
        "sales_cagr3": _cagr(rev), "profit_cagr3": _cagr(ni),
        "cfo_latest": _at(cfo, 0), "cfo_pat": cfo_pat,
        "fcf_pos": sum(1 for x in fcf3 if x > 0) if len(fcf3) >= 2 else None,
        "fcf_n": len(fcf3),
        "rec_days": rec_days,
        "dilution3": dil,
        "years": len(rev),
    }


# --------------------------------------------------------------------------- #
# Fetching with cache + time budget
# --------------------------------------------------------------------------- #
def fetch_metrics(symbols: list[str], cfg: QualityConfig) -> dict[str, dict]:
    path = cfg.data_dir / "quality_cache.json"
    cache: dict[str, dict] = {}
    if path.exists():
        try:
            cache = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            cache = {}
    now = time.time()
    ttl = cfg.cache_ttl_days * 86400
    # never-fetched names first, then the stalest
    todo = sorted((s for s in symbols if now - cache.get(s, {}).get("_ts", 0) > ttl),
                  key=lambda s: cache.get(s, {}).get("_ts", 0))
    log.info("Quality fundamentals: %d cached, %d to fetch (budget %ds)",
             len(symbols) - len(todo), len(todo), cfg.budget_s)
    deadline = now + cfg.budget_s

    def one(sym: str):
        if time.time() > deadline:
            return sym, None
        try:
            import yfinance as yf

            t = yf.Ticker(f"{sym}.NS")
            info = t.get_info() or {}
            if not info:
                return sym, None
            mc = _f(info.get("marketCap"))
            far_outside = mc is not None and not (
                cfg.mcap_min_cr * 0.7 <= mc / 1e7 <= cfg.mcap_max_cr * 1.5)
            if far_outside:      # saves three network calls for large/tiny companies
                rec = extract_metrics(info, None, None, None)
            else:
                rec = extract_metrics(info, t.income_stmt, t.balance_sheet, t.cashflow)
            rec["_ts"] = time.time()
            time.sleep(0.2)
            return sym, rec
        except Exception as exc:  # noqa: BLE001
            log.debug("quality fetch %s failed: %s", sym, exc)
            return sym, None

    fetched = 0
    if todo:
        with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
            for sym, rec in ex.map(one, todo):
                if rec:
                    cache[sym] = rec
                    fetched += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache))
    log.info("Quality fundamentals: fetched %d of %d", fetched, len(todo))
    return {s: cache[s] for s in symbols if s in cache}


# --------------------------------------------------------------------------- #
# Filter evaluation
# --------------------------------------------------------------------------- #
def _chk(v, test):
    return None if v is None else bool(test(v))


def evaluate(m: dict, f: dict, cfg: QualityConfig) -> dict:
    """Return per-filter results plus counts.  ok is True / False / None (= no data)."""
    close = f["close"]
    shares, eps = m.get("shares"), m.get("eps")
    mcap = shares * close / 1e7 if shares else m.get("mcap_cr")
    if eps is None:
        pe = None
    elif eps <= 0:
        pe = LOSS_PE
    else:
        pe = close / eps
    pg = m.get("profit_cagr3")
    peg = pe / pg if (pe is not None and pe < LOSS_PE and pg and pg > 0) else None
    if peg is None and pe is not None and pe < LOSS_PE:
        pi = m.get("peg_info")
        peg = pi if (pi is not None and pi > 0) else None
    sma = f.get("sma200")
    sma = None if (sma is None or (isinstance(sma, float) and math.isnan(sma))) else sma
    rd = m.get("rec_days") or [None, None]
    rec_ok = None
    if len(rd) >= 2 and rd[0] is not None and rd[1] is not None:
        rec_ok = rd[0] <= rd[1] * cfg.rec_days_jump or (rd[0] - rd[1]) <= cfg.rec_days_abs

    ok = {
        1: _chk(mcap, lambda v: cfg.mcap_min_cr <= v <= cfg.mcap_max_cr),
        2: bool(f["turnover_cr"] > cfg.min_turnover_cr),
        3: _chk(m.get("roce_latest"), lambda v: v > cfg.roce_min),
        4: _chk(m.get("roe"), lambda v: v > cfg.roe_min),
        5: _chk(m.get("op_margin"), lambda v: v > cfg.op_margin_min),
        6: _chk(m.get("sales_cagr3"), lambda v: v > cfg.sales_cagr_min),
        7: _chk(m.get("profit_cagr3"), lambda v: v > cfg.profit_cagr_min),
        8: _chk(m.get("qtr_growth"), lambda v: v > 0),
        9: _chk(m.get("de"), lambda v: v < cfg.de_max),
        10: _chk(m.get("int_cov"), lambda v: v > cfg.int_cov_min),
        11: None,
        12: _chk(m.get("cfo_latest"), lambda v: v > 0),
        13: _chk(m.get("promoter"), lambda v: v > cfg.promoter_min),
        14: _chk(m.get("inst"), lambda v: v > cfg.inst_min),
        15: _chk(pe, lambda v: v < cfg.pe_max),
        16: None if sma is None else bool(close > sma),
        17: _chk(peg, lambda v: v < cfg.peg_max),
        18: _chk(m.get("cfo_pat"), lambda v: v >= cfg.cfo_pat_min),
        19: _chk(m.get("fcf_pos"), lambda v: v >= cfg.fcf_pos_years_min),
        20: rec_ok,
        21: _chk(m.get("dilution3"), lambda v: v < cfg.dilution_max),
        22: _chk(m.get("roce_avg"), lambda v: v > cfg.roce_avg_min),
    }
    hard_fail = [n for n in HARD if ok[n] is False]
    hard_unk = [n for n in HARD if ok[n] is None]
    soft_fail = [n for n in SOFT if ok[n] is False]
    soft_unk = [n for n in SOFT if ok[n] is None]
    return {
        "ok": ok, "hard_fail": hard_fail, "hard_unknown": hard_unk,
        "soft_fail": soft_fail, "soft_unknown": soft_unk,
        "vals": {"mcap": mcap, "pe": pe, "peg": peg},
    }


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def quality_score(m: dict, vals: dict, cfg: QualityConfig) -> float:
    """0-100: ROCE 35, sales growth 25, profit growth 25, cheapness 15 (transparent, not predictive)."""
    roce = m.get("roce_latest") or 0.0
    sg = m.get("sales_cagr3") or 0.0
    pg = m.get("profit_cagr3") or 0.0
    pe = vals.get("pe")
    cheap = 0.5 if (pe is None or pe >= LOSS_PE) else _clip(1 - pe / cfg.pe_max)
    return 100 * (0.35 * _clip(roce / 50) + 0.25 * _clip(sg / 40)
                  + 0.25 * _clip(pg / 50) + 0.15 * cheap)


# --------------------------------------------------------------------------- #
# Screen
# --------------------------------------------------------------------------- #
def screen(feats: dict[str, dict], names: dict[str, str], industries: dict[str, str],
           cfg: QualityConfig, levels_fn, fetch) -> dict:
    stats = {"universe": len(feats), "illiquid": 0, "anomaly": 0, "below_trend": 0,
             "no_data": 0, "financials": 0, "evaluated": 0, "sector_capped": 0}
    cands = []
    for sym, f in feats.items():
        if (f["close"] < cfg.min_price or f["turnover_cr"] <= cfg.min_turnover_cr
                or f["zero_vol_days"] > 2):
            stats["illiquid"] += 1
        elif f["max_abs_ret60"] > 0.30:      # likely unadjusted split / bad tick
            stats["anomaly"] += 1
        else:
            sma = f.get("sma200")
            if sma is None or (isinstance(sma, float) and math.isnan(sma)) or f["close"] <= sma:
                stats["below_trend"] += 1
            else:
                cands.append(sym)
    cands.sort(key=lambda s: -feats[s]["turnover_cr"])
    metrics = fetch(cands) if cands else {}

    scored = []
    for s in cands:
        m = metrics.get(s)
        if not m:
            stats["no_data"] += 1
            continue
        sector = m.get("sector") or industries.get(s) or "Unknown"
        if "financial" in sector.lower() or "financial" in str(industries.get(s, "")).lower():
            stats["financials"] += 1
            continue
        stats["evaluated"] += 1
        ev = evaluate(m, feats[s], cfg)
        lv = levels_fn(feats[s])
        vals = ev["vals"]
        pe = vals["pe"]
        scored.append({
            "symbol": s, "name": names.get(s, s), "sector": sector,
            "cmp": feats[s]["close"],
            "score": round(quality_score(m, vals, cfg), 1),
            "hard_fail": ev["hard_fail"], "hard_unknown": ev["hard_unknown"],
            "soft_fail": ev["soft_fail"], "soft_unknown": ev["soft_unknown"],
            "buy_low": lv["buy_low"], "buy_high": lv["buy_high"], "sl": lv["sl"],
            "roce": m.get("roce_latest"), "roe": m.get("roe"),
            "sales3": m.get("sales_cagr3"), "profit3": m.get("profit_cagr3"),
            "de": m.get("de"), "pe": None if pe is None or pe >= LOSS_PE else pe,
            "loss_making": bool(pe is not None and pe >= LOSS_PE),
            "mcap": vals["mcap"],
        })

    shortlist = [r for r in scored
                 if not r["hard_fail"] and len(r["hard_unknown"]) <= cfg.max_unknown_hard
                 and len(r["soft_fail"]) <= cfg.max_soft_fails]
    shortlist.sort(key=lambda r: (len(r["soft_fail"]), len(r["hard_unknown"]), -r["score"]))
    rows, per_sector = [], {}
    for r in shortlist:
        if per_sector.get(r["sector"], 0) >= cfg.sector_cap:
            stats["sector_capped"] += 1
            continue
        per_sector[r["sector"]] = per_sector.get(r["sector"], 0) + 1
        r = dict(r)
        r["tier"] = "Full" if (not r["soft_fail"] and not r["hard_unknown"]) else "Near"
        rows.append(r)
        if len(rows) >= cfg.top_n:
            break
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    shown = {r["symbol"] for r in rows}
    misses = [r for r in scored if r["symbol"] not in shown and len(r["hard_fail"]) <= 2
              and r["symbol"] not in {x["symbol"] for x in shortlist}]
    misses.sort(key=lambda r: (len(r["hard_fail"]) + len(r["soft_fail"]),
                               len(r["hard_unknown"]), -r["score"]))
    misses = misses[: cfg.misses_n]

    labels = filter_labels(cfg)
    return {
        "stats": stats, "n_candidates": len(cands), "n_with_data": len(cands) - stats["no_data"],
        "rows": rows, "misses": misses,
        "filters": [[n, "hard" if n in HARD else "soft" if n in SOFT else "manual", labels[n]]
                    for n in range(1, 23)],
        "short": SHORT,
    }


# --------------------------------------------------------------------------- #
# Rendering (HTML section shared by dashboard + email; plain-text for the text part)
# --------------------------------------------------------------------------- #
def _n(x, d: int = 1, suf: str = "") -> str:
    return "–" if x is None else f"{x:,.{d}f}{suf}"


def _flags(r: dict) -> str:
    short = {int(k): v for k, v in (r.get("_short") or SHORT).items()}
    bits = []
    if r.get("loss_making"):
        bits.append("loss-making (P/E n/a)")
    if r["soft_fail"]:
        bits.append("Check: " + ", ".join(short[n] for n in r["soft_fail"]))
    unk = r["hard_unknown"] + r["soft_unknown"]
    if unk:
        bits.append("No data: " + ", ".join(short[n] for n in unk))
    return "; ".join(bits)


def _miss_text(r: dict, short: dict) -> str:
    out = []
    if r["hard_fail"]:
        out.append("fails " + ", ".join(short[n] for n in r["hard_fail"]))
    if r["soft_fail"]:
        out.append("check " + ", ".join(short[n] for n in r["soft_fail"]))
    return "; ".join(out)


def render_html_section(q: dict) -> str:
    short = {int(k): v for k, v in q.get("short", SHORT).items()}
    st = q["stats"]
    cover = ""
    if q["n_with_data"] < q["n_candidates"]:
        cover = (f" Fundamentals are loaded for {q['n_with_data']} of {q['n_candidates']} stocks that passed the "
                 "liquidity/trend pre-screen; the rest fill in over the next daily runs.")
    head = (f'<h2 id="quality">Quality small-cap shortlist <span class="sm">· 22 filters · '
            f"{len(q['rows'])} stocks</span></h2>")
    intro = (f'<div class="banner">Long-horizon, rules-based screen (not a trading signal). '
             f"{st['evaluated']} stocks evaluated after the liquidity/trend pre-screen.{escape(cover)} "
             "Banks/NBFCs are excluded. Prices are last-session closes.</div>")
    if not q["rows"]:
        body = '<div class="banner">No stock passed all hard filters today.</div>'
    else:
        rows = []
        for r in q["rows"]:
            badge = ('<span class="b bf">Full match</span>' if r["tier"] == "Full"
                     else '<span class="b bp">Near match</span>')
            r = dict(r, _short=short)
            url = f"https://www.screener.in/company/{quote(r['symbol'])}/"
            flags = _flags(r)
            rows.append(
                f'<tr><td class="n">{r["rank"]}</td>'
                f'<td class="nm"><a href="{url}">{escape(r["name"])}</a>{badge}'
                f'<div class="tk">NSE: {escape(r["symbol"])} · {escape(r["sector"])} · score {r["score"]:.0f}/100</div></td>'
                f'<td class="n">₹{r["cmp"]:,.2f}<div class="sm">mcap ₹{_n(r["mcap"], 0)} cr</div></td>'
                f'<td class="n">₹{r["buy_low"]:,.2f} – ₹{r["buy_high"]:,.2f}<div class="sm dn">stop ₹{r["sl"]:,.2f}</div></td>'
                f'<td class="n">{_n(r["roce"], 0, "%")}</td><td class="n">{_n(r["roe"], 0, "%")}</td>'
                f'<td class="n">{_n(r["sales3"], 0, "%")}</td><td class="n">{_n(r["profit3"], 0, "%")}</td>'
                f'<td class="n">{_n(r["de"], 2)}</td><td class="n">{_n(r["pe"], 1)}</td>'
                f'<td class="rat">{escape(flags) if flags else "All 21 automated checks passed"}</td></tr>')
        body = ('<div class="scroll"><table><tr><th class="n">#</th><th>Stock</th><th class="n">CMP</th>'
                '<th class="n">Buy zone</th><th class="n">ROCE</th><th class="n">ROE</th>'
                '<th class="n">Sales 3Y</th><th class="n">Profit 3Y</th><th class="n">D/E</th>'
                '<th class="n">P/E</th><th>Notes</th></tr>' + "".join(rows) + "</table></div>")
    miss_html = ""
    if q["misses"]:
        items = "".join(
            f"<li>{escape(r['name'])} ({escape(r['symbol'])}) - {escape(_miss_text(r, short))}</li>"
            for r in q["misses"])
        miss_html = (f'<div class="meth"><strong>Closest misses</strong> (fail at most two hard filters):'
                     f"<ul>{items}</ul></div>")
    filt = " · ".join(f"{n}. {escape(label)}" for n, _t, label in q["filters"])
    meth = (f'<div class="meth"><strong>The 22 filters.</strong> {filt}. '
            "Shortlist = every automated hard filter passes (up to 2 may lack data) and at most 2 soft checks "
            "(18-22) fail; ranked by a quality score; max 2 per sector. "
            "Yahoo provides 3-4 years of statements, so \"3Y\" growth and \"multi-year ROCE\" use that window. "
            "Promoter holding and institutional holding are Yahoo approximations. A bonus issue or split can "
            "trigger a false \"share dilution\" flag.</div>"
            '<div class="meth"><strong>Before acting - check manually:</strong> promoter pledging (filter 11, '
            "not in free data), auditor changes or qualified opinions, related-party dealings, and the last two "
            "earnings-call summaries. This screen is designed for equal-weight, long-horizon holding "
            "(about 10-12 names, no more than 2 per sector), reviewed about once a year.</div>")
    return head + intro + body + miss_html + meth


def render_text_section(q: dict) -> str:
    short = {int(k): v for k, v in q.get("short", SHORT).items()}
    out = ["== Quality small-cap shortlist (22 filters, long-horizon) =="]
    if q["n_with_data"] < q["n_candidates"]:
        out.append(f"(Fundamentals loaded for {q['n_with_data']} of {q['n_candidates']} candidates; "
                   "coverage grows each run.)")
    if not q["rows"]:
        out.append("No stock passed all hard filters today.")
    for r in q["rows"]:
        out.append(f"{r['rank']:>2}. {r['symbol']:<12} CMP {r['cmp']:.2f} | buy {r['buy_low']:.2f}-{r['buy_high']:.2f}"
                   f" | stop {r['sl']:.2f} | ROCE {_n(r['roce'], 0)}% ROE {_n(r['roe'], 0)}%"
                   f" Sales3Y {_n(r['sales3'], 0)}% Profit3Y {_n(r['profit3'], 0)}% P/E {_n(r['pe'], 1)}"
                   f" [{r['tier']}]")
        flags = _flags(dict(r, _short=short))
        if flags:
            out.append(f"      {flags}")
    out.append("Check manually: promoter pledging, auditor changes, related-party dealings, concall summaries.")
    out.append("")
    return "\n".join(out)

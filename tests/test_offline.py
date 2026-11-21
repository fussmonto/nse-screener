#!/usr/bin/env python3
"""Offline tests: synthetic prices, no network. Run:  python tests/test_offline.py"""
import json
import math
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import screener as S  # noqa: E402
import quality as Q  # noqa: E402
import desk as D  # noqa: E402

# main() also builds the trading desk; keep every test offline (no market-pulse / news downloads)
D._real_fetch_events = D.fetch_events      # desk tests exercise the real one
D.fetch_pulse = lambda: []
D.fetch_events = lambda syms, ref, dc, now_utc=None: {}
import radar as R  # noqa: E402  (watchlist + headlines; headlines would hit the network)
R._real_fetch_news = R.fetch_news           # test_radar.py exercises the real one
R.fetch_news = lambda *a, **k: []

NOW = datetime(2026, 11, 13, 8, 30, tzinfo=S.IST)
DATES = pd.bdate_range(end="2026-11-12", periods=260)
RNG = np.random.default_rng(7)


def make_df(start, drift, vol_sigma=0.015, base_vol=2_000_000, vol_spike=1.0, dead=False, split=False):
    n = len(DATES)
    rets = RNG.normal(drift, vol_sigma, n)
    if split:
        rets[-20] = -0.5
    close = start * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(RNG.normal(0.006, 0.004, n)))
    low = close * (1 - np.abs(RNG.normal(0.006, 0.004, n)))
    vol = RNG.integers(int(base_vol * 0.8), int(base_vol * 1.2), n).astype(float)
    vol[-1] *= vol_spike
    if dead:
        vol[-15:] = 0
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol}, index=DATES)


def build_universe():
    hist, names = {}, {}
    # trending-up stocks across all price brackets, 6 per bracket
    for b in range(10):
        for k in range(6):
            end_price = b * 100 + 20 + k * 12
            drift = 0.0012 + 0.0002 * k
            df = make_df(end_price / math.exp(drift * 260) * 0.95, drift, vol_spike=1.0 + 0.3 * k)
            sym = f"UP{b}X{k}"
            hist[sym] = df
            names[sym] = f"Uptrend Co {b}-{k} Ltd"
    # downtrend names (must never be recommended)
    for k in range(5):
        sym = f"DN{k}"
        hist[sym] = make_df(260.0, -0.002)
        names[sym] = f"Downtrend {k}"
    # illiquid, penny, split-anomaly, short history, above-range
    hist["ILLIQ"] = make_df(150.0, 0.001, base_vol=800)
    names["ILLIQ"] = "Illiquid"
    hist["PENNY"] = make_df(5.0, 0.001)
    names["PENNY"] = "Penny"
    hist["SPLIT"] = make_df(150.0, 0.001, split=True)
    names["SPLIT"] = "Split"
    hist["SHORT"] = make_df(150.0, 0.001).tail(60)
    names["SHORT"] = "Short"
    hist["BIG"] = make_df(3500.0, 0.001)
    names["BIG"] = "Above range"
    hist["DEAD"] = make_df(250.0, 0.001, dead=True)
    names["DEAD"] = "Zero volume"
    return hist, names


def fake_funds(syms):
    out = {}
    for i, s in enumerate(syms):
        out[s] = {"debtToEquity": 30 + 20 * (i % 6), "earningsQuarterlyGrowth": 0.2 if i % 2 == 0 else -0.05,
                  "revenueGrowth": 0.1, "trailingEps": 5.0, "returnOnEquity": 0.15, "sector": "Industrials"}
    return out


def test_indicators():
    c = pd.Series(np.linspace(100, 200, 100))
    assert S.rsi_wilder(c).iloc[-1] == 100.0, "monotonic rise => RSI 100"
    flat = pd.Series([100.0] * 60)
    r = S.rsi_wilder(flat).iloc[-1]
    assert math.isnan(r) or 0 <= r <= 100
    h, l = c + 1, c - 1
    atr = S.atr_wilder(h, l, c).iloc[-1]
    assert 1.9 < atr < 2.6, atr           # range 2 + drift ~1
    assert abs(S.ema(c, 10).iloc[-1] - c.iloc[-1]) < 10
    assert S.tick(101.237) == 101.25 and S.tick(100.0) == 100.0
    print("ok  indicators")


def test_brackets_and_screen():
    cfg = S.Config()
    hist, names = build_universe()
    rep = S.run_screen(hist, names, cfg, fake_funds, NOW)
    st = rep["stats"]
    assert len(rep["brackets"]) == 10
    assert st["illiquid"] >= 3, st          # ILLIQ, PENNY, DEAD
    assert st["anomaly"] >= 1, st           # SPLIT
    assert st["no_features"] >= 1, st       # SHORT
    all_syms = []
    for b in rep["brackets"]:
        assert len(b["rows"]) <= 10
        for r in b["rows"]:
            all_syms.append(r["symbol"])
            lo, hi = b["lo"], b["hi"]
            assert lo < r["cmp"] <= hi, (b["label"], r["symbol"], r["cmp"])
            # level ordering
            assert 0 < r["sl"] < r["buy_low"] <= r["buy_high"] <= r["cmp"] + 0.051, r
            assert r["cmp"] < r["st"] < r["mt"], r
            assert (r["st"] * 20) % 1 < 1e-6 or abs(round(r["st"] / 0.05) * 0.05 - r["st"]) < 1e-6
            assert r["rationale"] and r["rank"] >= 1
            assert (r["entry"] - r["sl"]) / r["entry"] <= 0.1001, "risk cap"
        scores = [(x["strict"], x["score"]) for x in b["rows"]]
        assert scores == sorted(scores, reverse=True), "rows ranked: full matches first, then score"
    assert not any(s.startswith("DN") for s in all_syms), "downtrends must never be picked"
    for bad in ("ILLIQ", "PENNY", "SPLIT", "SHORT", "BIG", "DEAD"):
        assert bad not in all_syms, bad
    assert sum(len(b["rows"]) for b in rep["brackets"]) > 20
    print("ok  screening  (", sum(len(b['rows']) for b in rep['brackets']), "picks )")
    return rep


def test_fundamentals():
    good = S.fundamentals_assessment({"debtToEquity": 20, "earningsQuarterlyGrowth": 0.3, "trailingEps": 4,
                                      "returnOnEquity": 0.2, "sector": "Industrials"})
    assert good["score"] == 20 and not good["red_flag"]
    bad = S.fundamentals_assessment({"debtToEquity": 350, "trailingEps": -2, "sector": "Industrials"})
    assert bad["red_flag"] and bad["score"] < 8
    bank = S.fundamentals_assessment({"debtToEquity": 900, "trailingEps": 3, "sector": "Financial Services"})
    assert not bank["red_flag"], "banks/NBFCs exempt from D/E flag"
    none = S.fundamentals_assessment(None)
    assert none["score"] == 8 and not none["red_flag"]      # neutral when unknown
    print("ok  fundamentals")


def test_render_and_outputs(rep):
    cfg = S.Config()
    rep = dict(rep)
    rep["generated_at"] = "13 Nov 2026, 08:30 IST"
    rep["market"] = S.nifty_snapshot(pd.Series(np.linspace(20000, 25000, 260), index=DATES))
    web = S.render_html(rep, cfg, web=True)
    mail = S.render_html(rep, cfg, web=False)
    for h in (web, mail):
        assert "educational and" in h and "research purposes only" in h, "disclaimer present"
        assert h.count("<table>") == sum(1 for b in rep["brackets"] if b["rows"]) + (1 if rep.get("fresh") else 0)
    assert 'href="#b0"' in web and 'href="#b0"' not in mail
    kb = len(mail.encode()) / 1024
    print(f"ok  render     (email html {kb:.0f} KB; Gmail clips ~102 KB)")
    assert kb < 100, "email would be clipped by Gmail"
    # XSS-safety of names
    rep2 = dict(rep)
    rep2["brackets"] = [dict(rep["brackets"][0])]
    rep2["brackets"][0]["rows"] = [dict(rep["brackets"][0]["rows"][0], name="<script>x</script>")] \
        if rep["brackets"][0]["rows"] else []
    if rep2["brackets"][0]["rows"]:
        assert "<script>x" not in S.render_html(rep2, cfg)

    with tempfile.TemporaryDirectory() as d:
        cfg.out_dir = Path(d) / "docs"
        S.write_outputs(rep, web, cfg)
        assert (cfg.out_dir / "report.html").exists() and (cfg.out_dir / "latest.json").exists()
        assert (cfg.out_dir / "archive" / f"{rep['data_date']}.html").exists()
        assert (cfg.out_dir / "archive" / "index.html").exists()
    print("ok  outputs")
    return mail


def test_email(rep, html):
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, m): sent["msg"] = m

    env = {"SMTP_USER": "me@example.com", "SMTP_PASS": "app-pass", "EMAIL_TO": "a@example.com, b@example.com"}
    with mock.patch.dict("os.environ", env), mock.patch("smtplib.SMTP_SSL", FakeSMTP):
        S.send_email(rep, html)
    m = sent["msg"]
    assert m["To"] == "a@example.com, b@example.com" and "2026-11-12" in m["Subject"]
    kinds = [p.get_content_type() for p in m.walk()]
    assert "text/plain" in kinds and kinds.count("text/html") == 2, kinds   # body + attachment
    with mock.patch.dict("os.environ", {"SMTP_USER": "", "SMTP_PASS": "", "EMAIL_TO": ""}):
        try:
            S.send_email(rep, html)
            raise AssertionError("should fail without config")
        except RuntimeError:
            pass
    print("ok  email")


def test_levels_edge():
    # stock far above every support: zone must be below CMP, never above
    f = dict(close=500.0, atr=5.0, S1=480, ema20=470, ema50=450, fib382=460, fib50=440, lo20=430,
             R1=505, R2=510, hi60=520, ext1272=530, ext1618=545)
    lv = S.compute_levels(f)
    assert lv["buy_high"] <= 500 and lv["buy_low"] == 495.0 or lv["buy_low"] < 500
    assert lv["sl"] < lv["buy_low"] and lv["st"] > 500 and lv["mt"] > lv["st"]
    print("ok  levels edge", lv["buy_low"], lv["buy_high"], lv["st"], lv["mt"], lv["sl"])


def test_full_size_email():
    """Worst case: 10 brackets x 10 picks must stay under Gmail's ~102 KB clipping limit."""
    cfg = S.Config()
    hist, names = {}, {}
    for b in range(10):
        for k in range(16):
            drift = 0.0012 + 0.00005 * k
            sym = f"U{b}X{k}"
            hist[sym] = make_df((b * 100 + 25 + k * 4) / math.exp(drift * 260) * 0.95, drift, vol_spike=1.3)
            names[sym] = f"A Reasonably Long Company Name Number {b}-{k} Industries Limited"
    rep = S.run_screen(hist, names, cfg, fake_funds, NOW)
    n = sum(len(b["rows"]) for b in rep["brackets"])
    rep["generated_at"], rep["market"] = "13 Nov 2026, 08:30 IST", {}
    kb = len(S.render_html(rep, cfg, web=False).encode()) / 1024
    est100 = kb * 100 / n
    print(f"ok  worst-case email: {n} picks -> {kb:.0f} KB (≈{est100:.0f} KB at 100 picks)")
    assert n >= 80 and est100 < 100, (n, est100)


def test_main_end_to_end():
    """main(): writes report, skips on holiday (no new session), --force overrides, stale data aborts."""
    import os

    hist, names = build_universe()
    uni = pd.DataFrame({"symbol": list(names), "name": list(names.values()), "industry": ""})

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    old = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            with mock.patch.object(S, "load_universe", lambda cfg: uni), \
                 mock.patch.object(S, "fetch_history", lambda syms, cfg: hist), \
                 mock.patch.object(S, "market_context", lambda cfg: {}), \
                 mock.patch.object(S, "fetch_fundamentals", lambda syms, cfg: fake_funds(syms)), \
                 mock.patch.object(Q, "fetch_metrics", lambda syms, qcfg: {}), \
                 mock.patch.object(S, "datetime", FakeDT):
                assert S.main(["--no-email"]) == 0
                p = Path("docs/report.html")
                assert p.exists() and "2026-11-12" in p.read_text()
                m1 = p.stat().st_mtime_ns
                assert S.main(["--no-email"]) == 0                      # same session => skipped
                assert p.stat().st_mtime_ns == m1, "should not regenerate on a holiday"
                assert S.main(["--no-email", "--force"]) == 0
                assert p.stat().st_mtime_ns != m1, "--force must regenerate"
                # email failure (no config) => exit code 1 but report still written
                with mock.patch.dict("os.environ", {"SMTP_USER": "", "SMTP_PASS": "", "EMAIL_TO": ""}):
                    assert S.main(["--force"]) == 1
            # stale data guard
            stale = {k: v.copy() for k, v in hist.items()}
            for k in stale:
                stale[k].index = stale[k].index - pd.Timedelta(days=30)
            with mock.patch.object(S, "load_universe", lambda cfg: uni), \
                 mock.patch.object(S, "fetch_history", lambda syms, cfg: stale), \
                 mock.patch.object(S, "datetime", FakeDT):
                assert S.main(["--no-email", "--force"]) == 2
        finally:
            os.chdir(old)
    print("ok  main() end-to-end (report, holiday-skip, force, email-fail, stale guard)")


# --------------------------------------------------------------------------- #
# Quality screen (22 filters)
# --------------------------------------------------------------------------- #
def _stmt(rows, ncols=4):
    """Fake yfinance statement: rows = {label: [newest .. oldest]}."""
    cols = pd.to_datetime([f"{2026 - i}-03-31" for i in range(ncols)])
    data = {lab: (vals + [None] * ncols)[:ncols] for lab, vals in rows.items()}
    return pd.DataFrame(data, index=cols).T


def good_company(**over):
    """A company that should pass every filter."""
    rev = [1000, 850, 730, 640]          # ~16% CAGR
    ni = [120, 100, 85, 72]              # ~19% CAGR
    inc = _stmt({"Total Revenue": rev, "Net Income": ni, "EBIT": [190, 160, 135, 115],
                 "Interest Expense": [10, 10, 11, 12]})
    bs = _stmt({"Total Assets": [900, 800, 700, 620], "Current Liabilities": [300, 270, 240, 210],
                "Total Debt": [100, 110, 120, 130], "Stockholders Equity": [450, 380, 320, 270],
                "Accounts Receivable": [110, 100, 90, 80],
                "Ordinary Shares Number": [10e6, 10e6, 10e6, 10e6]})
    cf = _stmt({"Operating Cash Flow": [110, 95, 80, 70], "Free Cash Flow": [60, 50, 40, 30]})
    info = {"sector": "Industrials", "marketCap": 2000e7, "sharesOutstanding": 100e6, "trailingEps": 12.0,
            "debtToEquity": 22.0, "returnOnEquity": 0.22, "operatingMargins": 0.19,
            "earningsQuarterlyGrowth": 0.25, "heldPercentInsiders": 0.55, "heldPercentInstitutions": 0.12}
    info.update(over)
    return Q.extract_metrics(info, inc, bs, cf)


def feat(close=200.0, sma200=150.0, turnover=5.0, **kw):
    f = {"close": close, "sma200": sma200, "turnover_cr": turnover, "zero_vol_days": 0,
         "max_abs_ret60": 0.05, "atr": 4.0, "ema20": close * 0.98, "ema50": close * 0.95,
         "ema200": close * 0.8, "S1": close * 0.97, "S2": close * 0.94, "R1": close * 1.03,
         "R2": close * 1.06, "hi60": close * 1.08, "lo60": close * 0.8, "fib382": close * 0.96,
         "fib50": close * 0.93, "fib618": close * 0.9, "ext1272": close * 1.1, "ext1618": close * 1.2,
         "lo20": close * 0.92, "hi20": close * 1.02, "hi52": close * 1.1}
    f.update(kw)
    return f


def test_quality_metrics():
    m = good_company()
    assert 15 < m["sales_cagr3"] < 17.5 and 17 < m["profit_cagr3"] < 21, m
    assert abs(m["roce_latest"] - 190 / 600 * 100) < 0.01, m["roce_latest"]
    assert abs(m["de"] - 0.22) < 1e-9 and m["int_cov"] == 19.0
    assert m["cfo_pat"] > 70 and m["fcf_pos"] == 3 and m["dilution3"] == 0
    assert m["roe"] == 22.0 and abs(m["op_margin"] - 19.0) < 1e-9
    assert abs(m["promoter"] - 55.0) < 1e-9 and abs(m["inst"] - 12.0) < 1e-9
    # missing statements must not crash, just yield unknowns
    empty = Q.extract_metrics({"sector": "x"}, None, None, None)
    assert empty["roce_latest"] is None and empty["sales_cagr3"] is None and empty["cfo_pat"] is None
    # loss-making base year => CAGR undefined, not a crash or a fake number
    assert Q._cagr([100, 80, 60, -5]) is None and Q._cagr([100, 80]) is None
    print("ok  quality metrics")


def test_quality_filters():
    cfg = Q.QualityConfig()
    ev = Q.evaluate(good_company(), feat(), cfg)
    assert not ev["hard_fail"] and not ev["hard_unknown"] and not ev["soft_fail"], ev
    assert all(ev["ok"][n] is True for n in Q.HARD + Q.SOFT), ev["ok"]
    assert ev["ok"][11] is None                       # pledging is always manual
    assert abs(ev["vals"]["pe"] - 200 / 12) < 1e-9 and ev["vals"]["peg"] is not None
    # each hard filter must be able to fail on its own
    cases = {
        3: dict(), 4: dict(returnOnEquity=0.05), 5: dict(operatingMargins=0.05), 8: dict(earningsQuarterlyGrowth=-0.1),
        9: dict(debtToEquity=90.0), 13: dict(heldPercentInsiders=0.2), 14: dict(heldPercentInstitutions=0.01),
    }
    for n, over in cases.items():
        if n == 3:
            continue
        ev = Q.evaluate(good_company(**over), feat(), cfg)
        assert n in ev["hard_fail"], (n, ev["hard_fail"])
    assert 15 in Q.evaluate(good_company(), feat(close=400.0), cfg)["hard_fail"]            # P/E 33
    assert 16 in Q.evaluate(good_company(), feat(sma200=300.0), cfg)["hard_fail"]           # below 200-DMA
    assert 2 in Q.evaluate(good_company(), feat(turnover=0.4), cfg)["hard_fail"]            # illiquid
    assert 1 in Q.evaluate(good_company(marketCap=20000e7, sharesOutstanding=1e9), feat(), cfg)["hard_fail"]
    ev = Q.evaluate(good_company(trailingEps=-3.0), feat(), cfg)                            # loss-making
    assert 15 in ev["hard_fail"] and ev["vals"]["pe"] == Q.LOSS_PE
    # missing data => unknown, never a pass or a fail
    ev = Q.evaluate(Q.extract_metrics({"sector": "x"}, None, None, None), feat(), cfg)
    assert 3 in ev["hard_unknown"] and 3 not in ev["hard_fail"]
    # soft checks
    bad = good_company()
    bad["cfo_pat"], bad["dilution3"], bad["rec_days"] = 30.0, 12.0, [200.0, 100.0]
    ev = Q.evaluate(bad, feat(), cfg)
    assert {18, 20, 21} <= set(ev["soft_fail"]) and not ev["hard_fail"], ev
    print("ok  quality filters")


def quality_universe():
    """Synthetic funds for a universe of uptrend stocks; returns (feats, names, industries, funds)."""
    hist, names = build_universe()
    funds, industries = {}, {}
    feats = {}
    for sym, df in hist.items():
        f = S.compute_features(df, S.Config(), NOW)
        if f is not None:
            feats[sym] = f
    for i, sym in enumerate(feats):
        m = good_company()
        m["sector"] = ["Industrials", "Chemicals", "Autos", "Textiles"][i % 4] if sym.startswith("UP") else "Industrials"
        m["shares"], m["eps"], m["mcap_cr"] = None, feats[sym]["close"] / 12, 1500.0   # P/E 12, mcap ₹1,500 cr
        if sym.startswith("UP") and sym.endswith("X0"):
            m["sector"] = "Financial Services"                  # must be excluded
        funds[sym] = m
        industries[sym] = ""
    return feats, names, industries, funds


def test_quality_screen():
    cfg = Q.QualityConfig()
    feats, names, industries, funds = quality_universe()
    q = Q.screen(feats, names, industries, cfg, S.compute_levels, lambda syms: {s: funds[s] for s in syms if s in funds})
    st = q["stats"]
    assert st["illiquid"] >= 3 and st["anomaly"] >= 1 and st["financials"] >= 1, st
    assert 0 < len(q["rows"]) <= cfg.top_n, len(q["rows"])
    per_sector = {}
    for r in q["rows"]:
        per_sector[r["sector"]] = per_sector.get(r["sector"], 0) + 1
        assert not r["hard_fail"] and r["sl"] < r["buy_low"] <= r["buy_high"], r
        assert "financial" not in r["sector"].lower()
        # (BIG is a ₹3,500 share price: the quality screen filters on market cap, not share price)
        assert r["symbol"] not in ("ILLIQ", "PENNY", "SPLIT", "SHORT", "DEAD") and not r["symbol"].startswith("DN")
    assert max(per_sector.values()) <= cfg.sector_cap, per_sector
    assert [r["rank"] for r in q["rows"]] == list(range(1, len(q["rows"]) + 1))
    assert q["filters"][10][1] == "manual" and len(q["filters"]) == 22
    # a stock failing a hard filter must never appear
    funds2 = {s: dict(m, roe=3.0) for s, m in funds.items()}
    q2 = Q.screen(feats, names, industries, cfg, S.compute_levels, lambda syms: {s: funds2[s] for s in syms if s in funds2})
    assert q2["rows"] == [] and q2["misses"], "ROE failure must exclude all"
    # report must be JSON-serialisable (written to latest.json)
    json.dumps(q, default=str)
    print("ok  quality screen (", len(q["rows"]), "shortlisted; sectors", per_sector, ")")
    return q


def test_quality_render(q):
    cfg = S.Config()
    html = Q.render_html_section(q)
    assert 'id="quality"' in html and "The 22 filters" in html and "promoter pledging" in html
    has_funnel = bool(q["funnel"]["n_small"] and q["funnel"]["rows"])
    assert html.count("<table>") == 1 + (1 if has_funnel else 0)
    txt = Q.render_text_section(q)
    assert "Quality small-cap shortlist" in txt and q["rows"][0]["symbol"] in txt
    bad = json.loads(json.dumps(q, default=str))
    bad["rows"][0]["name"] = "<script>x</script>"
    assert "<script>x" not in Q.render_html_section(bad), "names must be escaped"
    empty = dict(q, rows=[], misses=[])
    assert "No stock passed" in Q.render_html_section(empty) and "No stock passed" in Q.render_text_section(empty)
    # integrated into the full report + email size stays under Gmail's clipping limit
    rep, _ = S.run_screen(*build_universe(), cfg, fake_funds, NOW), None
    rep = dict(rep, generated_at="13 Nov 2026, 08:30 IST", market={}, quality=q)
    web, mail = S.render_html(rep, cfg, web=True), S.render_html(rep, cfg, web=False)
    assert 'href="#quality"' in web and 'href="#quality"' not in mail and 'id="quality"' in mail
    assert "Quality small-cap shortlist" in S.render_text(rep) and "Fresh signals today" in S.render_text(rep)
    kb = len(mail.encode()) / 1024
    assert kb < 100, f"email {kb:.0f} KB would be clipped by Gmail"
    print(f"ok  quality render (email html {kb:.0f} KB)")


def test_quality_fetch_cache():
    cfg = Q.QualityConfig(budget_s=30)
    calls = []

    class FakeTicker:
        def __init__(self, sym):
            calls.append(sym)
            self.income_stmt = _stmt({"Total Revenue": [100, 90, 80, 70], "Net Income": [10, 9, 8, 7],
                                      "EBIT": [15, 14, 12, 10]})
            self.balance_sheet = _stmt({"Total Assets": [90, 80, 70, 60], "Current Liabilities": [20, 18, 16, 14]})
            self.cashflow = _stmt({"Operating Cash Flow": [9, 8, 7, 6]})

        def get_info(self):
            return {"sector": "Industrials", "marketCap": 1000e7, "trailingEps": 3.0}

    fake_yf = types.SimpleNamespace(Ticker=FakeTicker)
    with tempfile.TemporaryDirectory() as d:
        cfg.data_dir = Path(d)
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf}):
            r1 = Q.fetch_metrics(["AAA", "BBB"], cfg)
            n1 = len(calls)
            r2 = Q.fetch_metrics(["AAA", "BBB"], cfg)           # served from cache
        assert set(r1) == {"AAA", "BBB"} and n1 == 2 and len(calls) == n1, calls
        assert r2["AAA"]["roce_latest"] is not None and (Path(d) / "quality_cache.json").exists()
        # a symbol that errors is skipped, not fatal
        def boom(sym):
            raise RuntimeError("rate limited")
        with mock.patch.dict(sys.modules, {"yfinance": types.SimpleNamespace(Ticker=boom)}):
            assert Q.fetch_metrics(["CCC"], cfg) == {}
    print("ok  quality fetch + cache")


def test_main_with_quality():
    """main() with the quality screen on: report has the section, email carries it, failures are isolated."""
    import os

    hist, names = build_universe()
    uni = pd.DataFrame({"symbol": list(names), "name": list(names.values()), "industry": ""})
    _, _, _, funds = quality_universe()

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): pass
        def send_message(self, m): sent["msg"] = m

    env = {"SMTP_USER": "me@example.com", "SMTP_PASS": "x", "EMAIL_TO": "a@example.com"}
    old = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            with mock.patch.object(S, "load_universe", lambda cfg: uni), \
                 mock.patch.object(S, "fetch_history", lambda syms, cfg: hist), \
                 mock.patch.object(S, "market_context", lambda cfg: {}), \
                 mock.patch.object(S, "fetch_fundamentals", lambda syms, cfg: fake_funds(syms)), \
                 mock.patch.object(Q, "fetch_metrics", lambda syms, qcfg: {s: funds[s] for s in syms if s in funds}), \
                 mock.patch.object(S, "datetime", FakeDT), \
                 mock.patch.dict("os.environ", env), mock.patch("smtplib.SMTP_SSL", FakeSMTP):
                assert S.main(["--force"]) == 0
                rep = json.loads(Path("docs/latest.json").read_text())
                assert rep["quality"]["rows"], "quality section must be in latest.json"
                assert 'id="quality"' in Path("docs/report.html").read_text()
                body = sent["msg"].get_body(preferencelist=("plain",)).get_content()
                assert "Quality small-cap shortlist" in body
                # --no-quality skips it
                assert S.main(["--force", "--no-email", "--no-quality"]) == 0
                assert "quality" not in json.loads(Path("docs/latest.json").read_text())
                # a crash inside the quality screen must not stop the main report or the email
                sent.clear()
                with mock.patch.object(Q, "screen", side_effect=RuntimeError("boom")):
                    assert S.main(["--force"]) == 0
                assert "msg" in sent and "quality" not in json.loads(Path("docs/latest.json").read_text())
        finally:
            os.chdir(old)
    print("ok  main() with quality (section, email text, --no-quality, failure isolation)")


# --------------------------------------------------------------------------- #
# Variety (rotation, fresh signals, pick history) and the wider universe
# --------------------------------------------------------------------------- #
def breakout_df():
    """Own RNG (does not disturb the shared one): steady uptrend, last bar closes above the 52-week high on 2.5x volume."""
    rng = np.random.default_rng(11)
    n = len(DATES)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0008, 0.008, n)))
    high, low = close * 1.004, close * 0.996
    vol = np.full(n, 2_000_000.0)
    close[-1] = high[-253:-1].max() * 1.002
    high[-1], low[-1], vol[-1] = close[-1] * 1.001, close[-2], 5_000_000.0
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol}, index=DATES)


def test_streaks_and_history():
    hist = {"2026-11-10": ["A", "B", "C"], "2026-11-11": ["A", "B"], "2026-11-12": ["A", "D"]}
    st = S.compute_streaks(hist, "2026-11-13")
    assert st == {"A": 3, "D": 1}, st               # B dropped out on 11-12, so its streak is 0
    assert S.compute_streaks(hist, "2026-11-12") == {"A": 2, "B": 2}      # a same-day re-run ignores today's entry
    assert S.compute_streaks(hist, "2026-11-10") is None and S.compute_streaks({}, "2026-11-13") is None
    cfg = S.Config()
    assert S.rotation_penalty("A", st, cfg) == 9.0 and S.rotation_penalty("Z", st, cfg) == 0.0
    assert S.rotation_penalty("A", {"A": 40}, cfg) == cfg.rotation_pts * cfg.rotation_max_days   # capped
    assert S.rotation_penalty("A", None, cfg) == 0.0
    with tempfile.TemporaryDirectory() as d:
        cfg.data_dir = Path(d) / "data"
        assert S.load_pick_history(cfg) == {}
        h = {}
        for i in range(20):
            S.save_pick_history(cfg, h, f"2026-10-{i + 1:02d}", ["Z", "A"])
            h = S.load_pick_history(cfg)
        assert len(h) == cfg.pick_history_days and min(h) == "2026-10-06" and h["2026-10-20"] == ["A", "Z"], sorted(h)
        (cfg.data_dir / "pick_history.json").write_text("{not json")
        assert S.load_pick_history(cfg) == {}, "corrupt history must not crash the run"
    print("ok  streaks + pick history")


def test_rotation_in_ranking():
    cfg = S.Config()
    hist, names = build_universe()
    base = S.run_screen(hist, names, cfg, fake_funds, NOW)
    assert all(r["streak"] is None for b in base["brackets"] for r in b["rows"]), "no history => no badges"
    tops = {b["rows"][0]["symbol"] for b in base["brackets"] if len(b["rows"]) >= 4}
    assert tops
    streaks = {s: 5 for s in tops}
    rot = S.run_screen(hist, names, cfg, fake_funds, NOW, streaks)
    moved = 0
    for b0, b1 in zip(base["brackets"], rot["brackets"]):
        for r in b1["rows"]:
            exp = 5 if r["symbol"] in tops else 0
            assert r["streak"] == exp
            assert abs(r["rank_score"] - (r["score"] - cfg.rotation_pts * exp)) < 0.11, r
        ranks = [(x["strict"], x["rank_score"]) for x in b1["rows"]]
        assert ranks == sorted(ranks, reverse=True), "ordered by rank_score after rotation"
        if b0["rows"] and b0["rows"][0]["symbol"] in tops and len(b1["rows"]) > 1:
            old = {x["symbol"]: x["rank"] for x in b0["rows"]}
            new = {x["symbol"]: x["rank"] for x in b1["rows"]}
            top = b0["rows"][0]["symbol"]
            assert top not in new or new[top] >= old[top], "a repeated leader must never move up"
            moved += new.get(top, 99) > old[top]
    assert moved >= 1, "rotation should demote at least one repeated leader in this synthetic universe"
    print("ok  rotation demotes repeated names (", moved, "brackets changed )")


def test_fresh_signals():
    base = dict(close=100.0, ema50=95.0, ema200=80.0, rsi=60.0, vol_ratio=1.0, prior_hi20=105.0,
                prior_hi52=110.0, chg_pct=0.5)
    assert S.fresh_signal(base) is None
    assert S.fresh_signal(dict(base, close=112.0, vol_ratio=1.3)) == ("New 52-week high", 3)
    assert S.fresh_signal(dict(base, close=106.0, vol_ratio=1.6, prior_hi52=120.0)) == ("20-day breakout on volume", 2)
    assert S.fresh_signal(dict(base, vol_ratio=2.6, chg_pct=3.0))[1] == 1
    assert S.fresh_signal(dict(base, close=112.0, vol_ratio=1.0)) is None, "breakout needs volume"
    assert S.fresh_signal(dict(base, close=112.0, vol_ratio=1.3, ema200=130.0)) is None, "below 200 EMA"
    assert S.fresh_signal(dict(base, close=112.0, vol_ratio=1.3, rsi=85.0)) is None, "too stretched"
    assert S.fresh_signal(dict(base, close=112.0, vol_ratio=1.3, ema200=math.nan)) is None
    # end to end: an engineered breakout stock must be reported, bad/illiquid ones must not
    hist, names = build_universe()
    hist["BRK"], names["BRK"] = breakout_df(), "Breakout Co"
    f = S.compute_features(hist["BRK"], S.Config(), NOW)
    assert S.fresh_signal(f) is not None, {k: f[k] for k in ("close", "prior_hi52", "vol_ratio", "rsi", "ema200")}
    rep = S.run_screen(hist, names, S.Config(), fake_funds, NOW)
    fr = rep["fresh"]
    assert any(r["symbol"] == "BRK" and r["signal"] == "New 52-week high" for r in fr), fr
    assert [(r["prio"], r["vol_ratio"]) for r in fr] == sorted([(r["prio"], r["vol_ratio"]) for r in fr], reverse=True)
    assert len(fr) <= S.Config().fresh_top_n and not any(r["symbol"] in ("ILLIQ", "PENNY", "SPLIT", "DEAD", "BIG") for r in fr)
    assert all(r["sl"] < r["cmp"] < r["st"] for r in fr)
    for r in fr:                                            # breakout levels: stop 1.5 ATR below, target 3 ATR above CMP
        assert 0.6 < (r["st"] - r["cmp"]) / max(r["cmp"] - r["sl"], 1e-9) < 2.5 and r["st_pct"] > 2.0, r
    rep.update(generated_at="13 Nov 2026, 08:30 IST", market={})
    html, txt = S.render_html(rep, S.Config(), web=True), S.render_text(rep)
    assert 'id="fresh"' in html and 'href="#fresh"' in html and "Breakout Co" in html and "BRK" in txt
    assert "<script" not in S.render_html(dict(rep, fresh=[dict(fr[0], name="<script>x</script>")]), S.Config())
    none_html = S.render_html(dict(rep, fresh=[]), S.Config())
    assert "No breakout or volume-surge signals today" in none_html
    assert 'id="fresh"' not in S.render_html({k: v for k, v in rep.items() if k != "fresh"}, S.Config())
    print("ok  fresh signals (", len(fr), "reported )")


def test_universe_all():
    def csv_total():
        rows = ["Company Name,Industry,Symbol,Series,ISIN Code"] + [f"Tot {i} Ltd,Industrials,T{i},EQ,INE{i}" for i in range(120)]
        return "\n".join(rows)

    def csv_equity():
        rows = ["SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING"]
        rows += [f"T{i},Tot {i} Ltd,EQ,01-JAN-2000" for i in range(60)]                 # duplicates of the Total Market list
        rows += [f"E{i},Extra {i} Ltd,{'BE' if i < 50 else 'EQ'},01-JAN-2010" for i in range(1200)]
        return "\n".join(rows)

    state = {"fail_all": False}

    def fake_get(url, headers=None, timeout=None):
        if "EQUITY_L" in url:
            if state["fail_all"]:
                raise RuntimeError("blocked")
            text = csv_equity()
        elif "niftytotalmarket" in url:
            text = csv_total()
        else:
            raise RuntimeError("unexpected url " + url)
        return types.SimpleNamespace(text=text, raise_for_status=lambda: None)

    assert S.Config().universe == "all"
    with tempfile.TemporaryDirectory() as d, mock.patch.object(S.requests, "get", fake_get), \
            mock.patch.object(S.time, "sleep", lambda s: None):
        cfg = S.Config(universe="all", data_dir=Path(d))
        u = S.load_universe(cfg)
        syms = list(u["symbol"])
        assert len(syms) == 120 + 1150 and len(set(syms)) == len(syms), len(syms)
        assert syms[:120] == [f"T{i}" for i in range(120)], "Total Market list comes first (priority if time runs out)"
        assert "E49" not in syms and "E50" in syms, "only EQ series; BE (trade-to-trade) excluded"
        assert dict(zip(u["symbol"], u["name"]))["E60"] == "Extra 60 Ltd"
        assert dict(zip(u["symbol"], u["industry"]))["T5"] == "Industrials"
        state["fail_all"] = True                                             # NSE blocks the full list -> cached copy
        assert len(S.load_universe(cfg)) == 1270
    with tempfile.TemporaryDirectory() as d, mock.patch.object(S.requests, "get", fake_get), \
            mock.patch.object(S.time, "sleep", lambda s: None):
        state["fail_all"] = True                                             # blocked and no cache -> Total Market list
        assert len(S.load_universe(S.Config(universe="all", data_dir=Path(d)))) == 120
        assert len(S.load_universe(S.Config(universe="total", data_dir=Path(d)))) == 120
    print("ok  universe=all (merge, dedupe, EQ only, cache fallback, total fallback)")


def test_history_budget():
    calls = []

    def download(*a, **k):
        calls.append(1)
        return pd.DataFrame()

    fake_yf = types.SimpleNamespace(download=download)
    with mock.patch.dict(sys.modules, {"yfinance": fake_yf}), mock.patch.object(S.time, "sleep", lambda s: None):
        assert S.fetch_history([f"S{i}" for i in range(250)], S.Config(history_budget_s=-1)) == {}
        assert not calls, "an exhausted budget must stop before downloading anything"
        S.fetch_history([f"S{i}" for i in range(250)], S.Config(history_budget_s=600))
        assert len(calls) == 3, "250 symbols / chunk 100 = 3 downloads"
    print("ok  history time budget")


def test_quality_funnel_and_order():
    cfg = Q.QualityConfig()
    feats, names, industries, funds = quality_universe()
    funds = {s: dict(m) for s, m in funds.items()}
    for s, m in funds.items():
        m["roe"] = 3.0                                   # every stock fails ROE
        if s.endswith("X1"):
            m["mcap_cr"] = 20000.0                       # large caps: fail the size filter
    asked = []

    def fetch(syms):
        asked.extend(syms)
        return {s: funds[s] for s in syms if s in funds}

    q = Q.screen(feats, names, industries, cfg, S.compute_levels, fetch)
    assert q["rows"] == [] and q["stats"]["too_liquid"] >= 1, q["stats"]
    assert "BIG" not in asked, "large-cap turnover skips the fundamentals fetch"
    keys = [abs(math.log(max(feats[s]["turnover_cr"], 0.01) / cfg.focus_turnover_cr)) for s in asked]
    assert keys == sorted(keys), "fetch order: typical small-cap turnover first"
    fn = q["funnel"]
    assert fn["n_small"] > 0 and fn["rows"][0][0] == 4 and fn["rows"][0][2] == fn["n_small"], fn["rows"][:3]
    assert all(n not in (1, 11) for n, *_ in fn["rows"]), "size filter and the manual filter are not in the funnel"
    assert q["misses"] and all(not r["symbol"].endswith("X1") for r in q["misses"]), "misses must be inside the size range"
    assert all(1 not in r["hard_fail"] and len(r["hard_fail"]) <= cfg.miss_max_hard_fail for r in q["misses"])
    html, txt = Q.render_html_section(q), Q.render_text_section(q)
    assert "Which filters remove the most stocks" in html and "ROE" in txt and "Closest misses" in txt
    json.dumps(q, default=str)
    print("ok  quality funnel, meaningful misses, fetch order")


def test_main_variety():
    """Two consecutive sessions: day 2 shows New / Day-N badges, the banner, and saves history."""
    import os

    hist, names = build_universe()
    hist["BRK"], names["BRK"] = breakout_df(), "Breakout Co"
    uni = pd.DataFrame({"symbol": list(names), "name": list(names.values()), "industry": ""})

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    old = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            with mock.patch.object(S, "load_universe", lambda cfg: uni), \
                 mock.patch.object(S, "fetch_history", lambda syms, cfg: hist), \
                 mock.patch.object(S, "market_context", lambda cfg: {}), \
                 mock.patch.object(S, "fetch_fundamentals", lambda syms, cfg: fake_funds(syms)), \
                 mock.patch.object(S, "datetime", FakeDT):
                assert S.main(["--no-email", "--no-quality"]) == 0
                day1 = json.loads(Path("data/pick_history.json").read_text())
                assert list(day1) == ["2026-11-12"] and len(day1["2026-11-12"]) > 20
                html1 = Path("docs/report.html").read_text()
                assert "Day 2" not in html1 and 'class="b bn"' not in html1, "first run: nothing to compare with"
                assert "BRK" in html1 and 'id="fresh"' in html1
                # pretend those picks were also listed the previous session, minus a few that become "New"
                listed = day1["2026-11-12"]
                prev = {"2026-11-11": listed[3:]}
                Path("data/pick_history.json").write_text(json.dumps(prev))
                assert S.main(["--no-email", "--no-quality", "--force"]) == 0
                html2 = Path("docs/report.html").read_text()
                assert "Day 2" in html2 and 'class="b bn">New' in html2 and "new</strong> since the previous session" in html2
                rep = json.loads(Path("docs/latest.json").read_text())
                streaks = [r["streak"] for b in rep["brackets"] for r in b["rows"]]
                assert set(streaks) <= {0, 1} and 0 in streaks and 1 in streaks, set(streaks)
                saved = json.loads(Path("data/pick_history.json").read_text())
                assert set(saved) == {"2026-11-11", "2026-11-12"}
        finally:
            os.chdir(old)
    print("ok  main() variety (first run, badges, banner, history saved)")


if __name__ == "__main__":
    test_indicators()
    test_fundamentals()
    report = test_brackets_and_screen()
    mail_html = test_render_and_outputs(report)
    test_email(report, mail_html)
    test_levels_edge()
    test_full_size_email()
    test_main_end_to_end()
    test_quality_metrics()
    test_quality_filters()
    quality_report = test_quality_screen()
    test_quality_render(quality_report)
    test_quality_fetch_cache()
    test_main_with_quality()
    test_streaks_and_history()
    test_rotation_in_ranking()
    test_fresh_signals()
    test_universe_all()
    test_history_budget()
    test_quality_funnel_and_order()
    test_main_variety()
    print("\nALL TESTS PASSED")

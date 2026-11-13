#!/usr/bin/env python3
"""Offline tests: synthetic prices, no network. Run:  python tests/test_offline.py"""
import math
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import screener as S  # noqa: E402

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
        assert h.count("<table>") == sum(1 for b in rep["brackets"] if b["rows"])
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
        assert (cfg.out_dir / "index.html").exists() and (cfg.out_dir / "latest.json").exists()
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
                 mock.patch.object(S, "datetime", FakeDT):
                assert S.main(["--no-email"]) == 0
                p = Path("docs/index.html")
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


if __name__ == "__main__":
    test_indicators()
    test_fundamentals()
    report = test_brackets_and_screen()
    mail_html = test_render_and_outputs(report)
    test_email(report, mail_html)
    test_levels_edge()
    test_full_size_email()
    test_main_end_to_end()
    print("\nALL TESTS PASSED")

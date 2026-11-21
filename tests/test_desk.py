#!/usr/bin/env python3
"""Offline tests for desk.py (intraday watchlist, CNC shortlist, pulse, news flags, sizing). Run: python tests/test_desk.py"""
import json
import math
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import desk as D  # noqa: E402
import screener as S  # noqa: E402
import test_offline as T  # noqa: E402  (also stubs the network calls in desk)

DC = D.DeskConfig()


def test_sizing():
    s = D.size_position(100.0, 98.0, 100_000, 1.0, 1.0, 0.34)
    assert s["qty"] == 340 and s["capped"], s                       # risk would allow 500, position cap says 340
    s = D.size_position(100.0, 98.0, 100_000, 1.0, 5.0, 0.34)
    assert s["qty"] == 500 and not s["capped"] and abs(s["risk"] - 1000) < 1e-6
    assert abs(s["margin"] - 10_000) < 1e-6 and abs(s["value"] - 50_000) < 1e-6
    assert D.size_position(100.0, 100.0, 1e5, 1.0)["qty"] == 0       # zero stop distance
    assert D.size_position(5000.0, 4990.0, 10_000, 1.0, 1.0, 0.34)["qty"] == 0  # one share does not fit the cap
    print("ok  position sizing (risk cap, position cap, leverage, degenerate inputs)")


def test_env_config():
    with mock.patch.dict(os.environ, {"INTRADAY_CAPITAL": "250000", "INTRADAY_LEVERAGE": "5",
                                      "CNC_CAPITAL": "", "CNC_RISK_PCT": "abc"}):
        c = D.DeskConfig.from_env()
    assert c.intraday_capital == 250000 and c.intraday_leverage == 5
    assert c.cnc_capital == 500_000 and c.cnc_risk_pct == 0.75       # blank / invalid fall back to defaults
    print("ok  settings from environment (blank and invalid values fall back)")


def feat(**kw):
    f = {"close": 300.0, "open": 295.0, "high": 301.0, "low": 294.0, "atr": 8.0, "ema20": 290.0, "ema50": 280.0,
         "rsi": 62.0, "chg_pct": 1.8, "vol_ratio": 1.6, "turnover_cr": 60.0, "zero_vol_days": 0,
         "max_abs_ret60": 0.05}
    f.update(kw)
    return f


def test_intraday_rules_and_levels():
    f = feat()
    assert D.intraday_eligible(f, DC) and D.intraday_side(f) == "long"
    assert D.intraday_side(feat(close=294.5, chg_pct=-0.2)) is None                    # not a decisive close
    assert D.intraday_side(feat(rsi=82)) is None                                      # overbought
    assert not D.intraday_eligible(feat(turnover_cr=10), DC)                          # illiquid for intraday
    assert not D.intraday_eligible(feat(atr=30.0), DC)                                # too wild (10% ATR)
    assert not D.intraday_eligible(feat(atr=2.0), DC)                                 # too quiet (0.7% ATR)
    assert not D.intraday_eligible(feat(max_abs_ret60=0.4), DC)                       # split / bad tick
    sh = feat(close=200.0, open=205.0, high=206.0, low=199.0, ema20=210.0, ema50=220.0, rsi=38.0,
              chg_pct=-2.0, atr=5.0)
    assert D.intraday_side(sh) == "short"

    lv = D.intraday_levels(f, "long", DC)
    assert lv["sl"] < f["high"] < lv["trigger"] < lv["skip"] and lv["trigger"] < lv["t1"] < lv["t2"], lv
    assert abs(lv["rr1"] - DC.t1_atr / DC.sl_atr) < 0.05, lv
    ls = D.intraday_levels(sh, "short", DC)
    assert ls["t2"] < ls["t1"] < ls["trigger"] < sh["low"] and ls["skip"] < ls["trigger"] < ls["sl"], ls
    score, why = D.intraday_score(f, "long")
    assert 0 < score <= 100 and why
    # a stronger close and heavier volume must score higher
    assert D.intraday_score(feat(high=300.5, vol_ratio=2.4), "long")[0] > D.intraday_score(feat(vol_ratio=0.9), "long")[0]
    # narrow CPR check: pivot range of a close at the high is wide, a close at the midpoint is narrow
    assert D.cpr_width_pct(feat(close=297.5, high=301.0, low=294.0)) < D.cpr_width_pct(feat(close=301.0))
    print("ok  intraday eligibility, long/short rules, trigger / stop / target geometry, score ordering")


def test_news_and_results_parsing():
    now = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
    new = {"content": {"title": "Acme bags Rs 500 crore order", "pubDate": "2026-11-20T00:30:00Z",
                       "canonicalUrl": {"url": "https://example.com/a"}}}
    old = {"title": "Acme faces SEBI probe", "link": "https://example.com/b",
           "providerPublishTime": int(datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc).timestamp())}
    a, b = D.parse_news_item(new, now), D.parse_news_item(old, now)
    assert a["tone"] == "pos" and abs(a["age_h"] - 2.5) < 0.01 and a["url"].endswith("/a")
    assert b["tone"] == "neg" and abs(b["age_h"] - 24) < 0.01 and b["url"].endswith("/b")
    assert D.parse_news_item({"content": {"title": "x"}}, now) is None                  # no timestamp
    assert D.parse_news_item({"foo": 1}, now) is None
    assert D.headline_tone("Profit jumps but SEBI probe widens") == "mixed"
    assert D.headline_tone("Board meeting on Friday") == "neutral"
    ref = date(2026, 11, 23)
    assert D.parse_earnings_date({"Earnings Date": [date(2026, 11, 10), date(2026, 11, 25)]}, ref) == date(2026, 11, 25)
    assert D.parse_earnings_date({"Earnings Date": [date(2026, 11, 10)]}, ref) is None
    cal = pd.DataFrame({0: [pd.Timestamp("2026-11-24")]}, index=["Earnings Date"])
    assert D.parse_earnings_date(cal, ref) == date(2026, 11, 24)
    assert D.parse_earnings_date(None, ref) is None and D.parse_earnings_date({}, ref) is None
    print("ok  news (both yfinance layouts, tone) and results-date parsing")


def test_fetch_events_isolation():
    """One failing / slow symbol must not break the rest."""
    def fake(sym, ref, now, age):
        if sym == "BAD":
            raise RuntimeError("boom")
        return {"headline": f"{sym} wins order", "tone": "pos", "age_h": 1.0, "url": ""}
    with mock.patch.object(D, "_events_for", fake):
        res = D._real_fetch_events(["A", "BAD", "B"], date(2026, 11, 23), DC)
    assert set(res) == {"A", "B"}
    print("ok  event fetch isolates per-symbol failures")


def test_global_bias():
    p = lambda l, v, c: {"label": l, "last": v, "chg_pct": c}  # noqa: E731
    assert D.global_bias([p("S&P 500", 6000, -1.4), p("Nasdaq", 20000, -1.9)])["tone"] == "weak"
    assert D.global_bias([p("S&P 500", 6000, 1.2), p("Nasdaq", 20000, 1.6)])["tone"] == "strong"
    g = D.global_bias([p("S&P 500", 6000, 0.2), p("Nasdaq", 20000, -0.1), p("India VIX", 22.5, 3.0)])
    assert g["tone"] == "neutral" and "VIX" in g["text"]
    assert D.global_bias([])["tone"] == "unknown"
    s = D.pulse_from_series("X", pd.Series([100.0, 101.0], index=pd.bdate_range("2026-11-18", periods=2)))
    assert abs(s["chg_pct"] - 1.0) < 1e-9 and D.pulse_from_series("X", pd.Series([1.0])) is None
    print("ok  market pulse and global-cue summary")


def desk_universe():
    """Trend stocks with a crafted last bar so there are definite long and short intraday candidates."""
    hist, names = T.build_universe()
    d = T.DATES
    for k in range(4):                                  # long candidates
        df = T.make_df(180.0, 0.0015, base_vol=3_000_000)
        pc = float(df["Close"].iloc[-2])
        df.iloc[-1, df.columns.get_loc("Open")] = pc
        df.iloc[-1, df.columns.get_loc("Close")] = pc * (1.02 + 0.002 * k)
        df.iloc[-1, df.columns.get_loc("High")] = pc * (1.0225 + 0.002 * k)
        df.iloc[-1, df.columns.get_loc("Low")] = pc * 0.996
        df.iloc[-1, df.columns.get_loc("Volume")] *= 1.9
        hist[f"LNG{k}"], names[f"LNG{k}"] = df, f"Long Cand {k}"
    for k in range(2):                                  # short candidates
        df = T.make_df(300.0, -0.0018, base_vol=3_000_000)
        pc = float(df["Close"].iloc[-2])
        df.iloc[-1, df.columns.get_loc("Open")] = pc
        df.iloc[-1, df.columns.get_loc("Close")] = pc * (0.98 - 0.002 * k)
        df.iloc[-1, df.columns.get_loc("Low")] = pc * (0.9775 - 0.002 * k)
        df.iloc[-1, df.columns.get_loc("High")] = pc * 1.004
        df.iloc[-1, df.columns.get_loc("Volume")] *= 1.6
        hist[f"SHT{k}"], names[f"SHT{k}"] = df, f"Short Cand {k}"
    return hist, names


def build_report(hist, names):
    streaks = None
    rep = S.run_screen(hist, names, S.Config(), lambda syms: T.fake_funds(syms), T.NOW, streaks)
    feats = {s: f for s, df in hist.items() if (f := S.compute_features(df, S.Config(), T.NOW))}
    rep["market"], rep["generated_at"] = {}, "test"
    return rep, feats


def test_build_desk():
    hist, names = desk_universe()
    rep, feats = build_report(hist, names)
    cnc = D.cnc_candidates(rep, DC)
    assert cnc, "synthetic data should yield Full-match CNC ideas"
    ic = D.intraday_candidates(feats, names, DC)
    assert {r["symbol"] for r in ic["long"]} & {f"LNG{k}" for k in range(4)}, ic["long"]
    assert {r["symbol"] for r in ic["short"]} & {"SHT0", "SHT1"}, ic["short"]
    assert all(r["side"] == "long" for r in ic["long"]) and all(r["side"] == "short" for r in ic["short"])
    assert not any(r["symbol"].startswith("DN") and r["side"] == "long" for r in ic["long"])
    assert not any(r["symbol"] in ("ILLIQ", "PENNY", "SPLIT") for r in ic["long"] + ic["short"])

    first_long = ic["long"][0]["symbol"]
    cnc_blocked = cnc[0]["symbol"]
    seen = {}

    def events(syms):
        seen["syms"] = syms
        ev = {first_long: {"earn_date": "2026-11-23", "earn_days": 0},                  # results on the session date
              cnc_blocked: {"earn_date": "2026-11-25", "earn_days": 2}}                  # results inside the CNC blackout
        other = [r["symbol"] for r in ic["long"][1:2]]
        for s in other:
            ev[s] = {"headline": "Co <b>wins</b> order", "tone": "pos", "age_h": 3.0, "url": "https://e.com/x?a=1&b=2"}
        return ev

    pulse = [{"label": "S&P 500", "last": 6000.0, "chg_pct": 1.5, "date": "2026-11-19"},
             {"label": "Nasdaq", "last": 20000.0, "chg_pct": 1.7, "date": "2026-11-19"},
             {"label": "India VIX", "last": 13.1, "chg_pct": -2.0, "date": "2026-11-19"}]
    dk = D.build_desk(feats, names, rep, DC, events_fn=events, pulse_fn=lambda: pulse)
    assert dk["session"] == "2026-11-13"                                                  # data date Thu 12 Nov -> Fri 13 Nov
    assert set(seen["syms"]) >= {first_long, cnc_blocked}
    longs = [r["symbol"] for r in dk["intraday"]["long"]]
    assert first_long not in longs and any(first_long in s for s in dk["intraday"]["skipped"])
    assert cnc_blocked not in [r["symbol"] for r in dk["cnc"]["rows"]] and dk["cnc"]["skipped"]
    assert len(dk["intraday"]["long"]) <= DC.long_n and len(dk["intraday"]["short"]) <= DC.short_n
    assert len(dk["cnc"]["rows"]) <= DC.cnc_n
    per = {}
    for r in dk["cnc"]["rows"]:
        per[r["bracket"]] = per.get(r["bracket"], 0) + 1
        assert r["status"] in ("In buy zone", "Wait for pullback") and r["rr"] >= DC.cnc_min_rr
        assert r["size"]["value"] <= DC.cnc_capital * DC.cnc_max_pos_pct / 100 + r["buy_high"]
    assert max(per.values()) <= DC.cnc_per_bracket
    for r in dk["intraday"]["long"] + dk["intraday"]["short"]:
        assert r["size"]["risk"] <= DC.intraday_capital * DC.intraday_risk_pct / 100 + 1e-6
    assert dk["bias"]["tone"] == "strong"
    json.dumps(dk)                                                                         # must be serialisable for latest.json

    html = D.render_html(dk)
    for needle in ("Market pulse", "Intraday (MIS) watchlist", "BUY above", "SELL below", "CNC delivery shortlist",
                   "Square off before about 3:15 PM", "₹1,00,000" if False else "100,000"):
        assert needle in html, needle
    assert "<b>wins</b>" not in html and "&lt;b&gt;wins&lt;/b&gt;" in html                 # headlines are escaped
    assert "a=1&amp;b=2" in html                                                           # and so are URLs
    txt = D.render_text(dk)
    assert "Intraday (MIS)" in txt and "CNC delivery shortlist" in txt
    print("ok  build_desk (candidates, results-date exclusions, per-bracket cap, sizing bounds, escaping, JSON, text)")
    return dk


def test_empty_inputs():
    rep = {"data_date": date(2026, 11, 20), "brackets": [{"label": "x", "rows": []}]}
    dk = D.build_desk({}, {}, rep, DC, events_fn=lambda s: {}, pulse_fn=lambda: [])
    assert dk["session"] == "2026-11-23"                                                  # Friday data -> Monday session
    html = D.render_html(dk)
    assert "No long candidates" in html and "No short candidates" in html and "No full-match delivery idea" in html
    assert "Global cues unavailable" in html
    print("ok  empty universe renders cleanly (weekend roll-over to Monday)")


def test_main_integration():
    hist, names = desk_universe()
    uni = pd.DataFrame({"symbol": list(names), "name": list(names.values()), "industry": ""})

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return T.NOW

    old = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            with mock.patch.object(S, "load_universe", lambda cfg: uni), \
                 mock.patch.object(S, "fetch_history", lambda syms, cfg: hist), \
                 mock.patch.object(S, "market_context", lambda cfg: {}), \
                 mock.patch.object(S, "fetch_fundamentals", lambda syms, cfg: T.fake_funds(syms)), \
                 mock.patch.object(T.Q, "fetch_metrics", lambda syms, qcfg: {}), \
                 mock.patch.object(S, "datetime", FakeDT):
                assert S.main(["--no-email"]) == 0
                html = Path("docs/report.html").read_text()
                assert "NSE Daily Trading Desk" in html and "Intraday (MIS) watchlist" in html
                assert 'href="#intraday"' in html and "CNC delivery shortlist" in html
                latest = json.loads(Path("docs/latest.json").read_text())
                assert latest["desk"]["intraday"]["long"], "desk must be saved to latest.json"
                text = S.render_text(latest | {"data_date": latest["data_date"]})
                assert "Intraday (MIS)" in text

                # --no-desk and the TRADING_DESK switch
                assert S.main(["--no-email", "--force", "--no-desk"]) == 0
                assert "Intraday (MIS) watchlist" not in Path("docs/report.html").read_text()
                with mock.patch.dict(os.environ, {"TRADING_DESK": "0"}):
                    assert S.main(["--no-email", "--force"]) == 0
                    assert "desk" not in json.loads(Path("docs/latest.json").read_text())

                # a crash inside the desk must not stop the report or the email
                with mock.patch.object(D, "build_desk", side_effect=RuntimeError("yahoo down")):
                    assert S.main(["--no-email", "--force"]) == 0
                    h2 = Path("docs/report.html").read_text()
                    assert "Intraday (MIS) watchlist" not in h2 and "₹" in h2           # bracket tables still there

                # email carries the new subject and the desk text
                sent = {}

                class FakeSMTP:
                    def __init__(self, *a, **k): pass
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                    def login(self, *a): pass
                    def send_message(self, m): sent["m"] = m

                env = {"SMTP_USER": "a@b.c", "SMTP_PASS": "x", "EMAIL_TO": "me@b.c"}
                with mock.patch.dict(os.environ, env), mock.patch.object(S.smtplib, "SMTP_SSL", FakeSMTP):
                    assert S.main(["--force"]) == 0
                m = sent["m"]
                assert "Intraday + CNC" in m["Subject"]
                body = m.get_body(("plain",)).get_content()
                assert "Intraday (MIS)" in body and "CNC delivery shortlist" in body
        finally:
            os.chdir(old)
    print("ok  main() integration (page, nav, latest.json, --no-desk, TRADING_DESK=0, crash isolation, email)")


if __name__ == "__main__":
    test_sizing()
    test_env_config()
    test_intraday_rules_and_levels()
    test_news_and_results_parsing()
    test_fetch_events_isolation()
    test_global_bias()
    test_build_desk()
    test_empty_inputs()
    test_main_integration()
    print("\nDESK TESTS PASSED")

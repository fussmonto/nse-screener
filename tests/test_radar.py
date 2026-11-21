#!/usr/bin/env python3
"""Offline tests for radar.py (watchlist + market headlines). Run: python tests/test_radar.py"""
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import radar as R  # noqa: E402
import screener as S  # noqa: E402
import notify_telegram as TG  # noqa: E402
import test_offline as T  # noqa: E402  (synthetic prices; also stubs the network in desk / radar)

CFG = S.Config()
NOW = T.NOW
FETCH_NEWS = T.R._real_fetch_news                     # the real implementation, stubbed out in the other suites

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>  Sensex jumps 400 points as Nifty hits record high  </title><link>https://e.com/a?x=1&amp;y=2</link>
      <pubDate>Fri, 20 Nov 2026 10:30:00 +0530</pubDate></item>
<item><title>UP0X1 Ltd shares crash after fraud probe</title><link>https://e.com/b</link>
      <pubDate>Fri, 20 Nov 2026 09:00:00 +0530</pubDate></item>
<item><title>Old story from last month</title><link>https://e.com/old</link>
      <pubDate>Mon, 19 Oct 2026 09:00:00 +0530</pubDate></item>
<item><title>Rupee holds steady</title><link>https://e.com/c</link></item>
<item><title></title><link>https://e.com/empty</link></item>
</channel></rss>"""


def write_watchlist(d, rows):
    p = Path(d) / "watchlist.json"
    p.write_text(json.dumps(rows))
    return p


def test_symbols_and_modes():
    assert R.normalise_symbol(" nse:suzlon ") == "SUZLON" and R.normalise_symbol("SBIN.NS") == "SBIN"
    assert R.normalise_symbol(None) == "" and R.normalise_symbol("nan") == ""
    assert R.normalise_mode("mis") == "INTRADAY" and R.normalise_mode("Intraday") == "INTRADAY"
    assert R.normalise_mode(None) == "CNC" and R.normalise_mode("whatever") == "CNC"
    assert R._num(float("nan")) is None and R._num(float("inf")) is None and R._num("x") is None and R._num(1.005, 2) == 1.0
    print("ok  symbol / mode normalisation, NaN-safe numbers")


def test_load_watchlist():
    with tempfile.TemporaryDirectory() as d:
        p = write_watchlist(d, [
            {"symbol": "suzlon", "name": "Suzlon Energy Ltd", "sector": "Energy", "mode": "cnc"},
            {"symbol": "SUZLON.NS", "name": "dupe"},                       # duplicate after normalising
            {"symbol": "mahabank", "mode": "MIS", "custom_buy": 50},
            {"symbol": "", "name": "blank"}, "junk", {"name": "no symbol"},
        ])
        items, src = R.load_watchlist(p, sheet_url="")
        assert src == "watchlist.json" and [i["symbol"] for i in items] == ["SUZLON", "MAHABANK"], items
        assert items[1]["mode"] == "INTRADAY" and items[1]["custom_buy"] == 50 and items[1]["name"] == "MAHABANK"

        sheet = pd.DataFrame({"Symbol": ["irb", "nhpc", float("nan")], "Name": ["IRB Infra", None, "x"],
                              "Mode": ["CNC", "Intraday", "CNC"], "My_Buy_Price": [17.5, float("nan"), 1]})
        items, src = R.load_watchlist(p, sheet_url="http://sheet", read_csv=lambda u: sheet)
        assert src == "google-sheet" and [i["symbol"] for i in items] == ["IRB", "NHPC"]
        assert items[0]["custom_buy"] == 17.5 and items[1]["custom_buy"] is None and items[1]["mode"] == "INTRADAY"

        def boom(u):
            raise OSError("offline")
        items, src = R.load_watchlist(p, sheet_url="http://sheet", read_csv=boom)
        assert src == "watchlist.json" and len(items) == 2, "sheet failure falls back to the JSON file"
        items, src = R.load_watchlist(p, sheet_url="http://sheet", read_csv=lambda u: pd.DataFrame({"Symbol": []}))
        assert src == "watchlist.json", "empty sheet falls back too"
        assert R.load_watchlist(Path(d) / "missing.json", sheet_url="") == ([], "none")
        (Path(d) / "bad.json").write_text("{not json")
        assert R.load_watchlist(Path(d) / "bad.json", sheet_url="") == ([], "none")
    print("ok  watchlist loading (json, sheet override, dedupe, modes, fallbacks, bad files)")


def one_stock(sym="UP3X2", **kw):
    hist, names = T.build_universe()
    df = hist[sym]
    f = S.compute_features(df, CFG, NOW)
    lv = S.compute_levels(f)
    meta = {"symbol": sym, "name": names[sym], "sector": "", "mode": "CNC", "custom_buy": kw.get("custom_buy")}
    return meta, f, lv, df


def test_analyze_stock():
    meta, f, lv, df = one_stock()
    row = R.analyze_stock(meta, f, lv, df, {"eps": 5.0, "roe": 18.2, "roce_latest": 22.0, "de": 0.3, "sector": "Industrials"},
                          "Capital Goods")
    json.dumps(row, allow_nan=False)                                     # strict JSON: no NaN / inf anywhere
    assert row["sl"] < row["buy_low"] <= row["buy_high"] <= row["cmp"] + 0.051 and row["cmp"] < row["t1"] < row["t2"], row
    assert row["sl"] == lv["sl"] and row["t1"] == lv["st"] and row["t2"] == lv["mt"], "same levels as the bracket report"
    assert row["sector"] == "Industrials", "Yahoo sector wins over the universe industry when no sector was given"
    assert abs(row["fund"]["pe"] - row["cmp"] / 5.0) < 0.06 and row["fund"]["roe"] == 18.2
    assert len(row["chart"]["close"]) == len(row["chart"]["ema20"]) == len(row["chart"]["dates"]) == R.CHART_BARS
    assert row["chart"]["close"][-1] == row["cmp"]
    assert row["in_zone"] == (row["cmp"] <= row["buy_high"] * 1.01)
    assert row["breakout"] >= row["cmp"] and abs(row["breakout"] / 0.05 - round(row["breakout"] / 0.05)) < 1e-6
    assert R.analyze_stock(meta, f, lv, df, None)["fund"] is None, "no fundamentals => null, never zeros"
    assert R.analyze_stock(meta, f, lv, df, {"eps": -2.0})["fund"]["pe"] is None, "loss-making => no P/E"
    # the analyst's sector label beats Yahoo's when the watchlist gives one
    assert R.analyze_stock({**meta, "sector": "My sector"}, f, lv, df, {"sector": "Industrials"})["sector"] == "My sector"

    meta2, f2, lv2, df2 = one_stock(custom_buy=100.0)
    r2 = R.analyze_stock(meta2, f2, lv2, df2, None)
    assert (r2["buy_low"], r2["buy_high"], r2["custom_buy"]) == (98.0, 100.0, 100.0), r2
    print("ok  analyze_stock (levels match the screener, strict JSON, fundamentals optional, custom buy price)")


def test_classify_setup():
    base = {"close": 100.0, "ema20": 95.0, "ema50": 90.0, "ema200": 80.0, "rsi": 55.0, "vol_ratio": 1.0, "hi52": 200.0}
    assert R.classify_setup({**base, "rsi": 30}) == "Oversold dip: accumulation zone"
    assert R.classify_setup({**base, "vol_ratio": 2.0}) == "Volume breakout above 20-EMA"
    assert R.classify_setup({**base, "hi52": 101}) == "Near 52-week high"
    assert R.classify_setup(base) == "Uptrend: above all EMAs"
    assert R.classify_setup({**base, "ema200": float("nan")}) == "Uptrend: above all EMAs"
    assert R.classify_setup({**base, "ema200": 120.0}) == "Bullish trend continuation"
    assert R.classify_setup({**base, "close": 85.0}) == "Consolidation: watch the 20-EMA"
    print("ok  setup classification")


def test_rss_and_news():
    now_utc = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
    items = R.parse_rss(RSS, "ET", now_utc)
    assert len(items) == 4 and items[0]["title"] == "Sensex jumps 400 points as Nifty hits record high", items
    assert items[0]["url"] == "https://e.com/a?x=1&y=2" and items[3]["ts"] is None and items[3]["age_h"] is None
    assert R.parse_rss("<not xml", "ET", now_utc) == []

    wl = [{"symbol": "UP0X1", "name": "UP0X1 Ltd"}, {"symbol": "NHPC", "name": "NHPC Ltd"}]
    assert R.tag_headline("UP0X1 Ltd shares crash", wl) == "UP0X1"
    assert R.tag_headline("NHPC and UP0X1 Ltd rally", wl) == "UP0X1, NHPC"
    assert R.tag_headline("Sensex ends higher", wl) == "NIFTY" and R.tag_headline("Rupee holds", wl) == "MARKET"
    assert R.tag_headline("Unhpcish word", wl) == "MARKET", "whole-word matches only"

    calls = []

    def get(url):
        calls.append(url)
        if "bad" in url:
            raise OSError("feed down")
        return RSS
    feeds = [("ET", "http://good/1"), ("Bad", "http://bad/2"), ("Mint", "http://good/3")]
    news = FETCH_NEWS(wl, NOW, get, feeds)
    assert len(calls) == 3, "every feed is tried; the failing one is skipped"
    titles = [n["title"] for n in news]
    assert "Old story from last month" not in titles, "stale (> 72 h) headlines dropped"
    assert len(titles) == len(set(titles)) == 3, "same headline from two feeds appears once"
    assert titles[0].startswith("Sensex"), "newest first"
    by = {n["title"]: n for n in news}
    assert by["UP0X1 Ltd shares crash after fraud probe"]["tone"] == "Bearish"
    assert by["UP0X1 Ltd shares crash after fraud probe"]["tag"] == "UP0X1"
    assert by[titles[0]]["time"] == "20 Nov, 10:30 IST", by[titles[0]]
    assert FETCH_NEWS(wl, NOW, lambda u: (_ for _ in ()).throw(OSError("x")), feeds) == []
    print("ok  RSS parsing, whole-word tagging, dedupe, staleness, tone, failing feed isolated")


def test_build_radar():
    hist, names = T.build_universe()
    with tempfile.TemporaryDirectory() as d:
        p = write_watchlist(d, [
            {"symbol": "UP3X2", "name": "Uptrend", "mode": "CNC"},
            {"symbol": "up4x1.ns", "mode": "MIS"},
            {"symbol": "EXTRA", "name": "Not in the screen"},             # downloaded on demand
            {"symbol": "SHORT", "name": "Too little history"},            # feature calc returns None -> "missing"
            {"symbol": "GONE", "name": "No data at all"},
        ])
        asked = []

        def fetch_missing(syms):
            asked.append(list(syms))
            return {"EXTRA": T.make_df(80.0, 0.001), "SHORT": hist["SHORT"]}

        out = R.build_radar(
            hist, {"UP3X2": "Capital Goods"}, now=NOW, path=p, sheet_url="", with_news=False,
            compute_features=lambda df: S.compute_features(df, CFG, NOW), compute_levels=S.compute_levels,
            fetch_missing=fetch_missing,
            fetch_metrics=lambda syms: {"UP3X2": {"eps": 4.0, "roe": 12.0, "sector": "Industrials"}})
        json.dumps(out, allow_nan=False)
        assert out["source"] == "watchlist.json" and out["news"] == []
        assert [r["symbol"] for r in out["watchlist"]] == ["UP3X2", "UP4X1", "EXTRA"], out["missing"]
        assert sorted(out["missing"]) == ["GONE", "SHORT"], out["missing"]
        assert asked == [["EXTRA", "GONE"]], "only symbols absent from the screen are downloaded"
        by = {r["symbol"]: r for r in out["watchlist"]}
        assert by["UP4X1"]["mode"] == "INTRADAY" and by["UP3X2"]["fund"]["roe"] == 12.0 and by["UP4X1"]["fund"] is None
        assert by["UP3X2"]["sector"] == "Industrials" and by["UP4X1"]["sector"] == ""

        # failure isolation: downloads, fundamentals, a bad row and the headline feeds may each fail independently
        def boom(*a, **k):
            raise RuntimeError("yahoo down")
        real = R.analyze_stock

        def flaky(meta, *a, **k):
            if meta["symbol"] == "UP4X1":
                raise ValueError("bad row")
            return real(meta, *a, **k)
        with mock.patch.object(R, "analyze_stock", flaky):
            out2 = R.build_radar(
                hist, {}, now=NOW, path=p, sheet_url="", with_news=False,
                compute_features=lambda df: S.compute_features(df, CFG, NOW), compute_levels=S.compute_levels,
                fetch_missing=boom, fetch_metrics=boom)
        assert [r["symbol"] for r in out2["watchlist"]] == ["UP3X2"], out2
        assert set(out2["missing"]) == {"UP4X1", "EXTRA", "SHORT", "GONE"}
        # empty watchlist -> clean, empty result (still valid JSON)
        e = R.build_radar(hist, {}, now=NOW, path=Path(d) / "nope.json", sheet_url="", with_news=False,
                          compute_features=lambda df: None, compute_levels=S.compute_levels)
        assert e == {"source": "none", "watchlist": [], "missing": [], "news": []}
    print("ok  build_radar (hist reuse, on-demand download, missing symbols, per-step failure isolation, empty list)")
    return out


def test_renderers(radar_out):
    html = R.render_html_section({**radar_out, "news": [
        {"title": "<b>x</b> rally", "url": "https://e.com/?a=1&b=2", "source": "ET", "time": "20 Nov", "tone": "Bullish", "tag": "X"},
        {"title": "no link", "url": "", "source": "ET", "time": "", "tone": "Neutral", "tag": "X"}]})
    for needle in ('id="watchlist"', "UP3X2", "Buy zone", 'id="news"', "&lt;b&gt;x&lt;/b&gt; rally", "a=1&amp;b=2"):
        assert needle in html, needle
    assert "<b>x</b>" not in html and "no link" not in html, "escaped; link-less headlines skipped"
    assert "No watchlist stocks" in R.render_html_section({"watchlist": [], "missing": ["A<B"]}) and "A&lt;B" in \
        R.render_html_section({"watchlist": [], "missing": ["A<B"]})
    txt = R.render_text_section(radar_out)
    assert "== My watchlist ==" in txt and "UP3X2" in txt
    print("ok  classic-report sections (escaping, empty state, plain text)")


def test_telegram_block(radar_out):
    rep = {"data_date": "2026-11-20", "radar": radar_out}
    msgs = TG.build_messages(rep)
    joined = "\n".join(msgs)
    assert "MY WATCHLIST" in joined and "UP3X2" in joined and all(len(m) <= 4096 for m in msgs)
    assert "MY WATCHLIST" not in "\n".join(TG.build_messages({"data_date": "2026-11-20"}))
    assert "MY WATCHLIST" not in "\n".join(TG.build_messages({"data_date": "2026-11-20", "radar": {"watchlist": []}}))
    print("ok  telegram watchlist block (present, absent, under the size limit)")


def test_main_integration():
    hist, names = T.build_universe()
    uni = pd.DataFrame({"symbol": list(names), "name": list(names.values()), "industry": "Capital Goods"})

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return T.NOW

    old = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            write_watchlist(".", [{"symbol": "UP3X2", "name": "Uptrend Co", "mode": "CNC"},
                                  {"symbol": "UP5X0", "name": "Another", "mode": "INTRADAY"}])
            fake_news = [{"title": "UP3X2 wins order", "url": "https://e.com/1", "source": "ET", "time": "20 Nov, 10:30 IST",
                          "tone": "Bullish", "tag": "UP3X2"}]
            with mock.patch.object(S, "load_universe", lambda cfg: uni), \
                 mock.patch.object(S, "fetch_history", lambda syms, cfg: hist), \
                 mock.patch.object(S, "market_context", lambda cfg: {}), \
                 mock.patch.object(S, "fetch_fundamentals", lambda syms, cfg: T.fake_funds(syms)), \
                 mock.patch.object(T.Q, "fetch_metrics", lambda syms, qcfg: {}), \
                 mock.patch.object(R, "fetch_news", lambda wl, now, get=None, feeds=None: fake_news), \
                 mock.patch.object(S, "datetime", FakeDT):
                assert S.main(["--no-email"]) == 0
                latest = json.loads(Path("docs/latest.json").read_text())      # strict parse below, like a browser would
                json.loads(Path("docs/latest.json").read_text(), parse_constant=lambda c: (_ for _ in ()).throw(
                    AssertionError(f"{c} in latest.json")))
                rd = latest["radar"]
                assert [r["symbol"] for r in rd["watchlist"]] == ["UP3X2", "UP5X0"] and rd["news"] == fake_news
                assert rd["watchlist"][0]["sector"] == "Capital Goods"
                html = Path("docs/report.html").read_text()
                assert 'id="watchlist"' in html and 'href="#watchlist"' in html and "UP3X2" in html
                assert "UP3X2 wins order" in html
                assert Path("docs/archive").exists() and not Path("docs/index.html").exists(), \
                    "the pipeline must never write (or overwrite) the website's index.html"
                assert "My watchlist" in S.render_text(latest | {"data_date": latest["data_date"]})

                # switches: --no-radar and RADAR=0
                assert S.main(["--no-email", "--force", "--no-radar"]) == 0
                assert "radar" not in json.loads(Path("docs/latest.json").read_text())
                with mock.patch.dict(os.environ, {"RADAR": "0"}):
                    assert S.main(["--no-email", "--force"]) == 0
                    assert "radar" not in json.loads(Path("docs/latest.json").read_text())
                assert 'id="watchlist"' not in Path("docs/report.html").read_text()

                # a crash inside the radar must not stop the report or the email
                with mock.patch.object(R, "build_radar", side_effect=RuntimeError("feed down")):
                    assert S.main(["--no-email", "--force"]) == 0
                    h2 = Path("docs/report.html").read_text()
                    assert 'id="watchlist"' not in h2 and "₹" in h2
                    assert "radar" not in json.loads(Path("docs/latest.json").read_text())
        finally:
            os.chdir(old)
    print("ok  main() integration (latest.json, classic report, index.html untouched, --no-radar, RADAR=0, crash isolation)")


if __name__ == "__main__":
    test_symbols_and_modes()
    test_load_watchlist()
    test_analyze_stock()
    test_classify_setup()
    test_rss_and_news()
    out = test_build_radar()
    test_renderers(out)
    test_telegram_block(out)
    test_main_integration()
    print("\nRADAR TESTS PASSED")

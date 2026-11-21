#!/usr/bin/env python3
"""Offline tests for notify_telegram.py. Run: python tests/test_telegram.py"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify_telegram as N  # noqa: E402
import test_desk as TD  # noqa: E402
import test_offline as T  # noqa: E402
import screener as S  # noqa: E402
import desk as D  # noqa: E402


def make_report():
    hist, names = TD.desk_universe()
    rep, feats = TD.build_report(hist, names)
    pulse = [{"label": "S&P 500", "last": 6000.0, "chg_pct": 0.4, "date": "x"},
             {"label": "India VIX", "last": 14.2, "chg_pct": -1.0, "date": "x"}]
    ev = lambda syms: {syms[0]: {"headline": "Acme <wins> & bags order", "tone": "pos", "age_h": 2.0, "url": "",
                                 "earn_date": "2026-11-25", "earn_days": 2}}  # noqa: E731
    rep["desk"] = D.build_desk(feats, names, rep, D.DeskConfig(), events_fn=ev, pulse_fn=lambda: pulse)
    rep["market"] = {"close": 25000.0, "chg_pct": 0.5, "above_50": True, "above_200": True, "date": "2026-11-12"}
    return rep


def test_messages():
    rep = make_report()
    msgs = N.build_messages(rep, "https://u.github.io/r/")
    assert msgs and all(len(m) <= 4096 for m in msgs)
    text = "\n".join(msgs)
    for needle in ("INTRADAY", "CNC DELIVERY", "BUY above", "SELL below", "Square off", "Full dashboard", "Nifty 25,000"):
        assert needle in text, needle
    assert "<wins> &" not in text and "&lt;wins&gt; &amp; bags" in text          # headline escaped for Telegram HTML
    # every tag that is opened is closed within the same message (Telegram rejects broken HTML)
    import re
    for m in msgs:
        for tag in ("b", "i", "code", "a"):
            assert len(re.findall(rf"<{tag}[ >]", m)) == len(re.findall(rf"</{tag}>", m)), (tag, m[:80])
    # no desk (feature off) still produces a valid message
    bare = {k: v for k, v in rep.items() if k != "desk"}
    assert N.build_messages(bare) and "INTRADAY" not in "\n".join(N.build_messages(bare))
    # long input is split, never truncated
    big = dict(rep)
    big["fresh"] = [{"symbol": f"S{i}", "cmp": 1.0, "chg_pct": 1.0, "vol_ratio": 2.0, "signal": "x" * 300,
                     "sl": 1.0, "st": 2.0} for i in range(5)]
    parts = N._split("\n".join(["line " + "y" * 200] * 60))
    assert len(parts) > 1 and all(len(p) <= N.LIMIT for p in parts)
    assert sum(p.count("line") for p in parts) == 60
    assert all(len(p) <= N.LIMIT for p in N._split("z" * 9000))
    print("ok  messages (content, escaping, balanced tags, split under 4096, no-desk fallback)")


class Resp:
    def __init__(self, code=200, desc=""):
        self.status_code, self._d = code, desc

    def json(self):
        return {"description": self._d}


def test_send():
    rep = make_report()
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        return Resp(400, "chat not found") if kw.get("data", {}).get("chat_id") == "bad" else Resp()

    with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
        assert not N.configured() and N.send_report(rep, "<html/>") == 0
    env = {"TELEGRAM_BOT_TOKEN": "123:ABC", "TELEGRAM_CHAT_ID": "111, bad ,222"}
    with mock.patch.dict(os.environ, env), mock.patch.object(N.requests, "post", post):
        assert N.configured()
        ok = N.send_report(rep, "<html>x</html>")
    assert ok == 2, "bad chat must not stop the others"
    docs = [c for c in calls if c[0].endswith("/sendDocument")]
    assert len(docs) == 2 and docs[0][1]["files"]["document"][0].startswith("nse_desk_")
    assert all("bot123:ABC/" in c[0] for c in calls)
    assert all(c[1]["data"]["parse_mode"] == "HTML" for c in calls if c[0].endswith("/sendMessage"))
    # errors never leak the token
    try:
        with mock.patch.object(N.requests, "post", lambda *a, **k: Resp(401, "Unauthorized")):
            N._call("123:ABC", "sendMessage", data={})
    except RuntimeError as e:
        assert "123:ABC" not in str(e) and "401" in str(e)
    else:
        raise AssertionError("expected failure")
    print("ok  sending (multi-chat, failure isolation, file upload, token never in errors)")


def test_main_integration():
    hist, names = TD.desk_universe()
    uni = pd.DataFrame({"symbol": list(names), "name": list(names.values()), "industry": ""})

    class FakeDT(S.datetime):
        @classmethod
        def now(cls, tz=None):
            return T.NOW

    sent = []
    old = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            env = {"TELEGRAM_BOT_TOKEN": "1:A", "TELEGRAM_CHAT_ID": "9", "SMTP_USER": "", "SMTP_PASS": "", "EMAIL_TO": ""}
            with mock.patch.object(S, "load_universe", lambda cfg: uni), \
                 mock.patch.object(S, "fetch_history", lambda syms, cfg: hist), \
                 mock.patch.object(S, "market_context", lambda cfg: {}), \
                 mock.patch.object(S, "fetch_fundamentals", lambda syms, cfg: T.fake_funds(syms)), \
                 mock.patch.object(T.Q, "fetch_metrics", lambda syms, qcfg: {}), \
                 mock.patch.object(S, "datetime", FakeDT), \
                 mock.patch.dict(os.environ, env), \
                 mock.patch.object(N.requests, "post", lambda url, **kw: sent.append(url) or Resp()):
                rc = S.main(["--force"])
                assert rc == 1, "email is not configured so exit code 1 ..."
                assert any(u.endswith("/sendDocument") for u in sent), "... but Telegram must still be delivered"
                sent.clear()
                assert S.main(["--force", "--no-email"]) == 0 and not sent          # --no-email silences Telegram too
            # Telegram failure does not break the email path and is reported
            with mock.patch.object(S, "load_universe", lambda cfg: uni), \
                 mock.patch.object(S, "fetch_history", lambda syms, cfg: hist), \
                 mock.patch.object(S, "market_context", lambda cfg: {}), \
                 mock.patch.object(S, "fetch_fundamentals", lambda syms, cfg: T.fake_funds(syms)), \
                 mock.patch.object(T.Q, "fetch_metrics", lambda syms, qcfg: {}), \
                 mock.patch.object(S, "datetime", FakeDT), \
                 mock.patch.dict(os.environ, env), \
                 mock.patch.object(N.requests, "post", lambda url, **kw: Resp(500)):
                assert S.main(["--force"]) == 1
        finally:
            os.chdir(old)
    print("ok  main() integration (independent of email, --no-email, failure isolation)")


if __name__ == "__main__":
    test_messages()
    test_send()
    test_main_integration()
    print("\nTELEGRAM TESTS PASSED")

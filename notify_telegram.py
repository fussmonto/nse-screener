#!/usr/bin/env python3
"""
Telegram delivery for the daily NSE trading desk.

Sends a compact, phone-friendly summary (market pulse, intraday triggers, CNC ideas, fresh breakouts) as one or
more messages, then the full HTML report as a file. Configured only through environment variables:

    TELEGRAM_BOT_TOKEN   token from @BotFather
    TELEGRAM_CHAT_ID     your chat id (comma-separate to send to several chats / groups)

If either is missing nothing is sent and nothing fails. Educational / research use only - not investment advice.
"""
from __future__ import annotations

import logging
import os
from html import escape

import requests

log = logging.getLogger("screener.telegram")
LIMIT = 3900            # Telegram allows 4096 characters per message; keep headroom for tags
API = "https://api.telegram.org/bot{token}/{method}"


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and _chat_ids())


def _chat_ids() -> list[str]:
    return [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]


def _p(x: float) -> str:
    return f"{x:,.2f}"


def _pc(x: float) -> str:
    return f"{x:+.1f}%"


def build_messages(report: dict, pages_url: str = "") -> list[str]:
    """Summary split into messages of at most LIMIT characters (HTML parse mode), split on line boundaries."""
    lines: list[str] = [f"<b>📊 NSE Trading Desk</b> · data of {escape(str(report['data_date']))}"]
    d = report.get("desk")
    mk = report.get("market") or {}
    if mk:
        lines.append(f"Nifty {mk['close']:,.0f} ({_pc(mk['chg_pct'])}) · "
                     f"{'above' if mk['above_50'] else 'below'} 50-DMA")
    if d:
        lines.append(f"<i>{escape(d['bias']['text'])}</i>")
        by = {r["label"]: r for r in d["pulse"]}
        bits = [f"{k} {_pc(by[k]['chg_pct'])}" for k in ("S&P 500", "Nasdaq", "Nikkei 225", "Brent crude") if k in by]
        if "India VIX" in by:
            bits.append(f"VIX {by['India VIX']['last']:.1f}")
        if bits:
            lines.append(escape(" · ".join(bits)))
        lines.append("")
        lines.append(f"<b>⚡ INTRADAY (MIS) · {escape(d['session'])}</b>")
        lines.append("<i>Levels to watch, not signals. Act only after the first 15-min candle breaks the trigger. "
                     "Square off by 3:15 PM.</i>")
        for side, title, word in (("long", "🟢 Long", "BUY above"), ("short", "🔴 Short", "SELL below")):
            rows = d["intraday"][side]
            if not rows:
                continue
            lines.append(f"\n<b>{title}</b>")
            for r in rows:
                q = r["size"]["qty"]
                lines.append(f"<b>{escape(r['symbol'])}</b> {word} <code>{_p(r['trigger'])}</code>\n"
                             f"  SL {_p(r['sl'])} · T1 {_p(r['t1'])} · T2 {_p(r['t2'])}"
                             + (f" · qty {q:,}" if q else "")
                             + f"\n  skip if open beyond {_p(r['skip'])}"
                             + _ev(r.get("event")))
        if d["intraday"]["skipped"]:
            lines.append("Skipped (results due): " + escape(", ".join(d["intraday"]["skipped"])))
        lines.append("")
        lines.append("<b>📦 CNC DELIVERY</b>")
        if not d["cnc"]["rows"]:
            lines.append("No full-match idea cleared the checks today.")
        for r in d["cnc"]["rows"]:
            q = r["size"]["qty"]
            lines.append(f"<b>{escape(r['symbol'])}</b> · CMP {_p(r['cmp'])} · <i>{escape(r['status'])}</i>\n"
                         f"  Buy {_p(r['buy_low'])}-{_p(r['buy_high'])} · SL {_p(r['sl'])} ({_pc(r['sl_pct'])})\n"
                         f"  T1 {_p(r['st'])} ({_pc(r['st_pct'])}) · T2 {_p(r['mt'])} ({_pc(r['mt_pct'])})"
                         f" · R:R {r['rr']:.1f}" + (f" · qty {q:,}" if q else "") + _ev(r.get("event")))
        if d["cnc"]["skipped"]:
            lines.append("Held back (results soon): " + escape(", ".join(d["cnc"]["skipped"])))
    watch = (report.get("radar") or {}).get("watchlist") or []
    if watch:
        lines.append("\n<b>👀 MY WATCHLIST</b>")
        for r in watch[:8]:
            lines.append(f"<b>{escape(r['symbol'])}</b> · CMP {_p(r['cmp'])} ({_pc(r['chg_pct'])}) · "
                         f"<i>{'In buy zone' if r['in_zone'] else 'Wait for pullback'}</i>\n"
                         f"  Buy {_p(r['buy_low'])}-{_p(r['buy_high'])} · SL {_p(r['sl'])} · "
                         f"T1 {_p(r['t1'])} · T2 {_p(r['t2'])}")
    fresh = report.get("fresh") or []
    if fresh:
        lines.append("\n<b>🚀 FRESH BREAKOUTS (top 5)</b>")
        for r in fresh[:5]:
            lines.append(f"<b>{escape(r['symbol'])}</b> {_p(r['cmp'])} ({_pc(r['chg_pct'])}) · vol {r['vol_ratio']:.1f}× · "
                         f"{escape(r['signal'])}\n  SL {_p(r['sl'])} · T {_p(r['st'])}")
    if pages_url:
        lines.append(f'\n<a href="{escape(pages_url, quote=True)}">Full dashboard</a>')
    lines.append("\n<i>Educational use only, not investment advice. Prices are the last close; verify on your broker.</i>")
    return _split("\n".join(lines))


def _ev(ev: dict | None) -> str:
    if not ev:
        return ""
    out = ""
    if ev.get("earn_days") is not None and ev["earn_days"] <= 7:
        out += f"\n  ⚠ Results {escape(ev['earn_date'])}"
    if ev.get("headline"):
        mark = {"neg": "▼", "pos": "▲"}.get(ev.get("tone", ""), "•")
        out += f"\n  {mark} {escape(ev['headline'][:90])}"
    return out


def _split(text: str, limit: int = LIMIT) -> list[str]:
    """Split on line boundaries; a stock's lines stay together because blocks are kept under the limit."""
    parts, cur = [], ""
    for ln in text.split("\n"):
        while len(ln) > limit:                       # a single absurdly long line: hard split
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(ln[:limit])
            ln = ln[limit:]
        if len(cur) + len(ln) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + ln
    if cur.strip():
        parts.append(cur)
    return parts


def _call(token: str, method: str, **kw) -> None:
    r = requests.post(API.format(token=token, method=method), timeout=40, **kw)
    if r.status_code != 200:
        # never log the URL: it contains the bot token
        try:
            desc = r.json().get("description", "")
        except Exception:  # noqa: BLE001
            desc = ""
        raise RuntimeError(f"Telegram {method} failed: HTTP {r.status_code} {desc}")


def send_report(report: dict, html: str, pages_url: str = "") -> int:
    """Send the summary and the HTML file to every configured chat. Returns the number of chats that succeeded."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = _chat_ids()
    if not (token and chats):
        log.info("Telegram not configured - skipping")
        return 0
    msgs = build_messages(report, pages_url)
    ok = 0
    for chat in chats:
        try:
            for m in msgs:
                _call(token, "sendMessage", data={"chat_id": chat, "text": m, "parse_mode": "HTML",
                                                  "disable_web_page_preview": "true"})
            _call(token, "sendDocument", data={"chat_id": chat, "caption": "Full report (open in a browser)"},
                  files={"document": (f"nse_desk_{report['data_date']}.html", html.encode("utf-8"), "text/html")})
            ok += 1
        except Exception as exc:  # noqa: BLE001 - one bad chat must not stop the others
            log.error("Telegram delivery to chat %s failed: %s", chat, exc)
    log.info("Telegram sent to %d of %d chat(s) (%d message(s) + file)", ok, len(chats), len(msgs))
    return ok

# NSE Daily Trading Desk (price-bracket screener + intraday + CNC)

Every weekday morning this project downloads NSE price history, screens liquid stocks, picks the
**Top 10 in each of 10 price brackets** (Below ₹100 … ₹901–1,000), and

* emails you an HTML report (full copy attached),
* sends a Telegram summary (optional), and
* publishes everything as **one website** on GitHub Pages (`docs/index.html`, reading `docs/latest.json`), plus the classic
  one-page report (`docs/report.html`) and a 45-day archive.

This project now also contains the former **Indian Stock Radar** (your own watchlist, market news and calculators), so there
is a single repo, a single daily job and a single site.

> **Educational / research use only. Not investment advice.** See the disclaimer in every report.

## The website (`docs/`)
One self-contained page (no CDN, works offline once loaded, installable on a phone) with ten tabs:

| Tab | What it shows | Comes from |
|---|---|---|
| Overview | Market pulse, overnight cues, key counts, top ideas from every list, headlines | both |
| Intraday (MIS) | Long and short triggers, stops, targets, "do not chase" level, quantity | screener |
| CNC delivery | Full-match delivery ideas with a buy-zone / stop / target ladder and sized quantity | screener |
| Price brackets | Top 10 in each of 10 price brackets, with rotation tags (New / Day N) | screener |
| Fresh signals | Breakouts and volume surges | screener |
| Quality 22 | 22-filter small-cap shortlist, closest misses, which filters remove most stocks | screener |
| My watchlist | Scanner, chart with 20/50 EMA, entry and exit planner, fundamentals for your own symbols | radar |
| News | Latest Economic Times and Mint headlines, tagged to your watchlist | radar |
| Tools | Position sizer, daily loss guard, copy-ready order ticket | radar |
| My book | Price alerts and buy-price tracker, checked against the last close (saved in your browser only) | radar |

Type any ticker in the search box to see **everything the site knows about it in one place** (watchlist, CNC, intraday,
bracket rank, fresh signal, quality score). Stocks show small tags when they appear on more than one list.

`docs/index.html` is hand-written and is never overwritten by the job; the job only writes `latest.json`, `report.html` and
`archive/`. To preview locally: `python -m http.server -d docs` and open http://localhost:8000.

### Your watchlist (`watchlist.json`)
```json
[{"symbol": "SUZLON", "name": "Suzlon Energy Ltd", "sector": "Renewable Energy", "mode": "CNC"},
 {"symbol": "MAHABANK", "name": "Bank of Maharashtra", "mode": "INTRADAY", "custom_buy": 63.5}]
```
`mode` is `CNC` or `INTRADAY`; `custom_buy` (optional) replaces the computed buy zone with your own price. Edit the file in
GitHub (works from a phone) and the next run picks it up. Alternatively publish a Google Sheet as CSV with columns
`Symbol, Name, Sector, Mode, My_Buy_Price` and save its URL as the repo secret `GOOGLE_SHEET_CSV_URL`; it overrides the file.
Watchlist stocks use exactly the same indicators and level maths as the bracket report, so a stock shows the same buy zone,
stop and targets everywhere. Switch it off with `RADAR=0` or `--no-radar`.

## What each row contains
Stock name + NSE ticker · CMP · buy zone · target 1–4 weeks · target 1–3 months · stop-loss · reward:risk · one-line rationale.

**CMP = previous session's close.** The job runs before the 9:15 AM open, so no live price exists yet.

## Keeping the lists fresh (no more same stocks every day)
The bracket tables rank on trend indicators that change slowly, so left alone the same leaders would sit on top for weeks.
Three things now prevent that:

* **Whole market, not just 750 stocks.** `UNIVERSE=all` (the default) screens every NSE EQ-series stock: the Nifty Total Market
  list first, then all other NSE equities. If the full list can't be downloaded it falls back to the cached copy, then to the
  Total Market list. Price history download stops after 10 minutes (`history_budget_s`) so the job always finishes; whatever is
  skipped is at the end of the list. Use `UNIVERSE=total` for the old smaller list.
* **Rotation.** The job remembers which stocks were listed on earlier days (`data/pick_history.json`, last 15 sessions). Each
  consecutive day a stock has already been listed deducts 3 points from its ranking score (max 5 days = 15 points), so repeats drop
  and new names enter. Tags show **New** or **Day N**, and a banner says how many picks are new. The score shown stays the raw score.
* **Fresh signals section.** Events, not positions: a close above the prior 52-week high on volume ≥ 1.2× average, a close above the
  prior 20-day high on volume ≥ 1.5×, or a ≥ 2% jump on volume ≥ 2.5×, only for liquid stocks above their 50 & 200 EMA with RSI ≤ 80
  and a price up to ₹1,000. Stop = 1.5 × ATR below CMP, target = 3 × ATR above (2:1). Top 15 by signal strength and volume.

Rotation and signals are about **variety and timing, not about predicting winners**. A stock being new on the list says nothing about
whether it will rise.

## Trading desk: intraday (MIS) and CNC delivery - `desk.py`
Added at the top of the email and dashboard, built from the same downloaded data:

| Section | What it is |
|---|---|
| **Market pulse** | Nifty, Bank Nifty, India VIX, S&P 500, Nasdaq, Nikkei, Hang Seng, Brent, USD/INR plus a one-line read of overnight cues (US weak/strong, VIX elevated). |
| **Intraday (MIS) watchlist** | Up to 8 long and 5 short candidates for the coming session. Liquid (20-day turnover ≥ ₹25 cr), ATR 1.5-6% of price. Each has a **trigger** (previous high/low ± 0.05 ATR), stop (0.4 ATR), Target 1 (0.6 ATR), Target 2 (1.0 ATR), a "skip if it opens beyond" level, and a sized quantity. These are *levels to watch*: nothing has fired at 8 AM. Act only if the trigger breaks after the first 15-minute candle. Square off before about 3:15 PM. |
| **CNC delivery shortlist** | Up to 10 Full-match ideas from the bracket tables with reward:risk ≥ 1.5, max 2 per price bracket, marked "In buy zone" or "Wait for pullback", with quantity sized to risk. |
| **News and results flags** | Latest headline (last 72 h, tagged ▲/▼) and next results date per candidate. A stock with results on the session date is dropped from intraday; results within 3 days drops it from CNC. |

**Position sizing.** Quantity = capital × risk% ÷ (entry − stop), capped by a max position size. Defaults: intraday ₹1,00,000, 1% risk, no leverage; CNC ₹5,00,000, 0.75% risk, ≤ 12% per stock.
Set your own under repo **Settings → Secrets and variables → Actions → Variables**: `INTRADAY_CAPITAL`, `INTRADAY_RISK_PCT`, `INTRADAY_LEVERAGE`, `CNC_CAPITAL`, `CNC_RISK_PCT`, `CNC_MAX_POSITION_PCT`. Blank = default.

Turn it off with `TRADING_DESK=0` or `python screener.py --no-desk`. If it fails, the bracket report and email still go out.

**Limits.** No live prices before the open, so no real-time signals. Yahoo news/results data is patchy for small stocks, so a missing flag does not mean no event. Short-selling is intraday only and some stocks are not shortable; check your broker's list. Mechanical, backtest-free rules: they are a starting list, not predictions.

## Telegram delivery - `notify_telegram.py`
Every run also sends a phone-friendly summary to Telegram (market pulse, intraday triggers, CNC ideas, top 5 fresh breakouts),
followed by the full HTML report as a file. It is independent of email: either can fail without stopping the other.

Setup (5 minutes):
1. In Telegram open **@BotFather** → `/newbot` → choose a name and a username ending in `bot` → copy the **token**.
2. Open your new bot and press **Start** (send it any message). Bots cannot message you first.
3. Get your **chat id**: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy `"chat":{"id": ...}`
   (or message **@userinfobot**). For a group, add the bot to the group, send a message there, and use the negative id.
4. Repo → Settings → Secrets and variables → Actions → New repository secret: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
   (several chats: comma-separate the ids).
5. Run the workflow manually once (Actions → Daily NSE Screener → Run workflow) to test.

`--no-email` also silences Telegram. Without the two secrets nothing is sent and nothing fails.

## Screening logic
| Step | Rule |
|---|---|
| Universe | Every NSE EQ stock (Nifty Total Market first) → fallback cached full list → Nifty Total Market (~750) → Nifty 500 → cached copy → built-in seed list |
| Liquidity / penny filter | Price ≥ ₹10, 20-day average traded value ≥ ₹3 crore, ≤ 2 zero-volume days in last 20 |
| Data-quality filter | Skip stocks with a >30% one-day move in the last 60 sessions (usually unadjusted splits / bad ticks) |
| Hard trend gate | Close above **both** 50-EMA and 200-EMA (never recommends downtrends) |
| Full match | Gate + RSI(14) 50–70 + last-day volume ≥ 20-day average + no fundamental red flag |
| Score (100) | Trend 30 · RSI 15 · Volume 20 · Momentum 15 · Fundamentals 20 |
| Fundamentals | Quarterly earnings growth, debt/equity (banks/NBFCs exempt), EPS > 0, ROE. Fetched only for each bracket's top 20 technical names; cached 7 days. Missing data scores neutral. |
| Ranking | Full matches first (by score), then "Partial" names. If a bracket has fewer than 10 qualifying stocks you get fewer rows - the list is never padded with weak names. |

### Levels (all ATR / pivot / Fibonacci based)
| Level | Method |
|---|---|
| Buy zone | Nearest support below CMP within 1.5×ATR (5-day pivot S1, 20/50 EMA, Fib 38.2%/50% of 60-day swing, 20-day low). Zone = support → min(CMP, support + 0.6×ATR). If CMP is extended, the zone sits below CMP = wait for a pullback. |
| Stop-loss | Buy-zone low − 1×ATR, never more than 10% below the zone mid-point |
| Target 1–4 wk | First resistance (pivot R1/R2, 60-day high, Fib extensions) between +1.5 and +3 ATR; else +2 ATR |
| Target 1–3 mo | First resistance between ST+1 ATR and +7 ATR; else +4.5 ATR |
| R:R | (Target 1 − zone mid) ÷ (zone mid − stop) |

Bracket edges are contiguous: a close of ₹100.50 falls in "₹101 – ₹200".

## Quality small-cap shortlist (22 filters) - `quality.py`
A second, **long-horizon** screen runs after the price-bracket screen and adds a *Quality small-cap shortlist*
at the top of the email and dashboard. It is meant for equal-weight, buy-and-hold research (about 10-12 names,
reviewed roughly once a year) - not for short-term trading. It has no price cap: it filters on market cap.

| # | Filter | Type |
|---|---|---|
| 1 | Market cap ₹300 - 8,000 cr | hard |
| 2 | Average daily traded value > ₹1 cr | hard |
| 3 | ROCE > 18% | hard |
| 4 | ROE > 15% | hard |
| 5 | Operating margin > 12% | hard |
| 6 | Sales growth, 3-year CAGR > 15% | hard |
| 7 | Profit growth, 3-year CAGR > 15% | hard |
| 8 | Latest-quarter profit growth > 0% YoY | hard |
| 9 | Debt/equity < 0.5 | hard |
| 10 | Interest cover > 4× | hard |
| 11 | Promoter pledging < 5% | **manual** (not in free data) |
| 12 | Operating cash flow positive last year | hard |
| 13 | Promoter holding > 40% | hard |
| 14 | FII + DII holding > 5% | hard |
| 15 | P/E < 25 (loss-making fails) | hard |
| 16 | Price above 200-day average | hard |
| 17 | PEG < 1.5 | hard |
| 18 | Operating cash flow ≥ 70% of profit (3-year) | soft |
| 19 | Free cash flow positive in ≥ 2 of the last 3 years | soft |
| 20 | Receivable days not rising sharply | soft |
| 21 | Share count up < 5% over 3 years | soft |
| 22 | Average ROCE (3-4 years) > 15% | soft |

**How the shortlist is built**
* Pre-screen on price data already downloaded (filters 2 and 16), then fetch fundamentals for the survivors only.
* **Full match** = all 16 automated hard filters and all 5 soft checks pass. **Near match** = every hard filter that has data
  passes (at most 2 may lack data) and at most 2 soft checks fail; the report says exactly which.
* A stock that *fails* a hard filter is never shortlisted; missing data is shown as "No data", never silently treated as a pass.
* Banks, NBFCs and insurers are excluded (ROCE, debt/equity and interest cover mean something different for lenders).
* Ranked by a transparent quality score (ROCE 35, sales growth 25, profit growth 25, cheapness 15); **max 2 stocks per sector**.
  Top 15 are shown. If nothing passes, the report lists the **closest misses** (stocks inside the size range that fail at most
  three hard filters) and a **filter table** showing how many stocks fail each filter and how many have no data for it, so you can
  see which thresholds are too strict or which Yahoo fields are missing before changing anything.
* Stocks trading over ₹250 crore a day are skipped (large caps cannot pass the market-cap filter) and fundamentals are fetched first
  for stocks with typical small-cap turnover.
* Buy zone and stop-loss reuse the same ATR / pivot / Fibonacci logic as the bracket screen, as an entry guide only.
* Thresholds are all in the `QualityConfig` dataclass at the top of `quality.py`. Turn the screen off with
  `QUALITY_SCREEN=0` or `python screener.py --no-quality`. If it fails for any reason the main report and email still go out.

**Data coverage.** Fundamentals come from Yahoo Finance (income statement, balance sheet, cash flow). Each run fetches for
up to 7 minutes and caches results in `data/quality_cache.json` (refreshed every 10 days), so **coverage grows over the first
few daily runs**; the report states how many candidates have data. Yahoo gives only 3-4 years of statements, so "3-year" growth
and "multi-year ROCE" use that window. Promoter and institutional holding are Yahoo approximations, and a bonus issue or
split can trigger a false "share dilution" flag.

**You must still check by hand** (the screen cannot): promoter pledging (filter 11), auditor changes or qualified opinions,
related-party dealings, and the latest annual report and two earnings-call summaries. Past screens do not predict future returns;
most of any one portfolio's result usually comes from one or two stocks.

## Setup (≈15 minutes, free)
1. **Create a GitHub repo** (github.com → New). *Public* is recommended: unlimited Actions minutes and free GitHub Pages. Private repos get 2,000 min/month (this job uses ~10 min/day ≈ 220/month) but need a paid plan for Pages.
2. **Upload this folder's contents**, keeping the structure: `screener.py`, `desk.py`, `quality.py`, `radar.py`, `notify_telegram.py`, `watchlist.json`, `requirements.txt`, `README.md`, `.gitignore`, `docs/`, `tests/`, and `.github/workflows/daily-screener.yml`. (Unzip first; drag the files into the repo page → Commit. If `.github` is hidden on your OS, enable "show hidden files".)
3. **Gmail App Password**: Google Account → Security → turn on 2-Step Verification → search "App passwords" → create one named `screener` → copy the 16 characters.
4. **Add secrets**: repo → Settings → Secrets and variables → Actions → New repository secret:

   | Name | Value |
   |---|---|
   | `SMTP_USER` | your Gmail address (the sender) |
   | `SMTP_PASS` | the 16-character App Password (no spaces) |
   | `EMAIL_TO` | where reports go (comma-separate for several) |

5. **Enable the dashboard (optional)**: Settings → Pages → Source "Deploy from a branch" → Branch `main`, folder `/docs` → Save. URL: `https://<your-username>.github.io/<repo-name>/`.
6. **Test now**: Actions tab → "Daily NSE Screener" → Run workflow. Open the run log. Check for `Universe: … symbols`, `Downloaded history for … symbols`, the 10 bracket lines, and `Email sent`. Manual runs bypass the "no new trading day" skip.
7. **Done.** It now runs Mon-Fri on its own at `30 2 * * 1-5` UTC = **08:00 IST**.

### About the timing
GitHub's scheduler runs in UTC and commonly starts 10-30 minutes late under load; the job itself takes ~8-12 minutes. Starting at 08:00 IST means the email typically lands around 08:20-08:45 IST. To shift it, edit the cron line (IST = UTC + 5:30).

### Holidays
If there has been no new trading session since the last report (market holiday), the job logs this and sends nothing.
GitHub may disable scheduled workflows in a repo with no activity for 60 days; the daily report commit normally prevents that, but if it ever happens click "Enable workflow" in the Actions tab.

## Run it locally
```bash
pip install -r requirements.txt
python tests/test_offline.py        # logic tests, no network needed (also test_desk, test_telegram, test_radar)
python screener.py --no-email       # real run; writes docs/latest.json (the site's data) and docs/report.html
python screener.py --limit 60 -v    # quick debug run on 60 symbols
python screener.py --no-quality     # skip the 22-filter quality shortlist
python screener.py --no-radar       # skip the watchlist and headlines
```
Tuning (environment variables): `UNIVERSE=all|total|500`, `MIN_TURNOVER_CR=5`, `MIN_PRICE=20`, `QUALITY_SCREEN=0`. Everything else is in the `Config` dataclass at the top of `screener.py`.

## Known limitations
* Yahoo Finance via `yfinance` is unofficial: it can rate-limit shared GitHub IPs, change formats, or serve occasional bad data. If a run fails, re-run it; if it keeps failing, upgrade `yfinance`.
* Fundamentals from Yahoo are patchy for small caps; missing values score neutral and the rationale only quotes what is known.
* NSE sometimes blocks automated downloads of its index lists. The script then falls back to the last cached list (committed in `data/`) and finally to a small seed list - check the log for "Using cached universe" or "seed list".
* Indicators are implemented directly in pandas (Wilder RSI/ATR, EMA) rather than `pandas_ta`, which has had repeated install/NumPy-compatibility breakages.
* Buy zones, targets and stops are mechanical estimates, not forecasts. Use your own position sizing and risk limits.

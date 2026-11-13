# NSE Daily Price-Bracket Screener

Every weekday morning this project downloads NSE price history, screens liquid stocks, picks the
**Top 10 in each of 10 price brackets** (Below ₹100 … ₹901–1,000), and

* emails you an HTML report (full copy attached), and
* publishes the same report as a web dashboard on GitHub Pages (`docs/index.html`, 45-day archive).

> **Educational / research use only. Not investment advice.** See the disclaimer in every report.

## What each row contains
Stock name + NSE ticker · CMP · buy zone · target 1–4 weeks · target 1–3 months · stop-loss · reward:risk · one-line rationale.

**CMP = previous session's close.** The job runs before the 9:15 AM open, so no live price exists yet.

## Screening logic
| Step | Rule |
|---|---|
| Universe | Nifty Total Market (~750) list from NSE → fallback Nifty 500 → cached copy → built-in seed list |
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

## Setup (≈15 minutes, free)
1. **Create a GitHub repo** (github.com → New). *Public* is recommended: unlimited Actions minutes and free GitHub Pages. Private repos get 2,000 min/month (this job uses ~10 min/day ≈ 220/month) but need a paid plan for Pages.
2. **Upload this folder's contents**, keeping the structure: `screener.py`, `requirements.txt`, `README.md`, `.gitignore`, `tests/`, and `.github/workflows/daily-screener.yml`. (Unzip first; drag the files into the repo page → Commit. If `.github` is hidden on your OS, enable "show hidden files".)
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
python tests/test_offline.py        # logic tests, no network needed
python screener.py --no-email       # real run; writes docs/index.html
python screener.py --limit 60 -v    # quick debug run on 60 symbols
```
Tuning (environment variables): `UNIVERSE=500`, `MIN_TURNOVER_CR=5`, `MIN_PRICE=20`. Everything else is in the `Config` dataclass at the top of `screener.py`.

## Known limitations
* Yahoo Finance via `yfinance` is unofficial: it can rate-limit shared GitHub IPs, change formats, or serve occasional bad data. If a run fails, re-run it; if it keeps failing, upgrade `yfinance`.
* Fundamentals from Yahoo are patchy for small caps; missing values score neutral and the rationale only quotes what is known.
* NSE sometimes blocks automated downloads of its index lists. The script then falls back to the last cached list (committed in `data/`) and finally to a small seed list - check the log for "Using cached universe" or "seed list".
* Indicators are implemented directly in pandas (Wilder RSI/ATR, EMA) rather than `pandas_ta`, which has had repeated install/NumPy-compatibility breakages.
* Buy zones, targets and stops are mechanical estimates, not forecasts. Use your own position sizing and risk limits.

# NSE to Telegram Bot (via NSE MCP servers)

A single Python script that pulls live NSE market data from the official NSE
MCP servers, formats a compact snapshot, and pushes it to your Telegram chat:

- **Intraday** — every 5 minutes, Monday to Friday, **09:15 to 15:30 IST**
  (NIFTY 50 / NIFTY BANK / NIFTY FIN SVC + INDIA VIX, top gainers and losers,
  market breadth).
- **Hourly module** — at 09:30, 10:30 ... 15:30 IST an extra "NSE Live
  Snapshot | Run #n" card: index breadth table with weighted advance/decline,
  weight drags/lifters and divergence flags for Nifty 50 / Bank / Fin Service /
  Midcap Select, India VIX tracked vs prev close AND vs the previous run, and
  F&O-only top-5 gainers/losers with rank changes between runs.
- **Pre-market snapshot** — once per trading day at **08:30 IST**: US levels
  (S&P/Nasdaq/Dow/futures/DXY/10Y via Yahoo), USD/INR, WTI/Brent/metals,
  India prev-session levels (NSE MCP) + Sensex, Nifty PCR (when the option
  chain is reachable; else honest "Data unavailable"), US+India economic
  calendar (ForexFactory feed, announced FOMC/RBI dates as fallback),
  last-24h commodity/trade headlines mapped to India sector impact, and a
  rule-based one-glance bias summary.
- **Phase A+B scanner** — sector ranking card (top 3 / bottom 3) + ORB signals
  and index probability model on every 5-minute tick.
- **End-of-day digest** — once per trading day after close (benchmarks,
  breadth, India VIX, sector movers, top gainers/losers, most active by
  turnover).
- Runs entirely on **GitHub Actions** (free for public repos) — no server
  needed.
- **Silent on weekends and NSE holidays** — built-in 2026 holiday calendar
  plus a data-staleness check that catches any unlisted non-trading day.

Data sources (public, no API key):

| Server | Endpoint | Used for |
| --- | --- | --- |
| NSE Live (cm-market-mcp) | `https://mcp.nseindia.in/cmmkt/mcp` | index quotes, India VIX, top gainers/losers |
| NSE Bhavcopy (nse-bhavcopy-redis-mcp) | `https://mcp.nseindia.in/bhavcopy/cm/mcp` | market breadth, mood/VIX, sector movers, EOD movers, most active |

---

## Repository layout

```
.
├── nse_bot.py                    <- snapshot bot + entry point
├── scanner.py                    <- Phase A+B intraday scanner
├── hourly.py                     <- hourly breadth / VIX / F&O movers card
├── premarket.py                  <- 08:30 IST pre-market macro snapshot
├── indices_data.py               <- index constituents + weights (embedded)
├── universe.py                   <- embedded F&O sector map + index weights
├── requirements.txt              <- requests + tzdata
├── .env.example                  <- template for local runs
├── .gitignore
├── state/state.json              <- scanner runtime state (committed back
│                                    by the workflow after every run)
├── state/hourly_state.json       <- hourly module run memory (same commit)
├── state/premarket.json          <- premarket once-per-day dedupe
└── .github/workflows/
    ├── premarket.yml             <- cron: 08:30 IST once per weekday
    ├── intraday.yml              <- cron: every 5 min, Mon-Fri, 09:15-15:30 IST
    └── eod.yml                   <- cron: 16:05 IST once per weekday
```

---

## Step 0 — Prerequisites

- A GitHub account (free plan is fine).
- Python 3.10+ installed locally if you want to test first (optional).
- Telegram installed on your phone or desktop.

---

## Step 1 — Create the Telegram bot and get your chat ID

### 1a. Create the bot (get the token)

1. Open Telegram and search for **@BotFather** (verified, blue tick).
2. Send `/newbot` and follow the prompts:
   - Name: e.g. `NSE Market Snapshot`
   - Username: must end in `bot`, e.g. `nse_snapshot_bot`
3. BotFather replies with a token like
   `123456789:AAH4x...8zo`. **Copy it and keep it private.**
4. Recommended: send `/setuserpic`, or `/setdescription` ("NSE market
   snapshots every 5 minutes").
5. Important: open a chat with your new bot and press **Start** (bots can
   only message people who started a conversation with them first).

### 1b. Get your chat ID

Easiest method (no code needed):

1. In the browser, open (replace `<TOKEN>` with your bot token):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
2. Send any message (e.g. "hi") to your bot in Telegram.
3. Refresh the browser page. In the JSON, find:
   `"chat":{"id": 123456789, ...}` — that number is your **chat ID**.
   (For a group chat the id looks like `-100xxxxxxx` — use it as-is.)

Alternative: `https://web.telegram.org/k/` — open your bot's chat; the URL
shows `#-123456789` which is the chat ID.

### 1c. Verify the bot can message you

Paste this in a browser (replace `<TOKEN>` and `<CHAT_ID>`):

```
https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>&text=hello
```

If "hello" lands in your Telegram, Step 1 is done.

---

## Step 2 — Optional but recommended: test locally before pushing

```bash
cd nse-telegram-bot
pip install -r requirements.txt

# See exactly what would be posted, without any Telegram setup:
python nse_bot.py --mode intraday --dry-run --force
python nse_bot.py --mode eod      --dry-run --force
```

`--force` bypasses the market-hours/holiday gates (so it works on weekends),
`--dry-run` prints the message instead of sending it.

To do a real end-to-end send from your machine:

```bash
export TELEGRAM_BOT_TOKEN="123456789:AAH..."
export TELEGRAM_CHAT_ID="123456789"
python nse_bot.py --mode intraday --force
```

---

## Step 3 — Put the code on GitHub

1. On github.com click **New repository**.
   - Name: `nse-telegram-bot`
   - Visibility: **Public** is recommended (Actions minutes are unlimited
     for public repos; a private repo consumes free-tier minutes, see Step 6).
   - Do **not** initialise with a README (the files are already here).
2. Push the project:

```bash
cd nse-telegram-bot
git init
git add .
git commit -m "NSE MCP to Telegram bot"
git branch -M main
git remote add origin https://github.com/<YOUR_USERNAME>/nse-telegram-bot.git
git push -u origin main
```

(When prompted, use a **Personal Access Token** as the password — GitHub no
longer accepts account passwords: GitHub -> Settings -> Developer settings ->
Personal access tokens -> Tokens (classic) -> Generate, tick `repo`.)

---

## Step 4 — Add the two secrets

In your repo: **Settings -> Secrets and variables -> Actions -> New
repository secret**. Add both:

| Name | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | the BotFather token (`123456789:AAH...`) |
| `TELEGRAM_CHAT_ID` | your chat ID (`123456789` or `-100...`) |

Optional: `TELEGRAM_THREAD_ID` if you want posts inside a forum-topic of a
group.

Secrets are encrypted; they are never printed in workflow logs.

---

## Step 5 — Enable Actions and run a manual test

1. In the repo open the **Actions** tab. If you see a banner "Workflows
   aren't being run on this repository", click **I understand my workflows,
   go ahead and enable them**.
2. Left sidebar shows two workflows: **NSE intraday snapshot** and **NSE
   end-of-day digest**.
3. Click **NSE intraday snapshot -> Run workflow -> Run workflow**.
   (Manual runs automatically pass `--force`, so they send a message even on
   weekends — perfect for testing.)
4. Watch the run turn green, then check Telegram: you should have received
   the live snapshot card.

If the run is red, open it and read the `[HH:MM:SS IST]` log lines — see
Troubleshooting below.

---

## Step 6 — Go live (automatic schedule)

Nothing more to do — the two cron schedules are already in the workflow
files and start firing as soon as Actions is enabled.

| Workflow | Cron (UTC) | IST | Fires |
| --- | --- | --- | --- |
| intraday.yml | `45,50,55 3 * * 1-5` | 09:15 / 09:20 / 09:25 | opening ticks |
| intraday.yml | `*/5 4-9 * * 1-5` | 09:30 ... 15:25 every 5 min | main session |
| intraday.yml | `0 10 * * 1-5` | 15:30 | closing tick |
| eod.yml | `35 10 * * 1-5` | 16:05 | end-of-day digest |

How the timing is kept correct:

- Cron runs in **UTC**; the script converts to **IST** (`Asia/Kolkata`) and
  re-validates the 09:10-15:35 window before sending, so GitHub's documented
  cron delays (up to a few minutes) can never post outside market hours.
- On weekends the workflow still triggers, but the script exits immediately
  (exit code 0, "SKIP" in the log) — a run without a message is normal.
- On NSE holidays listed in `NSE_HOLIDAYS` inside `nse_bot.py` the script
  exits silently. A data-staleness guard additionally blocks sending whenever
  the live data timestamp is not from today, catching any unlisted special
  holiday.
- The EOD job polls the bhavcopy MCP for today's file (NSE publishes it a
  few minutes after close) and sends the digest as soon as it appears; if
  today's file never appears by 17:30 IST it stays silent (holiday safety).

### Costs and limits

- **Public repo**: GitHub Actions is free and unlimited for this workload
  (76 runs/day, each ~30-60 seconds).
- **Private repo**: free tier includes 2,000 Actions-minutes/month. This
  workload uses roughly 1 minute x 76 runs x 21 trading days ≈ **1,600
  min/month**, which just fits but leaves little headroom — prefer a public
  repo, or reduce runs by removing the `45,50,55 3` cron line if you do not
  need the 09:15-09:25 ticks.
- GitHub auto-disables scheduled workflows after **60 days of repository
  inactivity**. Any commit (even editing this README) re-arms them; GitHub
  emails you before it happens.

---

## Customisation

All in `nse_bot.py` (or via environment variables / secrets without code
changes):

| What | How |
| --- | --- |
| More/fewer stocks per list | secret or env `TOP_N=10` |
| Post into a forum topic | secret `TELEGRAM_THREAD_ID` |
| Different indices in the card | edit the `want` dict in `fetch_live_card` |
| Benchmark list in the digest | edit `want` in `build_eod_message` |
| Message style | edit `build_intraday_message` / `build_eod_message` |
| 2027 holidays | add the new dict to `NSE_HOLIDAYS` when NSE publishes it |

### Fallback: run on your own server instead (optional)

The same script runs anywhere; only the scheduler changes. Crontab
( IST-safe because the script re-checks its own window):

```cron
*/5 9-15 * * 1-5 cd /opt/nse-telegram-bot && /usr/bin/python3 nse_bot.py --mode intraday >> bot.log 2>&1
35 16 * * 1-5    cd /opt/nse-telegram-bot && /usr/bin/python3 nse_bot.py --mode eod >> bot.log 2>&1
```

Set the two environment variables via a `.env`-style wrapper or
`/etc/environment`.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `SKIP: outside intraday window` in a scheduled run | Normal before 09:15 / after 15:35 IST or on weekends. GitHub cron delays can also drop the last tick inside a window boundary — harmless. |
| `SKIP: NSE holiday today ...` | Working as intended. |
| Red run with `Telegram send failed: ... chat not found` | Wrong `TELEGRAM_CHAT_ID`, or you never pressed Start in the bot chat. Group ids are negative (e.g. `-100...`). |
| Red run with `Unauthorized` | Wrong bot token in `TELEGRAM_BOT_TOKEN`. |
| Red run with `FAIL: MCP error: request failed` | NSE MCP endpoint temporarily down or unreachable from GitHub's runners. The script already retries 3x; the next 5-minute run will retry again. |
| `SKIP: live data is from ...` on a trading day before 09:45 | First ticks can arrive while NSE still serves the previous session timestamp; the next cycle sends as usual. |
| `::warning::state push failed after retries` | Another process pushed to `main` mid-run. The next 5-minute run continues from the last committed state — harmless unless it repeats every run. |
| `! [rejected] ... fetch first` when you push locally | The bot commits `state/state.json` to `main` every 5 minutes. Always run `git pull --rebase origin main` before pushing local edits. |
| EOD digest shows "(latest published session)" | Today's bhavcopy was not yet ingested when the digest was built; date is always labelled. |
| No runs at all in the Actions tab | Workflows were disabled or the repo has been inactive 60+ days; re-enable in Actions tab, or push any commit. |

---

## Notes

- The MCP tool descriptions state data is for **educational/research
  purposes only** and not investment advice — the same disclaimer is included
  in every message.
- Live MCP data refreshes roughly every 1-5 minutes at the source, which
  matches the 5-minute cadence well.
- `nse_bot.py` never writes secrets to logs and never stores anything
  beyond process memory; there is no database to maintain.

## v2 - Phase A+B intraday scanner (added 23 Nov 2026)

Every 5-minute run now also runs the scanner (`scanner.py`, universe in `universe.py`):

- **Sector ranking** - relative % vs NIFTY, opening-relative %, 3-bar fractal
  structure score (HH/HL/LH/LL), breadth (% of sector F&O members green),
  composite `0.5*z(rel) + 0.3*structure + 0.2*z(breadth)`. Top/bottom-3 buckets
  change only after 2 consecutive scans confirm. Table is sent once after 09:30
  and whenever the confirmed top/bottom-3 changes.
- **ORB stock signals** - F&O stocks of long/short sectors only; close beyond OR
  edge, breakout-bar volume >= 1.2x the average of the 10 bars *before* it (a
  fresh volume bar is required - stale breakouts do not re-fire), above/below
  snapshot VWAP, outperforming its sector, entry within 0.5x OR range of the
  edge, risk <= 1.2%, max 5 new signals per scan ranked by sector score x
  volume ratio. SL = OR midpoint, T1 = 1R, T2 = 2R; T1/SL/T2 hits arrive as
  threaded replies to the original signal message.
- **Index model** - NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY (SENSEX dropped:
  BSE not on NSE MCP). Factors: mover contribution (0.25, embedded approximate
  weights, coverage reported), ORB (0.20), flow proxy (0.20, 5-minute momentum
  - no OI or depth on MCP), VWAP + day-range position (0.10), sector alignment
  (0.05). Option-chain factor (0.20) has no MCP tool and is renormalised out.
  Signal at P >= 65% AND index ORB broke in the same direction; re-alert only
  on a >=10pp probability swing vs the last alerted level. Index signals carry
  SL/T1/T2 and are tracked like stock signals.
- **BTST 15:15** - up to 3 stocks: sector bucket, close in top/bottom 25% of day
  range, vs VWAP and OR edge, volume pace >= 1.3x 10-day avg (bhavcopy MCP),
  corporate-action filter via `get_corporate_actions`, day gain < 4%. Ban list /
  futures OI / global cues not available - stated in the message.
- **Dedupe & throttle** - key `date|symbol|direction|setup` sent once per day;
  T1/SL/T2 updates arrive as threaded replies to the original message; scans
  with no change send nothing.
- **State** - `state/state.json` carries the day's 5-minute snapshots, OR boxes,
  buckets, signals and outcomes between runs. Actions runners are ephemeral, so
  the intraday workflow declares `permissions: contents: write` and commits the
  updated state file back to the repo after every run (visible in the commit
  history; no cache-expiry surprises). The opening range forms only if the
  day's first snapshot lands by 09:19 IST (cron-delay tolerant); otherwise ORB
  is skipped that day and the message says so.
- **Verified against the live feed (23 Nov 2026)**: GUJGASLTD, ICICIPRULI and
  LTIM are absent from `cm_get_equity_stocks` (feed omission) and TATAMOTORS
  was renamed TMPV - all four are skipped gracefully; every other embedded
  symbol resolves. Each letter call stays far below the tool's 500-row cap.

Honest limitations (stated inside every message): option chain, futures OI,
F&O ban list, contract master (expiry/lot size) and market depth have no tool
on the two NSE MCP endpoints; candles/VWAP/OR are approximations built from
5-minute endpoint snapshots. Sector membership and index weights are embedded
approximations - update `universe.py` when NSE revises the F&O list.

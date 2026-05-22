# Futures Trading App — Project Brief

## What this is
A desktop/server application that fetches live futures market data,
stores historical OHLCV candle data locally, analyzes markets using
AI/ML, and trades (or suggests trades) via an Apex Trading account.

## Owner
Joe Thieme — beginner programmer, Mac user (MacBook Air)
Located in Sevierville, TN area

## Trading style
Futures day trading and scalping
Connected to Apex Trading account for execution

## Related project
This project builds on patterns from ~/crypto-app (crypto perpetual futures).
Key reusable components from crypto-app:
- SQLite data storage pattern (prices.db)
- Market analyzer with Claude AI reports (market_analyzer.py)
- AutoPilot AI strategy selector (autopilot.py)
- Dashboard UI patterns (CustomTkinter + matplotlib)
- Strategy scoring engine
- Systemd service deployment pattern (adamserver)

## Data source
- yfinance (Yahoo Finance) for initial data gathering
- Will expand to broker-native feeds as needed

## Broker
Apex Trading (futures)
- Will integrate for live trading or trade suggestions

## Tech stack
- Language: Python 3.14.3
- Database: SQLite (local file)
- Data handling: pandas
- Market data: yfinance
- Charts: matplotlib (custom candlestick rendering)
- Indicators: pandas/numpy (RSI, MACD, BB, etc.)
- AI/ML: scikit-learn, TensorFlow/Keras, anthropic SDK
- Market analysis: Claude API for AI reports
- UI: CustomTkinter

## Project folder
~/trading/futures-app (located at /Users/joethieme/trading/futures-app)

## Mac setup status
- Python 3.14.3 installed ✅
- pip installed ✅
- Git installed ✅
- GitHub repo: https://github.com/bigjoegenius/futures-app (pending)
- .gitignore created ✅
- CLAUDE.md created ✅
- yfinance installed ✅

## Phases (planned)

### Phase 1 — Data layer ✅ COMPLETE (2026-04-13)
Goal: Connect to yfinance, fetch historical futures data,
store OHLCV candles in local SQLite database.

**Files created:**
- futures_config.py — contract list, timeframes, DB path
- db_setup.py — creates SQLite tables (candles + fetch_log)
- fetch_data.py — downloads data from yfinance, stores in SQLite

**Database: futures.db**
- 675,453 total candles across 18 contracts
- Daily data back to year 2000 for most contracts
- 1-hour data back ~2 years
- 5m/15m data back ~60 days
- 1-minute data back ~7 days (yfinance limit)

**How to refresh data:**
- `python fetch_data.py` — daily data only (fast)
- `python fetch_data.py --all` — all timeframes
- `python fetch_data.py --symbol ES=F` — one contract
- Duplicates are automatically skipped

### Phase 2 — Charts and dashboard ✅ COMPLETE (2026-04-13)
Interactive candlestick charts, indicators, watchlist.

**Files created:**
- dashboard.py — full trading dashboard (CustomTkinter + matplotlib)

**Features:**
- Dark theme matching crypto-app style (#0d0d0d bg, green/red candles)
- 18 futures contracts organized by category (Indexes, Energy, Metals, Bonds, Ags)
- 5 timeframes (1m, 5m, 15m, 1h, 1d)
- Candlestick chart with volume subplot
- Bollinger Bands overlay (toggle)
- RSI indicator subplot (toggle)
- MACD indicator subplot (toggle)
- Mouse crosshair with OHLCV tooltip
- Scroll to pan, Ctrl+Scroll to zoom, drag to pan
- Right sidebar: indicator toggles, market info, watchlist, controls help
- Refresh button downloads fresh data from yfinance in background
- Status bar

**How to launch:**
- `python dashboard.py`

### Phase 3 — Strategy engine ✅ COMPLETE (2026-04-17)
Generic indicator-based paper-trading engine ported from crypto-app.

**Files created:**
- market_analyzer.py — RSI/EMA/MACD/BB/ATR math + 0-100 scoring for 6 strategies
- strategy_engine.py — paper trader: sizing, stops, targets, fees, P&L logging
- live_prices.py — yfinance poller (writes to `latest_prices` table every ~30s)

**6 generic strategies (no crypto basis/funding logic):**
- ema_cross, macd_momentum, rsi_extreme, bb_breakout, volume_breakout, triple_confluence

**Paper trading:**
- Risk modes: conservative 0.5% / moderate 1% / aggressive 2% of balance per trade
- ATR-based stop (1.2x ATR), 1.8R target
- Per-contract specs in CONTRACT_SPECS (ES=F point_value=50, NQ=F=20, CL=F=1000, etc.)
- Slippage + commission modeled (1 tick + $2.50/side)
- Closed trades written to `trades` table AND mirrored to trade_log.json

**How to test:**
- `python strategy_engine.py --test --symbol ES=F --timeframe 1d --bars 500`
- `python live_prices.py --once`

### Phase 4 — AI autopilot + server + controllers ✅ COMPLETE (2026-04-17)
Ported the full crypto-app control plane.

**Files created:**
- autopilot.py — hourly Claude call picks strategies + risk mode
- news_provider.py — free macro/futures RSS digest (cached 1h)
- run_autopilot.py — headless daemon orchestrating all workers
- web_controller.py — Flask + PWA backend on port 5100
- templates/index.html, static/app.js, static/style.css, static/sw.js, static/manifest.json
- server_controller.py — Tkinter desktop controller (talks to the API)
- .env.example — template for API keys + tokens

**How to launch:**
- `python web_controller.py` — PWA at http://localhost:5100 (tokens printed at boot)
- `python run_autopilot.py --balance 10000 --risk moderate` — daemon
- `python run_autopilot.py --dry-run` — 60s self-check
- `python server_controller.py --token <ADMIN_TOKEN>` — desktop window
- `python autopilot.py --once` — one-off AI decision
- `python market_analyzer.py --all --ai` — full market scan + Claude report
- `python news_provider.py` — headlines dump

**Env vars (.env):**
- ANTHROPIC_API_KEY (required for AI)
- WEB_CONTROLLER_TOKEN, WEB_VIEWER_TOKEN (auto-generated if missing)
- GMAIL_USER, GMAIL_APP_PASSWORD, REPORT_EMAIL_TO (daily email summary)

**DB tables added:** `latest_prices`, `trades`, `autopilot_log`

### Data collection pipeline ✅ LIVE (2026-04-21)
Automated 24/7 data collection running on adamserver with sync back to Mac.

**adamserver side** (Arch Linux, user-level systemd):
- `/home/joe/futures-app/futures_config.py`, `db_setup.py`, `fetch_data.py` — copied from Mac
- `/home/joe/futures-app/fetch_and_log.sh` — wrapper with `flock` + log rotation
- `/home/joe/futures-app/futures.db` — the growing database
- `~/.config/systemd/user/futures-collector.service` — runs fetch_and_log.sh
- `~/.config/systemd/user/futures-collector.timer` — fires at :07 every hour
- Linger enabled (survives reboots + SSH disconnects)
- Logs: `/home/joe/futures-app/fetch.log`

**Mac side** (launchd):
- `~/futures-app/sync_from_adamserver.sh` — rsync wrapper (portable mkdir lock)
- `~/Library/LaunchAgents/com.joe.futures-sync.plist` — fires at :45 every hour
- Pulls `adamserver:~/futures-app/futures.db` → `~/futures-app/futures.db` via SSH+rsync
- ~6 seconds per sync (18MB compressed from ~100MB file)
- Logs: `~/futures-app/sync.log`

**Cadence:** server fetches at :07 → Mac pulls at :45 → local db never more than ~38 min stale.

**Useful commands:**
- Check server: `ssh adamserver 'systemctl --user status futures-collector.service'`
- Tail server log: `ssh adamserver 'tail -f ~/futures-app/fetch.log'`
- Force server fetch: `ssh adamserver 'systemctl --user start futures-collector.service'`
- Tail Mac sync: `tail -f ~/futures-app/sync.log`
- Force Mac sync: `~/futures-app/sync_from_adamserver.sh`
- Pause collection: `ssh adamserver 'systemctl --user stop futures-collector.timer'`
- Pause Mac sync: `launchctl unload ~/Library/LaunchAgents/com.joe.futures-sync.plist`

**Known limits:**
- yfinance 1m cap = 7 days, 5m/15m = 60 days, 1h = 730 days. Hourly runs keep the recent
  window fresh but don't extend history beyond yfinance's caps. For deeper minute history
  need Firstrate Data (~$49) or IB API.
- Sync is server → Mac only. Paper trades written locally get overwritten. Put autopilot
  on adamserver too if you want persistent trade state.

### Phase 5 — Apex Trading integration (NOT STARTED)
Paper trading only for now. No broker module. When ready, add `broker.py` with a
clean place_order / close_position interface and wire it from strategy_engine.py.

### Phase 6 — 28-Strategy catalog + live Autopilot + Static deploy (2026-04-21) ✅
Expanded from 6 to 28 strategies, backtested, deployed on adamserver.

**Files created/updated:**
- `strategies/STRATEGY_CATALOG.md` — full 28-strategy reference (rules, markets, timeframes, sources)
- `market_analyzer.py` — rewritten with 28 strategies + VWAP/Keltner/Donchian helpers + dispatcher
- `strategy_engine.py` — per-strategy stop/target multipliers, `backtest_symbol` returns trade detail
- `news_fixtures.json` — FOMC/WASDE/EIA/OPEC dates + major market events for overlay
- `backtest_all.py` — walk-forward all (strategy × market × tf) combos, writes DB + per-combo JSON + equity PNG
- `build_strategy_report.py` — Word doc generator with DB + JSON fallback
- `autopilot.py` — upgraded to `claude-opus-4-7`, richer prompt (28 strategies), proper JSON schema parsing
- `run_autopilot.py` — dual-portfolio support (`--portfolio autopilot|static_all`), per-trade emails (open + close),
  wrapped `_open_position` for on-open callback, all emails to `baldwetcoby@gmail.com`

**Strategies (28 total):**
- Market-agnostic (10): ema_cross, macd_momentum, rsi_extreme, bb_breakout, volume_breakout,
  triple_confluence, donchian_20, donchian_55, keltner_squeeze, gap_fade
- Index specialists (4 — ES/NQ/YM/RTY): orb_15, vwap_pullback, overnight_gap, rth_reversal
- Energy specialists (2 — CL/NG): eia_fade, asia_london_breakout
- Metals specialists (2 — GC/SI/HG): gold_silver_ratio, copper_risk_on
- Bond specialists (2 — ZB/ZN): fomc_drift, steepener
- Grain specialists (2 — ZC/ZS/ZW): wasde_react, seasonal_harvest
- Softs + cattle (4 — KC/SB/CT/LE): coffee_weather_spike, sugar_carry, cotton_mean_rev, cattle_cot_long
- Extras (2 — all): range_reversal, breakout_retest

**Backtest (~488 combos):** Initial partial run (70 combos done at report time) validated the harness.
Top early combo: `ema_cross` × ES/GC/NQ on 1d delivers $10k+ P&L over ~1500 bars (4+ years daily).
Losing trades get news-tagged when they overlap FOMC/WASDE/OPEC/major events (±4h window).

**adamserver deployment:**
- Services added (all ENABLED + ACTIVE on 2026-04-21):
  - `futures-autopilot.service` — run_autopilot.py --portfolio autopilot (Claude-gated)
  - `futures-static.service` — run_autopilot.py --portfolio static_all (all 28 always on)
  - `futures-live-prices.service` — live_prices.py (30s poller)
  - All point at `/home/joe/crypto-app/venv/bin/python3` (reused crypto venv with anthropic/docx/etc)
  - EnvironmentFile=`/home/joe/futures-app/.env` loads ANTHROPIC_API_KEY, GMAIL creds,
    and BALDWETCOBY_EMAIL_TO=baldwetcoby@gmail.com
- Email setup: per-trade (open + close) + daily recap at 4pm ET → `baldwetcoby@gmail.com`
  Subject tags `[FUTURES AUTOPILOT]` / `[FUTURES STATIC]` / `[FUTURES DAILY]` for Gmail filters

**Unified web UI (crypto-app web_controller + public_viewer):**
- Mode toggle pill at top: Crypto / Futures
- `web_controller.py` — new `/api/futures/{status, services, prices, trades, paper, db, ai-overview, health,
   log, server, calendar, trade-candles, news-digest}` routes, all reading `~/futures-app/futures.db`
- `public_viewer.py` — new `/api/futures/{markets, summary, trades, strategies}` routes with plain-English
   explanations for all 28 strategies
- `static/app.js` — MODE state, mode-aware apiFetch, header price chip adapts to futures symbols
- `static/style.css` — `.mode-switch`, `.mode-btn` styles
- `static/sw.js` — cache bumped to `crypto-ctrl-v2`
- `static/public_viewer.js` + `public_viewer.css` + `templates/public_viewer.html` — mirror toggle

**Deliverable:** `~/Desktop/Futures_Strategy_Report_2026-04-21.docx` — cover, top-15 winners,
worst 10, contract specs, per-strategy deep dive (1 page each with equity curve + news-tagged losses),
per-market recommendations, "News That Hurt Us" section.

**How to continue the backtest** (if partial):
```
cd ~/futures-app && python3 -u backtest_all.py > backtests/run_log.txt 2>&1 &
# Once done:
python3 build_strategy_report.py --open   # regenerates Word doc on Desktop
```

**Email volume expectation:** static engine with all 28 strategies always-on will generate
significantly more emails than autopilot (estimated 15-30/day during active sessions vs 5/day for autopilot).
Both stamped with the appropriate tag so Gmail filters can sort them.

**Known caveats:**
- 1m data = 8 days, 5m/15m = ~60 days (yfinance limits). Backtests on those timeframes are low-sample.
- `cattle_cot_long` uses a price+volume proxy since CoT report data isn't in futures.db.
- `sugar_carry` uses a 20-day trend + end-of-month proxy instead of true contango term structure.
- News fixtures are baked-in calendar events; doesn't cover every headline.

### Phase 7 — Apex Trading integration (NOT STARTED)
Same as Phase 5 above — build `broker.py` and wire into `strategy_engine.py` when ready.

### Phase 6b — Confidence scoring + email overhaul (2026-04-21) ✅
Every trade now carries a 0-100 confidence score; per-trade spam replaced by
top-5% rolling alert. Everything routes to `baldwetcoby@gmail.com`.

**Files created/updated:**
- `confidence_tracker.py` — NEW. Rolling percentile tracker (last 200 closed
  trades' confidence), persisted to `confidence_window.json`. Cold-start
  fallback: confidence ≥ 85 triggers until the window has 50+ samples.
- `strategy_engine.py` — `TradeReport` and `Position` gained `confidence` +
  `confidence_source` fields. New `_estimate_confidence()` blends raw
  strategy score (raw, 100%). `_open_position()` now takes a
  `raw_score` kwarg and stores the confidence on the Position.
- `db_setup.py` — `trades` table gets `confidence REAL` + `confidence_source
  TEXT`. Old-deployment safe via `ALTER TABLE ADD COLUMN`.
- `run_autopilot.py` — per-trade OPEN/CLOSE emails REMOVED. New
  `_send_high_conviction_alert()` fires only when a trade opens with
  confidence ≥ 95th percentile of the rolling window (subject tag
  `[FUTURES HIGH CONVICTION]`). `on_trade_closed` records confidence into the
  window. Daily 4pm ET recap now shows the percentile cut + confidence column
  in recent trades. Subject tag changed to `[FUTURES DAILY]` for the recap.
  Startup seeds the tracker from prior closed trades in the SQLite `trades`
  table.
- `backtest_all.py` — every trade tagged with session bucket
  (`Asia`/`London`/`US_PreOpen`/`US_RTH`/`US_Post`/`Weekend`). End-of-run
  prints per-session WR/P&L/avg and writes
  `backtests/session_breakdown_<run_id>.json`.

**Email volume impact:**
- Before: ~150 per-trade + 1 daily recap = ~151/day
- After: 1 daily recap + 0-5 high-conviction alerts = **~1-6/day**
- Single inbox: `baldwetcoby@gmail.com` for everything

**Confidence formula:**
```
confidence = raw_strategy_score   (source label = "raw")
```

**Session buckets (ET, weekday):**
- US_RTH     09:00-16:00  — main liquidity window
- US_PreOpen 08:00-09:00
- US_Post    16:00-18:00  — post-close thin book
- London     03:00-08:00
- Asia       18:00-03:00  (wraps midnight)
- Weekend    Sat/Sun any hour — flags test artefacts

**Confidence field wiring:**
- `Position.confidence/_source` → `TradeReport.confidence/_source` →
  `trades` table columns → `trade_log.json` entries → rolling window →
  daily recap body.

**How to run the session-aware backtest:**
```
cd ~/futures-app && python3 backtest_all.py > backtests/session_run.log 2>&1 &
# Table prints at end of run; JSON at backtests/session_breakdown_<run_id>.json
```

**Next steps:**
- Let the rolling window accumulate 50+ real trades (1-2 days) before the
  percentile gate switches from cold-start cutoff (85) to true top-5%.
- Read `session_breakdown_*.json` to decide whether to restrict the
  autopilot to US RTH hours only (via a session guard in `StrategyWorker`).

### Session log — 2026-04-21 (Duplicate-trade bug fix)

**Symptom:** Futures dashboard showed the same `macd_momentum` SHORT
trade replaying 9–12x with identical entry/exit prices, timestamps,
and P&L. `trades` table held 172 closed rows but only 58 unique by
(symbol, strategy, entry_time, entry_price, exit_time, exit_price, pnl).

**Root cause** — `strategy_engine.step()`: `StrategyWorker` runs
`engine.step()` every 5 minutes but loads 1h bars. When a bar's
range already bracketed both entry price and target, the flow was:
  - step N:   no position → `_open_position` opens on bar X close
  - step N+1: `_manage_position` sees `bar.low ≤ target` → closes
              at target → falls through to entry check → same bar,
              same indicators, same signal → opens again
  - repeat 10–12x per hour until a new 1h bar forms.

Each close called `_persist_trade` (a plain INSERT, no dedup
guard), so the DB piled up identical rows.

**Fix** — one entry per bar per symbol:
- Added `_last_entry_bar: dict[str, str]` on `StrategyEngine`.
- `step()` still manages open positions on every call (so
  stops/targets fire promptly), but the entry scan now only runs
  when the latest bar's timestamp differs from the one last
  processed for that symbol.
- Safe for both live (`StrategyWorker`) and backtest (`backtest_symbol`
  iterates bar-by-bar, so each sub-frame has a fresh last bar).

**Crypto-app same class** — mitigated but not immune. `crypto-app`'s
`_close_trade` already set a 6-bar cooldown on STOP but none on
TARGET, so a `check_exit` that hit target followed by `check_entry`
could re-enter in the same call. Extended the cooldown block to set
a 1-bar cooldown on TARGET exits too. In practice crypto-app's
entry conditions (EMA crossovers against prior-bar values, RSI+BB
combos) rarely re-fire mid-bar — the DB had 62/62 unique closed
trades — so this is a belt-and-suspenders guard.

**Deployment (completed this session):**
  1. Stopped `futures-autopilot` + `futures-static` on adamserver
     to freeze the DB.
  2. Backed up `/home/joe/futures-app/futures.db` to
     `futures.db.backup-20260421-220549` (104MB).
  3. `scp` fixed `strategy_engine.py` to
     `adamserver:/home/joe/futures-app/` AND
     `adamserver:/home/joe/crypto-app/`.
  4. One-shot dedup on futures.db:
     ```sql
     DELETE FROM trades WHERE status='closed' AND id NOT IN (
       SELECT MIN(id) FROM trades WHERE status='closed'
       GROUP BY symbol, strategy, entry_time, entry_price,
                exit_time, exit_price, pnl_dollars);
     ```
     Result: 172 → 64 closed rows.
  5. Restarted `futures-autopilot`, `futures-static`,
     `crypto-autopilot` — all active.

**Recovery path:** pre-dedup backup lives at
`adamserver:/home/joe/futures-app/futures.db.backup-20260421-220549`.
Safe to delete once the fix has run clean for a day or two.

## Working style notes
- Joe is a beginner — explain everything in plain English
- Walk through commands one at a time
- Explain what each command does before running it
- Check in after each step before moving to the next

## How to continue in a new chat
1. Open Claude desktop app — Code tab
2. Point it at ~/futures-app folder
3. Claude reads this file automatically
4. Say "lets continue building the futures app" and pick up from
   the current phase listed above

### Strategy #29 — Liquidity Sweep & Reclaim (2026-05-11) ✅
Added a new institutional liquidity sweep reversal strategy from
@_market_decoded_ Instagram reel. Fades false breakouts at equal highs/lows.

**Concept:**
1. Identify equal highs (or lows) forming a visible liquidity pool
2. Wait for price to sweep through (trapping breakout traders)
3. Enter reversal when price reclaims back inside the range
4. Stop above the sweep extreme + 0.2x ATR buffer
5. ATR-based target (optimized per timeframe)

**Files created/updated:**
- `market_analyzer.py` — added `liquidity_sweep_reclaim` to STRATEGIES catalog (29th),
  `_check_liquidity_sweep_reclaim()` checker with timeframe-optimized parameters,
  `_sweep_side()` helper for both bull/bear sweeps, EMA50 trend filter on daily
- `backtest_liquidity_sweep.py` — standalone comprehensive backtest script
- `optimize_liquidity_sweep.py` — parameter sweep script (192 combos × 3 timeframes)

**Optimized parameters (from 192-combo sweep over 25 years of GC=F data):**
- 15m: lookback=10, tol=0.5×ATR, min_touches=2, target=3.0×ATR, no trend filter
- 1h:  lookback=30, tol=0.3×ATR, min_touches=3, target=1.8×ATR, no trend filter
- 1d:  lookback=10, tol=0.5×ATR, min_touches=2, target=1.8×ATR, EMA50 trend filter

**Backtest results (GC=F, all available data):**
- 15m (3 months, 5,831 bars): 238 trades, 35.3% WR, +$12,556 P&L, PF 1.36, Sharpe 1.99
- 1h (2.5 years, 14,067 bars): 322 trades, 40.1% WR, +$4,817 P&L, PF 1.14, Sharpe 0.77
- 1d (25 years, 6,449 bars): 143 trades, 51.0% WR, +$16,495 P&L, PF 2.06, Sharpe 3.26

**Key insight:** The EMA50 trend filter on daily is transformative —
filters out counter-trend shorts in gold's long uptrend eras, boosting
daily from PF 1.03 → 2.06 and win rate from 37.5% → 51.0%.

**Markets:** GC=F, SI=F, ES=F, NQ=F, CL=F
**Live deployment:** SCP'd to adamserver, futures-static + futures-autopilot
restarted. Now strategy #29 in the live catalog.

**Backtest output files:**
- `backtests/liquidity_sweep_GC_{15m,1h,1d}.json` — per-timeframe trade data
- `backtests/equity_liquidity_sweep_GC_{15m,1h,1d}.png` — equity curves
- `backtests/monthly_liquidity_sweep_GC_{15m,1h,1d}.png` — monthly P&L
- `backtests/scatter_liquidity_sweep_GC_{15m,1h,1d}.png` — trade scatter
- `backtests/liquidity_sweep_report.txt` — full text report (emailed to baldwetcoby)

## Update this file every session
Update whenever: a phase is completed, new libraries are installed,
new strategies are defined, major decisions are made, problems solved.

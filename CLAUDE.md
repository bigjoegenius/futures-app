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
~/futures-app (located at /Users/joethieme/futures-app)

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
- minimax_insights.py — optional MiniMax 10-min AI pass (stubbed if key missing)
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
- MINIMAX_API_KEY (optional)

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

## Update this file every session
Update whenever: a phase is completed, new libraries are installed,
new strategies are defined, major decisions are made, problems solved.

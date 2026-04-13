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

## Phases (planned)

### Phase 1 — Data layer
Goal: Connect to yfinance, fetch historical futures data,
store OHLCV candles in local SQLite database.

### Phase 2 — Charts and dashboard
Interactive candlestick charts, indicators, watchlist.

### Phase 3 — Strategy engine
Define trading setups, backtest against historical data.

### Phase 4 — AI prediction engine
Train models on setups, live pattern scanner with alerts.

### Phase 5 — Apex Trading integration
Connect to Apex for live trading or trade suggestions.

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

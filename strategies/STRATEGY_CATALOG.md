# Futures Strategy Catalog — 28 Strategies

Generated 2026-04-21. Source of truth for `strategy_engine.py` STRATEGIES dict and `market_analyzer.py` scoring. Every strategy below maps to a key used in code (lowercase snake_case).

## Universal conventions

- **Stop loss**: ATR-based, distance = `stop_atr_mult × ATR(14)` on the strategy's timeframe.
- **Take profit**: `target_atr_mult × ATR(14)`, default 1.8× (risk-reward 1:1.8). Overridden per strategy where noted.
- **Position sizing**: `risk_dollars = balance × risk_pct`, `contracts = risk_dollars / (stop_distance_points × point_value)`, rounded to 0.1. Capped at 10× leverage.
- **Fees**: entry = $2.50/contract taker; stop/target (bracket) = $2.50/contract maker equivalent; 1-tick slippage on both sides. Futures have NO funding rate (unlike crypto perps), but rolling to next contract costs ~1 tick each calendar month (modeled as a trade cost when a position spans a roll).
- **Entry filter**: all strategies require `ATR > 0.5 × tick` and `volume > 0` on the entry bar to avoid dead markets.
- **One trade per symbol at a time** (enforced in StrategyEngine). Autopilot can enable/disable individual strategies.

## Market-agnostic strategies (10)

### 1. `ema_cross` — EMA 9/21/50 Triple Stack
- **Rules**: `EMA9` crosses `EMA21` in direction of `EMA50`. Long if `close > EMA50 AND EMA9 crosses above EMA21`. Short if `close < EMA50 AND EMA9 crosses below EMA21`. Prefer cross within last 2 bars (fresh).
- **Timeframes**: 15m, 1h, 1d
- **Markets**: all 18
- **Stop / Target**: 1.2× ATR / 1.8× ATR
- **Why**: trend continuation after pullback; EMA50 filter keeps us on right side of dominant trend
- **Source**: classic trend-following, Steady Turtle Trading blog, TradingSim NQ guide

### 2. `macd_momentum` — MACD Histogram Flip with Trend
- **Rules**: MACD histogram flips positive while `close > EMA50` (long) or flips negative while `close < EMA50` (short). Require hist magnitude > `0.1 × ATR`.
- **Timeframes**: 1h, 1d
- **Markets**: all 18
- **Stop / Target**: 1.2× ATR / 1.8× ATR
- **Why**: momentum confirmation from MACD hist inflection often aligns with start of multi-bar trend leg
- **Source**: MetroTrade top-10 futures strategies guide, QuantVPS

### 3. `rsi_extreme` — RSI Mean Reversion in Range
- **Rules**: BB band width (`(upper-lower)/close`) < 4% (range regime). Long if RSI(14) < 30, short if RSI(14) > 70.
- **Timeframes**: 15m, 1h
- **Markets**: all 18
- **Stop / Target**: 1.2× ATR / 1.5× ATR (mean-reversion takes profit quicker)
- **Why**: sideways markets reject extremes; only works in ranges, hence BB width filter
- **Source**: NinjaTrader silver futures guide, StoneX energy trading blog

### 4. `bb_breakout` — Bollinger Band Volatility Expansion
- **Rules**: Close pokes outside BB(20, 2σ) with `volume > 1.3 × 20-bar-avg`. Direction = side of the poke.
- **Timeframes**: 5m, 15m, 1h
- **Markets**: indexes, energy, metals (volatile); skip bonds and cattle
- **Stop / Target**: 1.0× ATR / 2.0× ATR
- **Why**: volatility expansion after contraction = trend leg
- **Source**: Optimus Futures guide, BB breakout foundation research

### 5. `volume_breakout` — Range Break with Volume Surge
- **Rules**: Close breaks 20-bar `high/low` AND `volume > 2 × 20-bar-avg`.
- **Timeframes**: 15m, 1h, 1d
- **Markets**: all 18 (especially ES/NQ/CL/GC — liquid)
- **Stop / Target**: 1.2× ATR / 2.0× ATR
- **Why**: high-volume range break = institutional flow
- **Source**: CME retail-trader research, Turtle modifications

### 6. `triple_confluence` — RSI + BB + MACD Alignment
- **Rules**: 3-of-3: (a) RSI < 35 or > 65 on correct side; (b) close beyond BB; (c) MACD hist improving in trade direction.
- **Timeframes**: 1h, 1d
- **Markets**: all 18
- **Stop / Target**: 1.5× ATR / 2.5× ATR
- **Why**: combining multiple indicators reduces false positives
- **Source**: NOTE: triple-indicator had negative expectancy in crypto backtests. Keep but monitor; disable if win rate < 30% live

### 7. `donchian_20` — Turtle Short-System 20-Bar Breakout
- **Rules**: Long on close > 20-bar high, short on close < 20-bar low. ATR position sizing with 1 unit = 1% risk.
- **Timeframes**: 1d, 1h
- **Markets**: all 18 (Turtles traded commodities)
- **Stop / Target**: 2.0× ATR / trailing (exit at 10-bar opposite extreme)
- **Why**: trend-following core, 38% win rate but large winners. Well-documented profitability.
- **Source**: Richard Dennis / Altrady blog / Modern Turtle research (TOS Indicators)

### 8. `donchian_55` — Turtle Long-System 55-Bar Breakout
- **Rules**: Same as `donchian_20` but 55-bar. Slower, fewer trades, but catches the biggest trends.
- **Timeframes**: 1d
- **Markets**: all 18
- **Stop / Target**: 2.0× ATR / trailing (exit at 20-bar opposite extreme)
- **Why**: Turtle "failsafe" system for never missing a major trend
- **Source**: Turtle rules original

### 9. `keltner_squeeze` — Keltner + BB Squeeze Release
- **Rules**: "Squeeze" = BB(20,2) inside Keltner(20,1.5×ATR). When close breaks out of Keltner, enter in breakout direction.
- **Timeframes**: 1h, 1d
- **Markets**: all 18
- **Stop / Target**: 1.5× ATR / 2.5× ATR
- **Why**: volatility contraction then expansion = trend ignition
- **Source**: John Carter / SimplerTrading "TTM Squeeze" derivative

### 10. `gap_fade` — Daily Gap Back to Prior Close
- **Rules**: Today's open gaps > 0.5 × ATR away from yesterday's close. Fade: long gap-down, short gap-up. Target: prior close. Stop: 0.5 × gap beyond open.
- **Timeframes**: 1d (with 1h entry confirmation)
- **Markets**: indexes, bonds, grains (gaps commonly fade)
- **Stop / Target**: 0.5× gap / prior close
- **Why**: ~60% of gaps fill same-session for liquid futures (CME research)
- **Source**: Overnight Gap research, TastyTrade gap studies

## Index specialists — ES, NQ, YM, RTY (4)

### 11. `orb_15` — 15-Minute Opening Range Breakout
- **Rules**: At 9:30 ET, establish `hi_09_45 = max(high for 9:30-9:45)` and `lo_09_45 = min(low ...)`. After 9:45, long on close > hi_09_45, short on close < lo_09_45. Exit: VWAP cross, or EOD (16:00 ET).
- **Timeframes**: 5m (entry), 15m (structure)
- **Markets**: ES, NQ, YM, RTY
- **Stop / Target**: 1.0× ATR / 2.0× ATR OR opposite ORB edge (whichever is tighter)
- **Why**: first 15 min captures institutional positioning; break = directional bias
- **Source**: OnlyPropFirms ORB guide, Steady Turtle "6 proven strategies"

### 12. `vwap_pullback` — Trend-Day VWAP Retrace Entry
- **Rules**: Identify trend day: current bar on same side of VWAP as previous 5 bars AND VWAP slope > 0 (bull) or < 0 (bear). Enter on pullback to VWAP with rejection bar (body < 30% range).
- **Timeframes**: 1m, 5m
- **Markets**: ES, NQ, YM, RTY
- **Stop / Target**: 0.8× ATR / 1.5× ATR
- **Why**: VWAP = institutional fair value; trending markets pull back to it
- **Source**: TradingSim NQ guide, Futures Hive 2025 guide

### 13. `overnight_gap` — RTH-Open Gap Continuation
- **Rules**: If overnight session close > previous RTH close by > 0.5× ATR (bullish overnight) AND open is green 1m, go long at 9:31 ET. Mirror for short.
- **Timeframes**: 1d (with 1m entry)
- **Markets**: ES, NQ, YM
- **Stop / Target**: 1.0× ATR / 2.0× ATR, exit at noon ET if no target hit
- **Why**: overnight flow often continues into RTH for equity index futures
- **Source**: CME overnight-gap research, QuantVPS

### 14. `rth_reversal` — Late-Day Mean Reversion at Session Extreme
- **Rules**: In last 90 min (14:30–16:00 ET), if current bar high or low makes new session extreme AND RSI(14) > 70 or < 30, fade with rejection candle.
- **Timeframes**: 5m, 15m
- **Markets**: ES, NQ, YM, RTY
- **Stop / Target**: 1.0× ATR / 1.5× ATR, exit at 16:00 ET regardless
- **Why**: late-day profit taking / hedging creates reversals at extremes
- **Source**: TradingSim, day-trading community folklore (supported by data)

## Energy specialists — CL, NG (2)

### 15. `eia_fade` — EIA Post-Release Volatility Fade
- **Rules**: EIA inventory release is Wednesday 10:30 ET (crude) / Thursday 10:30 ET (natural gas). In the first 15m after release, measure the initial move. If it extends > 2× ATR and reverses within 30m, fade the move.
- **Timeframes**: 5m entry, 1h for ATR
- **Markets**: CL, NG
- **Stop / Target**: 1.5× ATR / 2.5× ATR
- **Why**: initial reaction often overshoots; fade the exhaustion
- **Source**: StoneX energy blog, StarTrader crude oil guide, EIA release volatility research

### 16. `asia_london_breakout` — CL London-Session Range Break
- **Rules**: 03:00–09:00 ET is London crude session. Establish range high/low. After 09:00, long on break above, short on break below.
- **Timeframes**: 1h
- **Markets**: CL, NG
- **Stop / Target**: 1.2× ATR / 2.0× ATR
- **Why**: London oil traders set directional bias before NY opens
- **Source**: Cannon Trading day-trading crude guide

## Metals specialists — GC, SI, HG (2)

### 17. `gold_silver_ratio` — GC/SI Pair Mean Reversion
- **Rules**: Compute `ratio = GC_close / SI_close`. Z-score over 20-day window. If z > 2: gold rich, short gold OR long silver. If z < -2: mirror.
- **Timeframes**: 1h, 1d
- **Markets**: GC (acts as ratio proxy; logic evaluates both)
- **Stop / Target**: 1.5× ATR / ratio mean reversion (ratio z back to ±1)
- **Why**: historically mean-reverting pair (80-year avg ratio)
- **Source**: SSRN paper on ML-enhanced GC-SI mean reversion, NinjaTrader silver guide

### 18. `copper_risk_on` — HG Long When ES Breaks Out
- **Rules**: Long HG when ES=F close > 20-day high AND HG RSI(14) > 50 (momentum present).
- **Timeframes**: 1d
- **Markets**: HG
- **Stop / Target**: 1.5× ATR / 2.5× ATR
- **Why**: copper is a risk-on industrial metal; equity breakouts often drag it higher
- **Source**: Robinhood metals futures guide, macro commodity research

## Bonds specialists — ZB, ZN (2)

### 19. `fomc_drift` — Post-FOMC 3-Day Drift
- **Rules**: FOMC decisions are 8 per year at 14:00 ET. The 3 days after a DOVISH surprise tend to see bonds rally. Long ZN/ZB if 14:30 ET reaction bar is green on the FOMC day AND trade is still alive 24h later. Hold max 3 days.
- **Timeframes**: 1d
- **Markets**: ZB, ZN
- **Stop / Target**: 1.5× ATR / trail on 1d close
- **Why**: post-FOMC drift documented in CME Ironbeam + Maverick Trading
- **Source**: Ironbeam Sep Fed rate cut guide, Maverick Trading ZN 2025 guide

### 20. `steepener` — 2s/10s Steepener via ZN/ZB Proxy
- **Rules**: When 10Y yield (`ZN_close → yield`) > 30Y yield (`ZB_close → yield`) — curve inverted — enter a "steepener": long ZN, short ZB. Exit when curve returns to positive.
- **Timeframes**: 1d
- **Markets**: ZB + ZN (pair)
- **Stop / Target**: 1.5× ATR / trail
- **Why**: curve reverts from inversion; 2025 consensus "crowded trade"
- **Source**: Ironbeam 2025 yield-curve guide

## Grains specialists — ZC, ZS, ZW (2)

### 21. `wasde_react` — WASDE Report 30-Min Fade
- **Rules**: WASDE released monthly (second Friday typically) at 12:00 ET. First 30 min often has extreme reaction. Fade moves > 3% in 30 min with reversal confirmation.
- **Timeframes**: 5m, 15m
- **Markets**: ZC, ZS, ZW
- **Stop / Target**: 1.5× ATR / 2.0× ATR
- **Why**: overreaction to supply/demand revisions fades as traders digest data
- **Source**: HAAWKS futures blog (112-tick average WASDE moves), MarketScreener WASDE coverage

### 22. `seasonal_harvest` — Harvest-Season Short
- **Rules**: Short ZC/ZS between September 1 and October 31 calendar (harvest flood supply pushes prices down). Enter on first 5-bar down move in September. Exit October 31 or at stop.
- **Timeframes**: 1d
- **Markets**: ZC, ZS
- **Stop / Target**: 2.0× ATR / trail until calendar exit
- **Why**: well-documented seasonal (CME education on grain seasonality)
- **Source**: CME Group Seasonality in Grains, Gov Capital grains guide

## Softs + cattle — KC, SB, CT, LE (4)

### 23. `coffee_weather_spike` — KC Post-Spike Fade
- **Rules**: Detect daily move > 3σ (based on 60-day volatility). Fade the spike on confirmation of reversal (next day opens opposite).
- **Timeframes**: 1d
- **Markets**: KC
- **Stop / Target**: 2.0× ATR / 1.5× ATR
- **Why**: coffee spikes on weather news (Brazil frost, Vietnam drought) often overshoot and revert
- **Source**: Switch Markets softs guide, RJO Futures coffee research

### 24. `sugar_carry` — SB Rollover Contango Long
- **Rules**: When front-month SB is in contango (next month > spot), long SB in last 5 days before front-month expiry. Exit at expiry.
- **Timeframes**: 1d
- **Markets**: SB
- **Stop / Target**: 1.5× ATR / positive carry target
- **Why**: rolling long gains positive carry in contango structure
- **Source**: Paradigm Futures softs analysis, contango/backwardation theory

### 25. `cotton_mean_rev` — CT RSI+BB Bounce
- **Rules**: RSI(14) < 30 AND close < BB lower band → long. Mirror short.
- **Timeframes**: 1h, 1d
- **Markets**: CT
- **Stop / Target**: 1.5× ATR / 1.8× ATR
- **Why**: cotton is cyclical and mean-reverts in absence of weather shocks
- **Source**: Switch Markets soft commodities guide

### 26. `cattle_cot_long` — LE Long on Extreme Commercial Positioning
- **Rules**: When CoT (Commitments of Traders) report shows commercials net-long at 52-week high, go long LE on next daily close > 5-day high. (Simplified: approximate via open interest + price action when CoT file not available.)
- **Timeframes**: 1d
- **Markets**: LE
- **Stop / Target**: 2.0× ATR / 3.0× ATR
- **Why**: commercial producers hedging long signals expected price strength
- **Source**: AgOptimus cattle futures 2025 guide

## Extras (2)

### 27. `range_reversal` — Tight-Range Extreme Reversal
- **Rules**: `ATR(14) / ATR(50) < 0.7` (contracted vol) AND RSI < 25 or > 75 → fade.
- **Timeframes**: 15m, 1h
- **Markets**: all 18
- **Stop / Target**: 1.0× ATR / 1.5× ATR
- **Why**: tight ranges with extremes often produce clean reversals
- **Source**: MetroTrade mean reversion guide

### 28. `breakout_retest` — Breakout, Pullback, Continuation
- **Rules**: Identify fresh 20-bar break (prior 2 bars didn't break). On retest (pullback to within 0.5× ATR of breakout level) that holds, enter continuation.
- **Timeframes**: 1h, 1d
- **Markets**: all 18 (especially ES/NQ/CL/GC)
- **Stop / Target**: 1.0× ATR (below retest low) / 2.5× ATR
- **Why**: retest confirms breakout isn't a false one; high reward-to-risk
- **Source**: HighStrike futures strategies, StrategyQuant retest logic

---

## Timeframe × strategy coverage matrix

Every timeframe has strategies assigned:

| Timeframe | Strategies |
|-----------|-----------|
| **1m**    | `vwap_pullback` (ES/NQ), `overnight_gap` (ES/NQ/YM) |
| **5m**    | `bb_breakout`, `orb_15`, `vwap_pullback`, `rth_reversal`, `wasde_react`, `eia_fade` |
| **15m**   | `ema_cross`, `rsi_extreme`, `bb_breakout`, `volume_breakout`, `orb_15`, `rth_reversal`, `wasde_react`, `keltner_squeeze`, `range_reversal` |
| **1h**    | `ema_cross`, `macd_momentum`, `rsi_extreme`, `bb_breakout`, `volume_breakout`, `triple_confluence`, `donchian_20`, `keltner_squeeze`, `asia_london_breakout`, `gold_silver_ratio`, `cotton_mean_rev`, `range_reversal`, `breakout_retest`, `eia_fade` |
| **1d**    | `ema_cross`, `macd_momentum`, `volume_breakout`, `triple_confluence`, `donchian_20`, `donchian_55`, `keltner_squeeze`, `gap_fade`, `overnight_gap`, `gold_silver_ratio`, `copper_risk_on`, `fomc_drift`, `steepener`, `seasonal_harvest`, `coffee_weather_spike`, `sugar_carry`, `cotton_mean_rev`, `cattle_cot_long`, `breakout_retest` |

## Market × strategy coverage matrix (every market has ≥ 1 specialist + shares in the 10 agnostic)

| Market | Specialist strategies | Agnostic strategies | Total |
|--------|----------------------|---------------------|-------|
| ES=F, NQ=F, YM=F, RTY=F | `orb_15`, `vwap_pullback`, `overnight_gap`, `rth_reversal` (+4) | all 10 | **14** |
| CL=F, NG=F | `eia_fade`, `asia_london_breakout` (+2) | all 10 | **12** |
| GC=F, SI=F, HG=F | `gold_silver_ratio`, `copper_risk_on` (+2) | all 10 | **12** |
| ZB=F, ZN=F | `fomc_drift`, `steepener` (+2) | 7 agnostic (skip `bb_breakout` — bonds too tight) | **9** |
| ZC=F, ZS=F, ZW=F | `wasde_react`, `seasonal_harvest` (+2) | all 10 | **12** |
| KC=F | `coffee_weather_spike` (+1) | all 10 | **11** |
| SB=F | `sugar_carry` (+1) | all 10 | **11** |
| CT=F | `cotton_mean_rev` (+1) | all 10 | **11** |
| LE=F | `cattle_cot_long` (+1) | 7 agnostic (skip `bb_breakout`/`volume_breakout`/`vwap_pullback` — thin liquidity) | **8** |

All 18 markets have at least one specialist. Every timeframe has multiple strategies.

## Implementation note

The strategies are keyed as Python dicts in `strategy_engine.py` STRATEGIES with this shape:

```python
"strategy_key": {
    "name": "Display Name",
    "direction": "LONG" | "SHORT" | "BOTH",
    "timeframes": ["1h", "1d"],
    "markets": ["all"] | ["ES=F", "NQ=F", ...],
    "stop_atr_mult": 1.2,
    "target_atr_mult": 1.8,
    "description": "plain-English summary",
    "source": "where the idea came from",
}
```

And each has a `_check_entry_<key>(df, symbol, tf, indicators)` function that returns `"LONG"`, `"SHORT"`, or `None`.

#!/usr/bin/env python3
"""
market_analyzer.py — Indicator scoring + hourly Claude AI reports for futures.

Two jobs:
  1. score_strategies(symbol, df) — compute indicators on a 1h OHLCV dataframe
     and return a 0-100 score for each generic strategy.
  2. generate_hourly_report_with_ai(market_data, trades) — ship the snapshot
     to Claude and get back a plain-English analysis.

The scoring is pure pandas/numpy. The AI report is optional and gracefully
no-ops if ANTHROPIC_API_KEY is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

from futures_config import DB_PATH, FUTURES


# ─── Strategy catalog ───────────────────────────────────────────────────
STRATEGIES = {
    "ema_cross": {
        "name": "EMA Cross",
        "direction": "BOTH",
        "description": "EMA9 vs EMA21 cross, confirmed by price vs EMA50 trend.",
    },
    "macd_momentum": {
        "name": "MACD Momentum",
        "direction": "BOTH",
        "description": "MACD histogram flip in the direction of the 50-period EMA.",
    },
    "rsi_extreme": {
        "name": "RSI Extreme",
        "direction": "BOTH",
        "description": "Mean-reversion: long RSI<30 bounces, short RSI>70 fades, in a range.",
    },
    "bb_breakout": {
        "name": "BB Breakout",
        "direction": "BOTH",
        "description": "Close pokes outside Bollinger bands with volume expansion.",
    },
    "volume_breakout": {
        "name": "Volume Breakout",
        "direction": "BOTH",
        "description": "Volume > 2x 20-bar average + breakout of 20-bar range.",
    },
    "triple_confluence": {
        "name": "Triple Confluence",
        "direction": "BOTH",
        "description": "RSI extreme + BB extreme + MACD improving, same side.",
    },
}


# ─── Indicator helpers ──────────────────────────────────────────────────
def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(span=length, adjust=False).mean()
    roll_down = down.ewm(span=length, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    line = ema_fast - ema_slow
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist


def bollinger(series: pd.Series, length: int = 20, stdev: float = 2.0):
    mid = series.rolling(length).mean()
    sd = series.rolling(length).std()
    upper = mid + stdev * sd
    lower = mid - stdev * sd
    return upper, mid, lower


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df["high"]; low = df["low"]; close = df["close"]
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean()


# ─── Data loader ────────────────────────────────────────────────────────
def load_bars(symbol: str, timeframe: str = "1h", limit: int = 300) -> pd.DataFrame | None:
    """Load the most recent `limit` bars for one symbol/timeframe."""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT datetime, open, high, low, close, volume FROM candles "
        "WHERE symbol = ? AND timeframe = ? "
        "ORDER BY datetime DESC LIMIT ?",
        conn, params=(symbol, timeframe, limit),
    )
    conn.close()
    if df.empty or len(df) < 30:
        return None
    df = df.iloc[::-1].reset_index(drop=True)  # oldest first
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df


# ─── Strategy scoring ───────────────────────────────────────────────────
def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, v)))


def score_strategies(symbol: str, df: pd.DataFrame) -> dict:
    """
    Return {strategy_id: {"score": 0-100, "direction": "LONG"/"SHORT"/"NONE",
                          "note": "...", "signals": {...}}}
    """
    if df is None or len(df) < 60:
        return {k: {"score": 0.0, "direction": "NONE", "note": "insufficient data"} for k in STRATEGIES}

    close = df["close"]
    vol = df["volume"].astype(float)
    e9 = ema(close, 9); e21 = ema(close, 21); e50 = ema(close, 50)
    r = rsi(close, 14)
    _, _, mhist = macd(close)
    bb_up, bb_mid, bb_lo = bollinger(close, 20, 2.0)
    vol_ma = vol.rolling(20).mean()
    atr_series = atr(df, 14)

    last = close.iloc[-1]
    prev = close.iloc[-2] if len(close) > 1 else last
    r_last = r.iloc[-1] if not np.isnan(r.iloc[-1]) else 50.0
    mhist_last = mhist.iloc[-1] if not np.isnan(mhist.iloc[-1]) else 0.0
    mhist_prev = mhist.iloc[-2] if len(mhist) > 1 and not np.isnan(mhist.iloc[-2]) else 0.0
    bb_up_last = bb_up.iloc[-1]; bb_lo_last = bb_lo.iloc[-1]; bb_mid_last = bb_mid.iloc[-1]
    vol_last = vol.iloc[-1]; vol_avg = vol_ma.iloc[-1] if not np.isnan(vol_ma.iloc[-1]) else vol_last
    atr_last = atr_series.iloc[-1] if not np.isnan(atr_series.iloc[-1]) else max(last * 0.01, 1e-6)

    out: dict = {}

    # 1. EMA cross
    trend_up = e50.iloc[-1] < last
    cross_up = e9.iloc[-1] > e21.iloc[-1] and e9.iloc[-2] <= e21.iloc[-2]
    cross_dn = e9.iloc[-1] < e21.iloc[-1] and e9.iloc[-2] >= e21.iloc[-2]
    separation = abs(e9.iloc[-1] - e21.iloc[-1]) / last * 100
    base = 40 + min(separation * 50, 40)
    if cross_up and trend_up:
        out["ema_cross"] = {"score": _clip(base + 20), "direction": "LONG", "note": "fresh EMA9>EMA21 cross in uptrend"}
    elif cross_dn and not trend_up:
        out["ema_cross"] = {"score": _clip(base + 20), "direction": "SHORT", "note": "fresh EMA9<EMA21 cross in downtrend"}
    elif e9.iloc[-1] > e21.iloc[-1] and trend_up:
        out["ema_cross"] = {"score": _clip(base), "direction": "LONG", "note": "aligned bull stack"}
    elif e9.iloc[-1] < e21.iloc[-1] and not trend_up:
        out["ema_cross"] = {"score": _clip(base), "direction": "SHORT", "note": "aligned bear stack"}
    else:
        out["ema_cross"] = {"score": 25.0, "direction": "NONE", "note": "mixed EMAs"}

    # 2. MACD momentum
    flipped_up = mhist_prev <= 0 < mhist_last
    flipped_dn = mhist_prev >= 0 > mhist_last
    strength = min(abs(mhist_last) / (atr_last * 0.1 + 1e-6) * 60, 60)
    if flipped_up and trend_up:
        out["macd_momentum"] = {"score": _clip(40 + strength), "direction": "LONG", "note": "histogram flipped positive with trend"}
    elif flipped_dn and not trend_up:
        out["macd_momentum"] = {"score": _clip(40 + strength), "direction": "SHORT", "note": "histogram flipped negative with trend"}
    elif mhist_last > 0 and trend_up:
        out["macd_momentum"] = {"score": _clip(35 + strength * 0.5), "direction": "LONG", "note": "bullish hist holding"}
    elif mhist_last < 0 and not trend_up:
        out["macd_momentum"] = {"score": _clip(35 + strength * 0.5), "direction": "SHORT", "note": "bearish hist holding"}
    else:
        out["macd_momentum"] = {"score": 20.0, "direction": "NONE", "note": "hist against trend"}

    # 3. RSI extreme (mean reversion in range)
    range_ratio = (bb_up_last - bb_lo_last) / last * 100 if last else 0
    is_rangy = range_ratio < 4.0  # tight BBs -> mean reversion regime
    if r_last < 30 and is_rangy:
        out["rsi_extreme"] = {"score": _clip(60 + (30 - r_last) * 1.5), "direction": "LONG", "note": f"RSI {r_last:.0f}, ranging"}
    elif r_last > 70 and is_rangy:
        out["rsi_extreme"] = {"score": _clip(60 + (r_last - 70) * 1.5), "direction": "SHORT", "note": f"RSI {r_last:.0f}, ranging"}
    elif r_last < 35:
        out["rsi_extreme"] = {"score": 45.0, "direction": "LONG", "note": f"RSI {r_last:.0f}, but trending"}
    elif r_last > 65:
        out["rsi_extreme"] = {"score": 45.0, "direction": "SHORT", "note": f"RSI {r_last:.0f}, but trending"}
    else:
        out["rsi_extreme"] = {"score": 15.0, "direction": "NONE", "note": f"RSI {r_last:.0f} neutral"}

    # 4. BB breakout
    vol_ratio = vol_last / (vol_avg + 1e-9)
    if last > bb_up_last and vol_ratio > 1.3:
        out["bb_breakout"] = {"score": _clip(55 + min(vol_ratio * 10, 35)), "direction": "LONG", "note": f"close>upper BB, vol x{vol_ratio:.1f}"}
    elif last < bb_lo_last and vol_ratio > 1.3:
        out["bb_breakout"] = {"score": _clip(55 + min(vol_ratio * 10, 35)), "direction": "SHORT", "note": f"close<lower BB, vol x{vol_ratio:.1f}"}
    elif last > bb_up_last:
        out["bb_breakout"] = {"score": 40.0, "direction": "LONG", "note": "BB upper pierce, thin volume"}
    elif last < bb_lo_last:
        out["bb_breakout"] = {"score": 40.0, "direction": "SHORT", "note": "BB lower pierce, thin volume"}
    else:
        out["bb_breakout"] = {"score": 15.0, "direction": "NONE", "note": "inside bands"}

    # 5. Volume breakout (range breakout confirmed by volume)
    hi20 = df["high"].rolling(20).max().iloc[-2]
    lo20 = df["low"].rolling(20).min().iloc[-2]
    if last > hi20 and vol_ratio > 2.0:
        out["volume_breakout"] = {"score": _clip(60 + min(vol_ratio * 5, 30)), "direction": "LONG", "note": f"range break up, vol x{vol_ratio:.1f}"}
    elif last < lo20 and vol_ratio > 2.0:
        out["volume_breakout"] = {"score": _clip(60 + min(vol_ratio * 5, 30)), "direction": "SHORT", "note": f"range break down, vol x{vol_ratio:.1f}"}
    elif vol_ratio > 2.0:
        out["volume_breakout"] = {"score": 35.0, "direction": "NONE", "note": f"volume x{vol_ratio:.1f}, no break"}
    else:
        out["volume_breakout"] = {"score": 15.0, "direction": "NONE", "note": "quiet"}

    # 6. Triple confluence
    long_votes = sum([
        r_last < 35,
        last < bb_lo_last,
        mhist_last > mhist_prev,
    ])
    short_votes = sum([
        r_last > 65,
        last > bb_up_last,
        mhist_last < mhist_prev,
    ])
    if long_votes >= 3:
        out["triple_confluence"] = {"score": 80.0, "direction": "LONG", "note": "RSI+BB+MACD all long"}
    elif short_votes >= 3:
        out["triple_confluence"] = {"score": 80.0, "direction": "SHORT", "note": "RSI+BB+MACD all short"}
    elif long_votes == 2:
        out["triple_confluence"] = {"score": 50.0, "direction": "LONG", "note": "2/3 long factors"}
    elif short_votes == 2:
        out["triple_confluence"] = {"score": 50.0, "direction": "SHORT", "note": "2/3 short factors"}
    else:
        out["triple_confluence"] = {"score": 15.0, "direction": "NONE", "note": "no confluence"}

    # Attach raw signals so autopilot / UI can show them
    common = {
        "close": float(last),
        "rsi": float(r_last),
        "macd_hist": float(mhist_last),
        "atr": float(atr_last),
        "bb_upper": float(bb_up_last),
        "bb_mid": float(bb_mid_last),
        "bb_lower": float(bb_lo_last),
        "ema9": float(e9.iloc[-1]),
        "ema21": float(e21.iloc[-1]),
        "ema50": float(e50.iloc[-1]),
        "vol_ratio": float(vol_ratio),
    }
    for k in out:
        out[k]["signals"] = common
    return out


# ─── Market snapshot ────────────────────────────────────────────────────
def get_market_snapshot(symbol: str) -> dict:
    """One consolidated object describing a symbol's current state."""
    df = load_bars(symbol, "1h", 300)
    if df is None:
        df = load_bars(symbol, "1d", 300)
    if df is None:
        return {"symbol": symbol, "error": "no data"}

    scores = score_strategies(symbol, df)
    last = float(df["close"].iloc[-1])
    prev24 = df["close"].iloc[-24] if len(df) > 24 else df["close"].iloc[0]
    chg_pct = (last - prev24) / prev24 * 100 if prev24 else 0.0

    return {
        "symbol": symbol,
        "name": FUTURES.get(symbol, symbol),
        "last": last,
        "change_24h_pct": chg_pct,
        "bars": len(df),
        "strategies": scores,
        "indicators": scores[next(iter(scores))]["signals"] if scores else {},
        "as_of": df["datetime"].iloc[-1].isoformat() if "datetime" in df else None,
    }


def get_market_overview(symbols: list[str] | None = None) -> dict:
    """Get snapshots for many symbols at once."""
    if symbols is None:
        symbols = list(FUTURES.keys())
    return {s: get_market_snapshot(s) for s in symbols}


# ─── Claude hourly report ───────────────────────────────────────────────
def generate_hourly_report_with_ai(market_data: dict, trades: list | None = None) -> str:
    """Send the market snapshot to Claude. Returns plain-text analysis.

    Fails gracefully: if there is no API key, returns a local summary.
    """
    trades = trades or []
    summary_lines = ["Futures Market Snapshot", ""]
    for sym, snap in market_data.items():
        if "error" in snap:
            continue
        best = max(snap.get("strategies", {}).items(),
                   key=lambda kv: kv[1].get("score", 0), default=(None, None))
        bname, binfo = best
        line = (f"{sym} {snap.get('name','')} @ {snap.get('last', 0):,.2f}  "
                f"({snap.get('change_24h_pct', 0):+.2f}% 24h)")
        if bname:
            line += f"  best: {bname} ({binfo['direction']}, {binfo['score']:.0f})"
        summary_lines.append(line)
    local_summary = "\n".join(summary_lines)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return local_summary + "\n\n[No ANTHROPIC_API_KEY — using local summary.]"

    try:
        import anthropic
    except ImportError:
        return local_summary + "\n\n[anthropic SDK not installed — `pip install anthropic`.]"

    client = anthropic.Anthropic(api_key=api_key)
    prompt = f"""You are analyzing the US futures market for a day trader.
Here is a JSON snapshot of current conditions across several contracts:

{json.dumps(market_data, indent=2, default=str)[:6000]}

Recent closed trades (most recent last):
{json.dumps(trades[-10:], indent=2, default=str)[:2000]}

Give a concise (under 250 words) report covering:
1. Overall risk environment (risk-on vs risk-off)
2. 2-3 contracts with the best setups right now, and which strategy to use
3. One thing the trader should watch out for
Be plain-spoken, no markdown, no fluff.
"""
    try:
        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content).strip()
        return text or local_summary
    except Exception as e:
        return local_summary + f"\n\n[Claude error: {e}]"


# ─── CLI ────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Futures market analyzer")
    ap.add_argument("--symbol", default=None, help="Score one symbol")
    ap.add_argument("--all", action="store_true", help="Score all symbols")
    ap.add_argument("--ai", action="store_true", help="Generate Claude report")
    args = ap.parse_args()

    if args.all or (not args.symbol):
        overview = get_market_overview()
        for sym, snap in overview.items():
            if "error" in snap:
                print(f"{sym:<6}  {snap['error']}")
                continue
            print(f"\n{sym}  {snap['name']}  last={snap['last']:,.2f}  24h={snap['change_24h_pct']:+.2f}%")
            for sid, s in snap["strategies"].items():
                print(f"   {sid:<20} {s['direction']:<5} {s['score']:>5.1f}   {s['note']}")
        if args.ai:
            print("\n" + "=" * 55)
            print(generate_hourly_report_with_ai(overview))
    else:
        snap = get_market_snapshot(args.symbol)
        if "error" in snap:
            print(f"{args.symbol}: {snap['error']}")
            return
        print(f"{snap['symbol']}  {snap['name']}  last={snap['last']:,.2f}")
        for sid, s in snap["strategies"].items():
            print(f"  {sid:<20} {s['direction']:<5} {s['score']:>5.1f}   {s['note']}")


if __name__ == "__main__":
    main()

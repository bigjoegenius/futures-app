#!/usr/bin/env python3
"""
market_analyzer.py — Indicator scoring + hourly Claude AI reports for futures.

Catalog of 28 strategies (10 agnostic + 18 market-specialist) as defined in
strategies/STRATEGY_CATALOG.md.

Public API (unchanged signatures):
  - score_strategies(symbol, df, *, context=None, now_dt=None) -> dict
  - load_bars(symbol, timeframe, limit) -> DataFrame | None
  - atr(df, length=14) -> Series
  - ema, rsi, macd, bollinger, keltner, donchian, vwap helpers
  - get_market_snapshot / get_market_overview / generate_hourly_report_with_ai
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta, time as dtime
from typing import Optional

import numpy as np
import pandas as pd

from futures_config import DB_PATH, FUTURES


# ─── Strategy catalog ───────────────────────────────────────────────────
STRATEGIES = {
    # ── Market-agnostic (10) ─────────────────────────────────────────
    "ema_cross": {
        "name": "EMA 9/21/50 Triple Stack",
        "direction": "BOTH",
        "timeframes": ["15m", "1h", "1d"],
        "markets": ["all"],
        "stop_atr_mult": 1.2, "target_atr_mult": 1.8,
        "description": "EMA9 crosses EMA21 in direction of EMA50 trend filter.",
    },
    "macd_momentum": {
        "name": "MACD Momentum Flip",
        "direction": "BOTH",
        "timeframes": ["1h", "1d"],
        "markets": ["all"],
        "stop_atr_mult": 1.2, "target_atr_mult": 1.8,
        "description": "MACD histogram flips with 50-EMA trend.",
    },
    "rsi_extreme": {
        "name": "RSI Mean Reversion (Range)",
        "direction": "BOTH",
        "timeframes": ["15m", "1h"],
        "markets": ["all"],
        "stop_atr_mult": 1.2, "target_atr_mult": 1.5,
        "description": "RSI extremes reverse inside tight BB ranges.",
    },
    "bb_breakout": {
        "name": "Bollinger Breakout",
        "direction": "BOTH",
        "timeframes": ["5m", "15m", "1h"],
        "markets": ["ES=F","NQ=F","YM=F","RTY=F","CL=F","NG=F","GC=F","SI=F","HG=F","ZC=F","ZS=F","ZW=F","KC=F","SB=F","CT=F"],
        "stop_atr_mult": 1.0, "target_atr_mult": 2.0,
        "description": "Close pierces BB band with volume expansion.",
    },
    "volume_breakout": {
        "name": "Volume-Confirmed Range Break",
        "direction": "BOTH",
        "timeframes": ["15m", "1h", "1d"],
        "markets": ["all"],
        "stop_atr_mult": 1.2, "target_atr_mult": 2.0,
        "description": "20-bar range break with volume > 2x avg.",
    },
    "triple_confluence": {
        "name": "RSI + BB + MACD Confluence",
        "direction": "BOTH",
        "timeframes": ["1h", "1d"],
        "markets": ["all"],
        "stop_atr_mult": 1.5, "target_atr_mult": 2.5,
        "description": "Three indicators aligned on same side.",
    },
    "donchian_20": {
        "name": "Donchian 20 (Turtle Short System)",
        "direction": "BOTH",
        "timeframes": ["1h", "1d"],
        "markets": ["all"],
        "stop_atr_mult": 2.0, "target_atr_mult": 4.0,
        "description": "Close breaks 20-bar high/low. ATR sized.",
    },
    "donchian_55": {
        "name": "Donchian 55 (Turtle Long System)",
        "direction": "BOTH",
        "timeframes": ["1d"],
        "markets": ["all"],
        "stop_atr_mult": 2.0, "target_atr_mult": 5.0,
        "description": "Close breaks 55-bar high/low. Major trend catcher.",
    },
    "keltner_squeeze": {
        "name": "Keltner/BB Squeeze Release",
        "direction": "BOTH",
        "timeframes": ["1h", "1d"],
        "markets": ["all"],
        "stop_atr_mult": 1.5, "target_atr_mult": 2.5,
        "description": "BB inside Keltner then breaks out.",
    },
    "gap_fade": {
        "name": "Daily Gap Fade",
        "direction": "BOTH",
        "timeframes": ["1d"],
        "markets": ["ES=F","NQ=F","YM=F","RTY=F","ZB=F","ZN=F","ZC=F","ZS=F","ZW=F","GC=F"],
        "stop_atr_mult": 0.8, "target_atr_mult": 1.5,
        "description": "Fade daily open gaps > 0.5 ATR back toward prior close.",
    },

    # ── Index specialists (4) ────────────────────────────────────────
    "orb_15": {
        "name": "15-min Opening Range Breakout",
        "direction": "BOTH",
        "timeframes": ["5m", "15m"],
        "markets": ["ES=F", "NQ=F", "YM=F", "RTY=F"],
        "stop_atr_mult": 1.0, "target_atr_mult": 2.0,
        "description": "Break of first 15-min (9:30-9:45 ET) range.",
    },
    "vwap_pullback": {
        "name": "VWAP Pullback on Trend Day",
        "direction": "BOTH",
        "timeframes": ["1m", "5m"],
        "markets": ["ES=F", "NQ=F", "YM=F", "RTY=F"],
        "stop_atr_mult": 0.8, "target_atr_mult": 1.5,
        "description": "Pullback to VWAP in confirmed trend day.",
    },
    "overnight_gap": {
        "name": "Overnight Gap Continuation",
        "direction": "BOTH",
        "timeframes": ["1d"],
        "markets": ["ES=F", "NQ=F", "YM=F"],
        "stop_atr_mult": 1.0, "target_atr_mult": 2.0,
        "description": "Trade in direction of overnight gap at RTH open.",
    },
    "rth_reversal": {
        "name": "Late-Day RTH Extreme Reversal",
        "direction": "BOTH",
        "timeframes": ["5m", "15m"],
        "markets": ["ES=F", "NQ=F", "YM=F", "RTY=F"],
        "stop_atr_mult": 1.0, "target_atr_mult": 1.5,
        "description": "Fade session extreme in last 90 min of cash session.",
    },

    # ── Energy specialists (2) ───────────────────────────────────────
    "eia_fade": {
        "name": "EIA Release Post-Spike Fade",
        "direction": "BOTH",
        "timeframes": ["5m", "1h"],
        "markets": ["CL=F", "NG=F"],
        "stop_atr_mult": 1.5, "target_atr_mult": 2.5,
        "description": "Fade first 30-min move after 10:30 ET EIA release.",
    },
    "asia_london_breakout": {
        "name": "London-Session Range Break",
        "direction": "BOTH",
        "timeframes": ["1h"],
        "markets": ["CL=F", "NG=F"],
        "stop_atr_mult": 1.2, "target_atr_mult": 2.0,
        "description": "Break of 03:00-09:00 ET range after 09:00.",
    },

    # ── Metals specialists (2) ───────────────────────────────────────
    "gold_silver_ratio": {
        "name": "GC/SI Ratio Mean Reversion",
        "direction": "BOTH",
        "timeframes": ["1h", "1d"],
        "markets": ["GC=F", "SI=F"],
        "stop_atr_mult": 1.5, "target_atr_mult": 2.5,
        "description": "GC/SI ratio z-score > 2 fades to mean.",
    },
    "copper_risk_on": {
        "name": "Copper Risk-On Long",
        "direction": "LONG",
        "timeframes": ["1d"],
        "markets": ["HG=F"],
        "stop_atr_mult": 1.5, "target_atr_mult": 2.5,
        "description": "Long HG when ES breaks out.",
    },

    # ── Bond specialists (2) ─────────────────────────────────────────
    "fomc_drift": {
        "name": "Post-FOMC Drift",
        "direction": "BOTH",
        "timeframes": ["1d"],
        "markets": ["ZB=F", "ZN=F"],
        "stop_atr_mult": 1.5, "target_atr_mult": 3.0,
        "description": "Trade direction of Day-of-FOMC reaction for 3 days.",
    },
    "steepener": {
        "name": "2s/10s Steepener Proxy (ZN long, ZB short)",
        "direction": "BOTH",
        "timeframes": ["1d"],
        "markets": ["ZN=F", "ZB=F"],
        "stop_atr_mult": 1.5, "target_atr_mult": 3.0,
        "description": "Long ZN + Short ZB when curve inverted.",
    },

    # ── Grain specialists (2) ────────────────────────────────────────
    "wasde_react": {
        "name": "WASDE 30-Min Post-Release Fade",
        "direction": "BOTH",
        "timeframes": ["5m", "15m"],
        "markets": ["ZC=F", "ZS=F", "ZW=F"],
        "stop_atr_mult": 1.5, "target_atr_mult": 2.0,
        "description": "Fade > 3% spike in first 30 min after WASDE.",
    },
    "seasonal_harvest": {
        "name": "Grains Harvest-Season Short",
        "direction": "SHORT",
        "timeframes": ["1d"],
        "markets": ["ZC=F", "ZS=F"],
        "stop_atr_mult": 2.0, "target_atr_mult": 3.0,
        "description": "Short Sep-Oct on first 5-bar down in Sep.",
    },

    # ── Softs + cattle (4) ───────────────────────────────────────────
    "coffee_weather_spike": {
        "name": "KC Spike Fade",
        "direction": "BOTH",
        "timeframes": ["1d"],
        "markets": ["KC=F"],
        "stop_atr_mult": 2.0, "target_atr_mult": 1.5,
        "description": "Fade > 3-sigma daily KC move after reversal confirmation.",
    },
    "sugar_carry": {
        "name": "SB Contango Rollover Long",
        "direction": "LONG",
        "timeframes": ["1d"],
        "markets": ["SB=F"],
        "stop_atr_mult": 1.5, "target_atr_mult": 2.0,
        "description": "Long SB in last 5 days before front-month expiry when in contango.",
    },
    "cotton_mean_rev": {
        "name": "CT RSI + BB Bounce",
        "direction": "BOTH",
        "timeframes": ["1h", "1d"],
        "markets": ["CT=F"],
        "stop_atr_mult": 1.5, "target_atr_mult": 1.8,
        "description": "RSI + BB extremes revert.",
    },
    "cattle_cot_long": {
        "name": "LE Commercial-Long Proxy",
        "direction": "LONG",
        "timeframes": ["1d"],
        "markets": ["LE=F"],
        "stop_atr_mult": 2.0, "target_atr_mult": 3.0,
        "description": "Long LE when price rising + OI rising (commercial proxy).",
    },

    # ── Extras (2) ───────────────────────────────────────────────────
    "range_reversal": {
        "name": "Tight-Range Extreme Reversal",
        "direction": "BOTH",
        "timeframes": ["15m", "1h"],
        "markets": ["all"],
        "stop_atr_mult": 1.0, "target_atr_mult": 1.5,
        "description": "Contracted ATR + extreme RSI reverses.",
    },
    "breakout_retest": {
        "name": "Breakout Retest Continuation",
        "direction": "BOTH",
        "timeframes": ["1h", "1d"],
        "markets": ["all"],
        "stop_atr_mult": 1.0, "target_atr_mult": 2.5,
        "description": "20-bar break, retest of level holds, then continuation.",
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


def keltner(df: pd.DataFrame, length: int = 20, atr_mult: float = 1.5):
    mid = ema(df["close"], length)
    atr_s = atr(df, length)
    return mid + atr_mult * atr_s, mid, mid - atr_mult * atr_s


def donchian(df: pd.DataFrame, length: int = 20):
    upper = df["high"].rolling(length).max()
    lower = df["low"].rolling(length).min()
    mid = (upper + lower) / 2
    return upper, mid, lower


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP based on typical price × volume. Reset each RTH day."""
    if "datetime" not in df.columns or df.empty:
        return pd.Series([np.nan] * len(df), index=df.index)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].astype(float).clip(lower=0)
    # Session key = calendar date
    key = pd.to_datetime(df["datetime"]).dt.date
    cum_tpv = (tp * vol).groupby(key).cumsum()
    cum_vol = vol.groupby(key).cumsum().replace(0, np.nan)
    return cum_tpv / cum_vol


# ─── Data loader ────────────────────────────────────────────────────────
def load_bars(symbol: str, timeframe: str = "1h", limit: int = 300) -> Optional[pd.DataFrame]:
    """Load the most recent `limit` bars for one symbol/timeframe."""
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(
            "SELECT datetime, open, high, low, close, volume FROM candles "
            "WHERE symbol = ? AND timeframe = ? "
            "ORDER BY datetime DESC LIMIT ?",
            conn, params=(symbol, timeframe, limit),
        )
        conn.close()
    except Exception:
        return None
    if df.empty or len(df) < 30:
        return None
    df = df.iloc[::-1].reset_index(drop=True)  # oldest first
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df


# ─── Event fixtures ─────────────────────────────────────────────────────
_NEWS_FIX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_fixtures.json")
_NEWS_CACHE: Optional[dict] = None


def _news() -> dict:
    global _NEWS_CACHE
    if _NEWS_CACHE is not None:
        return _NEWS_CACHE
    try:
        with open(_NEWS_FIX_PATH) as f:
            _NEWS_CACHE = json.load(f)
    except Exception:
        _NEWS_CACHE = {"fomc_dates": [], "wasde_dates": [], "major_events": []}
    return _NEWS_CACHE


def _is_fomc(date_str: str) -> bool:
    return date_str in set(_news().get("fomc_dates", []))


def _is_wasde(date_str: str) -> bool:
    return date_str in set(_news().get("wasde_dates", []))


# ─── Scoring utilities ──────────────────────────────────────────────────
def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, v)))


def _empty(note: str = "no signal") -> dict:
    return {"score": 0.0, "direction": "NONE", "note": note}


# ─── Per-strategy checkers ──────────────────────────────────────────────
def _check_ema_cross(df, ind):
    e9, e21, e50 = ind["e9"], ind["e21"], ind["e50"]
    last, prev = ind["last"], ind["prev"]
    trend_up = e50.iloc[-1] < last
    cross_up = e9.iloc[-1] > e21.iloc[-1] and e9.iloc[-2] <= e21.iloc[-2]
    cross_dn = e9.iloc[-1] < e21.iloc[-1] and e9.iloc[-2] >= e21.iloc[-2]
    sep = abs(e9.iloc[-1] - e21.iloc[-1]) / last * 100
    base = 40 + min(sep * 50, 40)
    if cross_up and trend_up:
        return {"score": _clip(base + 20), "direction": "LONG", "note": "fresh EMA9>EMA21 bull cross"}
    if cross_dn and not trend_up:
        return {"score": _clip(base + 20), "direction": "SHORT", "note": "fresh EMA9<EMA21 bear cross"}
    if e9.iloc[-1] > e21.iloc[-1] and trend_up:
        return {"score": _clip(base), "direction": "LONG", "note": "aligned bull stack"}
    if e9.iloc[-1] < e21.iloc[-1] and not trend_up:
        return {"score": _clip(base), "direction": "SHORT", "note": "aligned bear stack"}
    return _empty("mixed EMAs")


def _check_macd_momentum(df, ind):
    mhist, mhist_prev = ind["mhist_last"], ind["mhist_prev"]
    atr_last, trend_up = ind["atr_last"], ind["trend_up"]
    flipped_up = mhist_prev <= 0 < mhist
    flipped_dn = mhist_prev >= 0 > mhist
    strength = min(abs(mhist) / (atr_last * 0.1 + 1e-9) * 60, 60)
    if flipped_up and trend_up:
        return {"score": _clip(40 + strength), "direction": "LONG", "note": "MACD hist flipped +"}
    if flipped_dn and not trend_up:
        return {"score": _clip(40 + strength), "direction": "SHORT", "note": "MACD hist flipped -"}
    if mhist > 0 and trend_up:
        return {"score": _clip(35 + strength * 0.5), "direction": "LONG", "note": "bull hist holding"}
    if mhist < 0 and not trend_up:
        return {"score": _clip(35 + strength * 0.5), "direction": "SHORT", "note": "bear hist holding"}
    return _empty("hist against trend")


def _check_rsi_extreme(df, ind):
    r = ind["r_last"]; bbw = ind["bb_width_pct"]
    rangy = bbw < 4.0
    if r < 30 and rangy:
        return {"score": _clip(60 + (30 - r) * 1.5), "direction": "LONG", "note": f"RSI {r:.0f} ranging"}
    if r > 70 and rangy:
        return {"score": _clip(60 + (r - 70) * 1.5), "direction": "SHORT", "note": f"RSI {r:.0f} ranging"}
    if r < 35:
        return {"score": 45.0, "direction": "LONG", "note": f"RSI {r:.0f} trending"}
    if r > 65:
        return {"score": 45.0, "direction": "SHORT", "note": f"RSI {r:.0f} trending"}
    return _empty(f"RSI {r:.0f} neutral")


def _check_bb_breakout(df, ind):
    last = ind["last"]; bb_up = ind["bb_up_last"]; bb_lo = ind["bb_lo_last"]; vr = ind["vol_ratio"]
    if last > bb_up and vr > 1.3:
        return {"score": _clip(55 + min(vr * 10, 35)), "direction": "LONG", "note": f"BB upper break vol x{vr:.1f}"}
    if last < bb_lo and vr > 1.3:
        return {"score": _clip(55 + min(vr * 10, 35)), "direction": "SHORT", "note": f"BB lower break vol x{vr:.1f}"}
    if last > bb_up:
        return {"score": 40.0, "direction": "LONG", "note": "BB upper pierce thin vol"}
    if last < bb_lo:
        return {"score": 40.0, "direction": "SHORT", "note": "BB lower pierce thin vol"}
    return _empty("inside bands")


def _check_volume_breakout(df, ind):
    last = ind["last"]; vr = ind["vol_ratio"]
    hi20 = df["high"].rolling(20).max().iloc[-2]
    lo20 = df["low"].rolling(20).min().iloc[-2]
    if last > hi20 and vr > 2.0:
        return {"score": _clip(60 + min(vr * 5, 30)), "direction": "LONG", "note": f"range break up vol x{vr:.1f}"}
    if last < lo20 and vr > 2.0:
        return {"score": _clip(60 + min(vr * 5, 30)), "direction": "SHORT", "note": f"range break down vol x{vr:.1f}"}
    if vr > 2.0:
        return {"score": 35.0, "direction": "NONE", "note": f"vol x{vr:.1f} no break"}
    return _empty("quiet")


def _check_triple_confluence(df, ind):
    r = ind["r_last"]; last = ind["last"]; bb_up = ind["bb_up_last"]; bb_lo = ind["bb_lo_last"]
    mhist, mhist_prev = ind["mhist_last"], ind["mhist_prev"]
    long_votes = int(r < 35) + int(last < bb_lo) + int(mhist > mhist_prev)
    short_votes = int(r > 65) + int(last > bb_up) + int(mhist < mhist_prev)
    if long_votes >= 3:
        return {"score": 80.0, "direction": "LONG", "note": "RSI+BB+MACD all long"}
    if short_votes >= 3:
        return {"score": 80.0, "direction": "SHORT", "note": "RSI+BB+MACD all short"}
    if long_votes == 2:
        return {"score": 50.0, "direction": "LONG", "note": "2/3 long factors"}
    if short_votes == 2:
        return {"score": 50.0, "direction": "SHORT", "note": "2/3 short factors"}
    return _empty("no confluence")


def _check_donchian(df, ind, length=20):
    upper, _, lower = donchian(df, length)
    last = ind["last"]
    u_prev = upper.iloc[-2] if len(upper) > 1 else np.nan
    l_prev = lower.iloc[-2] if len(lower) > 1 else np.nan
    if last > u_prev:
        return {"score": 65.0, "direction": "LONG", "note": f"{length}-bar high break"}
    if last < l_prev:
        return {"score": 65.0, "direction": "SHORT", "note": f"{length}-bar low break"}
    return _empty(f"inside {length}-bar range")


def _check_keltner_squeeze(df, ind):
    # Squeeze = BB inside Keltner
    bb_u, _, bb_l = bollinger(df["close"], 20, 2.0)
    k_u, _, k_l = keltner(df, 20, 1.5)
    last = ind["last"]
    squeezed_prev = bb_u.iloc[-2] < k_u.iloc[-2] and bb_l.iloc[-2] > k_l.iloc[-2]
    if not squeezed_prev:
        return _empty("no prior squeeze")
    if last > k_u.iloc[-1]:
        return {"score": 70.0, "direction": "LONG", "note": "squeeze break up"}
    if last < k_l.iloc[-1]:
        return {"score": 70.0, "direction": "SHORT", "note": "squeeze break down"}
    return _empty("still squeezed")


def _check_gap_fade(df, ind):
    if len(df) < 3:
        return _empty("short history")
    today_open = df["open"].iloc[-1]
    prev_close = df["close"].iloc[-2]
    atr_last = ind["atr_last"]
    gap = today_open - prev_close
    if atr_last <= 0:
        return _empty("flat atr")
    if gap > 0.5 * atr_last:
        return {"score": 60.0, "direction": "SHORT", "note": f"gap up {gap:.2f} fade"}
    if gap < -0.5 * atr_last:
        return {"score": 60.0, "direction": "LONG", "note": f"gap dn {gap:.2f} fade"}
    return _empty("small gap")


def _check_orb_15(df, ind, now_dt=None):
    # Needs intraday timestamps in ET
    dt = df["datetime"]
    last = df.iloc[-1]
    t = last["datetime"]
    try:
        hour, minute = t.hour, t.minute
    except Exception:
        return _empty("no time")
    if not (9 <= hour <= 15):  # RTH only
        return _empty("outside RTH")
    # Establish ORB from today's 09:30-09:45 bars
    today = pd.Timestamp(t).normalize()
    mask = (dt >= today + pd.Timedelta(hours=9, minutes=30)) & (dt < today + pd.Timedelta(hours=9, minutes=45))
    orb = df.loc[mask]
    if len(orb) < 2:
        return _empty("ORB not formed yet")
    orb_hi = orb["high"].max(); orb_lo = orb["low"].min()
    price = last["close"]
    if price > orb_hi and hour >= 9 and (hour > 9 or minute >= 45):
        return {"score": 65.0, "direction": "LONG", "note": f"break ORB hi {orb_hi:.2f}"}
    if price < orb_lo and hour >= 9 and (hour > 9 or minute >= 45):
        return {"score": 65.0, "direction": "SHORT", "note": f"break ORB lo {orb_lo:.2f}"}
    return _empty("inside ORB")


def _check_vwap_pullback(df, ind):
    if "datetime" not in df.columns:
        return _empty("no datetime")
    v = vwap(df)
    if v.isna().all():
        return _empty("no vwap")
    last = ind["last"]
    vlast = v.iloc[-1]
    vprev5 = v.iloc[-6] if len(v) > 6 else v.iloc[0]
    slope_up = vlast > vprev5
    # price on same side of vwap as prev 5 bars
    above = (df["close"].iloc[-6:-1] > v.iloc[-6:-1]).all()
    below = (df["close"].iloc[-6:-1] < v.iloc[-6:-1]).all()
    # Pullback touched vwap on recent bar
    touched = df["low"].iloc[-2] <= vlast <= df["high"].iloc[-2]
    if above and slope_up and touched and last > vlast:
        return {"score": 65.0, "direction": "LONG", "note": "vwap bounce trend up"}
    if below and not slope_up and touched and last < vlast:
        return {"score": 65.0, "direction": "SHORT", "note": "vwap bounce trend dn"}
    return _empty("no pullback")


def _check_overnight_gap(df, ind):
    if len(df) < 3:
        return _empty("short history")
    today_open = df["open"].iloc[-1]
    prev_close = df["close"].iloc[-2]
    atr_last = ind["atr_last"]
    if atr_last <= 0:
        return _empty("flat")
    gap = today_open - prev_close
    # Continue in direction of gap if first bar of day confirmed
    today_close = df["close"].iloc[-1]
    if gap > 0.5 * atr_last and today_close > today_open:
        return {"score": 55.0, "direction": "LONG", "note": "bull gap + green bar"}
    if gap < -0.5 * atr_last and today_close < today_open:
        return {"score": 55.0, "direction": "SHORT", "note": "bear gap + red bar"}
    return _empty("gap not continuing")


def _check_rth_reversal(df, ind):
    dt = df["datetime"].iloc[-1]
    try:
        hour = dt.hour; minute = dt.minute
    except Exception:
        return _empty("no time")
    if hour < 14 or (hour == 14 and minute < 30) or hour >= 16:
        return _empty("not late-day")
    # Look at today's bars
    today = pd.Timestamp(dt).normalize()
    today_mask = df["datetime"] >= today
    today_df = df.loc[today_mask]
    if len(today_df) < 10:
        return _empty("thin day")
    sess_hi = today_df["high"].max(); sess_lo = today_df["low"].min()
    last = ind["last"]; r = ind["r_last"]
    if last >= sess_hi * 0.9995 and r > 70:
        return {"score": 60.0, "direction": "SHORT", "note": "late-day top fade"}
    if last <= sess_lo * 1.0005 and r < 30:
        return {"score": 60.0, "direction": "LONG", "note": "late-day bottom bounce"}
    return _empty("not at extreme")


def _check_eia_fade(df, ind, symbol=None):
    if symbol not in ("CL=F", "NG=F"):
        return _empty("wrong market")
    dt = df["datetime"].iloc[-1]
    try:
        dow = dt.dayofweek  # 2 = Wed (CL), 3 = Thu (NG)
        hour = dt.hour; minute = dt.minute
    except Exception:
        return _empty("no time")
    target_dow = 2 if symbol == "CL=F" else 3
    if dow != target_dow or hour < 10 or hour > 12:
        return _empty("outside window")
    # Measure move from 10:30 ET bar
    today = pd.Timestamp(dt).normalize()
    release = today + pd.Timedelta(hours=10, minutes=30)
    window = df[(df["datetime"] >= release) & (df["datetime"] <= release + pd.Timedelta(minutes=30))]
    if len(window) < 3:
        return _empty("no release bar yet")
    rel_price = window["open"].iloc[0]
    atr_last = ind["atr_last"]
    move = ind["last"] - rel_price
    if atr_last > 0 and move > 2 * atr_last and ind["last"] < ind["prev"]:
        return {"score": 65.0, "direction": "SHORT", "note": f"EIA spike up reversing {move:.2f}"}
    if atr_last > 0 and move < -2 * atr_last and ind["last"] > ind["prev"]:
        return {"score": 65.0, "direction": "LONG", "note": f"EIA spike dn reversing {move:.2f}"}
    return _empty("no spike")


def _check_asia_london_breakout(df, ind, symbol=None):
    if symbol not in ("CL=F", "NG=F"):
        return _empty("wrong market")
    dt = df["datetime"].iloc[-1]
    try:
        hour = dt.hour
    except Exception:
        return _empty("no time")
    if hour < 9 or hour > 12:
        return _empty("outside window")
    today = pd.Timestamp(dt).normalize()
    london = df[(df["datetime"] >= today + pd.Timedelta(hours=3)) &
                (df["datetime"] <= today + pd.Timedelta(hours=9))]
    if len(london) < 3:
        return _empty("no london range")
    lhi = london["high"].max(); llo = london["low"].min()
    last = ind["last"]
    if last > lhi:
        return {"score": 60.0, "direction": "LONG", "note": f"break London hi {lhi:.2f}"}
    if last < llo:
        return {"score": 60.0, "direction": "SHORT", "note": f"break London lo {llo:.2f}"}
    return _empty("inside range")


def _check_gold_silver_ratio(df, ind, symbol=None, tf="1h"):
    if symbol not in ("GC=F", "SI=F"):
        return _empty("wrong market")
    # Load the other leg
    other = "SI=F" if symbol == "GC=F" else "GC=F"
    other_df = load_bars(other, tf, 300)
    if other_df is None or len(other_df) < 60:
        return _empty("no partner data")
    # Align by date
    merged = pd.merge_asof(
        df[["datetime", "close"]].rename(columns={"close": "c_this"}).sort_values("datetime"),
        other_df[["datetime", "close"]].rename(columns={"close": "c_other"}).sort_values("datetime"),
        on="datetime", direction="nearest", tolerance=pd.Timedelta(hours=2),
    ).dropna()
    if len(merged) < 40:
        return _empty("merge thin")
    if symbol == "GC=F":
        ratio = merged["c_this"] / merged["c_other"]
    else:
        ratio = merged["c_other"] / merged["c_this"]
    zs = (ratio - ratio.rolling(20).mean()) / ratio.rolling(20).std()
    z = zs.iloc[-1]
    if pd.isna(z):
        return _empty("z nan")
    # ratio high = gold rich → short gold / long silver
    if z > 2:
        return ({"score": 70.0, "direction": "SHORT", "note": f"GC/SI z={z:.2f} gold rich"}
                if symbol == "GC=F"
                else {"score": 70.0, "direction": "LONG", "note": f"GC/SI z={z:.2f} silver cheap"})
    if z < -2:
        return ({"score": 70.0, "direction": "LONG", "note": f"GC/SI z={z:.2f} gold cheap"}
                if symbol == "GC=F"
                else {"score": 70.0, "direction": "SHORT", "note": f"GC/SI z={z:.2f} silver rich"})
    return _empty(f"z={z:.2f} ok")


def _check_copper_risk_on(df, ind, symbol=None, tf="1d"):
    if symbol != "HG=F":
        return _empty("wrong market")
    es_df = load_bars("ES=F", tf, 40)
    if es_df is None or len(es_df) < 25:
        return _empty("no ES data")
    es_20h = es_df["high"].rolling(20).max().iloc[-2]
    es_last = es_df["close"].iloc[-1]
    r = ind["r_last"]
    if es_last > es_20h and r > 50:
        return {"score": 65.0, "direction": "LONG", "note": "ES breakout + HG momentum"}
    return _empty("no risk-on")


def _check_fomc_drift(df, ind, symbol=None):
    if symbol not in ("ZB=F", "ZN=F"):
        return _empty("wrong market")
    dt = df["datetime"].iloc[-1]
    today = dt.strftime("%Y-%m-%d")
    fomc = set(_news().get("fomc_dates", []))
    # Is today within 3 days after an FOMC date?
    for i in range(0, 4):
        check = (pd.Timestamp(dt) - pd.Timedelta(days=i)).strftime("%Y-%m-%d")
        if check in fomc:
            # Direction of that day's bar (from df)
            ref = df[df["datetime"].dt.strftime("%Y-%m-%d") == check]
            if ref.empty:
                continue
            ref_row = ref.iloc[0]
            if ref_row["close"] > ref_row["open"]:
                return {"score": 55.0, "direction": "LONG", "note": f"post-FOMC drift day {i}"}
            if ref_row["close"] < ref_row["open"]:
                return {"score": 55.0, "direction": "SHORT", "note": f"post-FOMC drift day {i}"}
    return _empty("not post-FOMC")


def _check_steepener(df, ind, symbol=None, tf="1d"):
    if symbol not in ("ZN=F", "ZB=F"):
        return _empty("wrong market")
    # Proxy for curve: ZN close / ZB close ratio
    other = "ZB=F" if symbol == "ZN=F" else "ZN=F"
    other_df = load_bars(other, tf, 40)
    if other_df is None or len(other_df) < 10:
        return _empty("no partner")
    zn_close = df["close"].iloc[-1] if symbol == "ZN=F" else other_df["close"].iloc[-1]
    zb_close = other_df["close"].iloc[-1] if symbol == "ZN=F" else df["close"].iloc[-1]
    # Inversion proxy: zn < zb historically means curve normal; zn > zb means inverted.
    # Simplified: z-score of zn/zb ratio; if ratio > mean+1σ → trade steepener
    ratio = zn_close / zb_close
    ratio_hist_n = df["close"].values[-20:] / other_df["close"].values[-20:] if symbol == "ZN=F" else other_df["close"].values[-20:] / df["close"].values[-20:]
    mean = np.nanmean(ratio_hist_n); sd = np.nanstd(ratio_hist_n)
    if sd > 0 and (ratio - mean) / sd > 1.5:
        return ({"score": 60.0, "direction": "LONG", "note": "steepener long ZN"}
                if symbol == "ZN=F"
                else {"score": 60.0, "direction": "SHORT", "note": "steepener short ZB"})
    return _empty("curve normal")


def _check_wasde_react(df, ind, symbol=None):
    if symbol not in ("ZC=F", "ZS=F", "ZW=F"):
        return _empty("wrong market")
    dt = df["datetime"].iloc[-1]
    today = dt.strftime("%Y-%m-%d")
    if not _is_wasde(today):
        return _empty("not WASDE day")
    try:
        hour = dt.hour
    except Exception:
        return _empty("no time")
    if hour < 12 or hour > 13:
        return _empty("outside 12:00-13:00 ET")
    # 30-min post-release: fade > 3% moves
    release = pd.Timestamp(today) + pd.Timedelta(hours=12)
    window = df[(df["datetime"] >= release) & (df["datetime"] <= release + pd.Timedelta(minutes=30))]
    if len(window) < 3:
        return _empty("no release bars")
    rel_open = window["open"].iloc[0]
    move_pct = (ind["last"] - rel_open) / rel_open * 100
    if move_pct > 3 and ind["last"] < ind["prev"]:
        return {"score": 65.0, "direction": "SHORT", "note": f"WASDE spike up {move_pct:.1f}% fading"}
    if move_pct < -3 and ind["last"] > ind["prev"]:
        return {"score": 65.0, "direction": "LONG", "note": f"WASDE spike dn {move_pct:.1f}% fading"}
    return _empty("no extreme")


def _check_seasonal_harvest(df, ind, symbol=None):
    if symbol not in ("ZC=F", "ZS=F"):
        return _empty("wrong market")
    dt = df["datetime"].iloc[-1]
    month = dt.month
    if month not in (9, 10):
        return _empty("not Sep/Oct")
    # First 5-bar down sequence in September triggers
    if len(df) < 6:
        return _empty("short history")
    recent = df["close"].iloc[-6:].values
    down = all(recent[i] > recent[i + 1] for i in range(len(recent) - 1))
    if down:
        return {"score": 55.0, "direction": "SHORT", "note": "harvest-season short"}
    return _empty("no 5-bar down")


def _check_coffee_weather_spike(df, ind, symbol=None):
    if symbol != "KC=F":
        return _empty("wrong market")
    if len(df) < 65:
        return _empty("short history")
    rets = df["close"].pct_change().iloc[-60:]
    sd = rets.std()
    last_ret = df["close"].pct_change().iloc[-1]
    if sd == 0 or np.isnan(sd):
        return _empty("flat")
    sigma = last_ret / sd
    if sigma > 3 and ind["last"] < ind["prev"]:
        return {"score": 60.0, "direction": "SHORT", "note": f"{sigma:.1f}σ spike fading"}
    if sigma < -3 and ind["last"] > ind["prev"]:
        return {"score": 60.0, "direction": "LONG", "note": f"{sigma:.1f}σ dip fading"}
    return _empty("no weather spike")


def _check_sugar_carry(df, ind, symbol=None):
    if symbol != "SB=F":
        return _empty("wrong market")
    dt = df["datetime"].iloc[-1]
    # Contango proxy: recent 20-day uptrend (prices rising = contango in softs typically)
    if len(df) < 20:
        return _empty("short history")
    trend = ind["last"] > df["close"].rolling(20).mean().iloc[-1]
    # Last 5 trading days of month = "rollover window" proxy
    day = dt.day
    if day >= 24 and trend:
        return {"score": 55.0, "direction": "LONG", "note": "sugar rollover contango"}
    return _empty("off rollover")


def _check_cotton_mean_rev(df, ind, symbol=None):
    if symbol != "CT=F":
        return _empty("wrong market")
    r = ind["r_last"]; last = ind["last"]
    bb_up = ind["bb_up_last"]; bb_lo = ind["bb_lo_last"]
    if r < 30 and last < bb_lo:
        return {"score": 65.0, "direction": "LONG", "note": "CT RSI+BB oversold"}
    if r > 70 and last > bb_up:
        return {"score": 65.0, "direction": "SHORT", "note": "CT RSI+BB overbought"}
    return _empty("no extreme")


def _check_cattle_cot_long(df, ind, symbol=None):
    if symbol != "LE=F":
        return _empty("wrong market")
    # Proxy: price rising + volume rising over 10 bars
    if len(df) < 12:
        return _empty("short history")
    price_rising = df["close"].iloc[-1] > df["close"].iloc[-10]
    vol_rising = df["volume"].iloc[-5:].mean() > df["volume"].iloc[-15:-5].mean()
    if price_rising and vol_rising:
        return {"score": 55.0, "direction": "LONG", "note": "LE price+vol rising"}
    return _empty("no setup")


def _check_range_reversal(df, ind):
    atr_short = ind["atr_last"]
    atr_long = atr(df, 50).iloc[-1]
    if atr_long <= 0:
        return _empty("flat")
    ratio = atr_short / atr_long
    r = ind["r_last"]
    if ratio < 0.7 and r < 25:
        return {"score": 65.0, "direction": "LONG", "note": f"tight range rsi {r:.0f}"}
    if ratio < 0.7 and r > 75:
        return {"score": 65.0, "direction": "SHORT", "note": f"tight range rsi {r:.0f}"}
    return _empty("no setup")


def _check_breakout_retest(df, ind):
    if len(df) < 25:
        return _empty("short history")
    hi20 = df["high"].rolling(20).max()
    lo20 = df["low"].rolling(20).min()
    # Fresh breakout: any close in last 5 bars > hi20(-6); current bar retests
    recent = df["close"].iloc[-6:-1]
    broke_up = (recent > hi20.iloc[-12:-6].max()).any()
    broke_dn = (recent < lo20.iloc[-12:-6].min()).any()
    last = ind["last"]
    atr_last = ind["atr_last"]
    retest_long = broke_up and abs(last - hi20.iloc[-6]) < 0.5 * atr_last and last > df["low"].iloc[-1]
    retest_short = broke_dn and abs(last - lo20.iloc[-6]) < 0.5 * atr_last and last < df["high"].iloc[-1]
    if retest_long:
        return {"score": 60.0, "direction": "LONG", "note": "breakout retest hold"}
    if retest_short:
        return {"score": 60.0, "direction": "SHORT", "note": "breakdown retest hold"}
    return _empty("no retest")


# ─── Dispatcher ─────────────────────────────────────────────────────────
_CHECKER = {
    "ema_cross":             lambda df, ind, **k: _check_ema_cross(df, ind),
    "macd_momentum":         lambda df, ind, **k: _check_macd_momentum(df, ind),
    "rsi_extreme":           lambda df, ind, **k: _check_rsi_extreme(df, ind),
    "bb_breakout":           lambda df, ind, **k: _check_bb_breakout(df, ind),
    "volume_breakout":       lambda df, ind, **k: _check_volume_breakout(df, ind),
    "triple_confluence":     lambda df, ind, **k: _check_triple_confluence(df, ind),
    "donchian_20":           lambda df, ind, **k: _check_donchian(df, ind, 20),
    "donchian_55":           lambda df, ind, **k: _check_donchian(df, ind, 55),
    "keltner_squeeze":       lambda df, ind, **k: _check_keltner_squeeze(df, ind),
    "gap_fade":              lambda df, ind, **k: _check_gap_fade(df, ind),
    "orb_15":                lambda df, ind, **k: _check_orb_15(df, ind),
    "vwap_pullback":         lambda df, ind, **k: _check_vwap_pullback(df, ind),
    "overnight_gap":         lambda df, ind, **k: _check_overnight_gap(df, ind),
    "rth_reversal":          lambda df, ind, **k: _check_rth_reversal(df, ind),
    "eia_fade":              lambda df, ind, **k: _check_eia_fade(df, ind, symbol=k.get("symbol")),
    "asia_london_breakout":  lambda df, ind, **k: _check_asia_london_breakout(df, ind, symbol=k.get("symbol")),
    "gold_silver_ratio":     lambda df, ind, **k: _check_gold_silver_ratio(df, ind, symbol=k.get("symbol"), tf=k.get("tf", "1h")),
    "copper_risk_on":        lambda df, ind, **k: _check_copper_risk_on(df, ind, symbol=k.get("symbol"), tf=k.get("tf", "1d")),
    "fomc_drift":            lambda df, ind, **k: _check_fomc_drift(df, ind, symbol=k.get("symbol")),
    "steepener":             lambda df, ind, **k: _check_steepener(df, ind, symbol=k.get("symbol"), tf=k.get("tf", "1d")),
    "wasde_react":           lambda df, ind, **k: _check_wasde_react(df, ind, symbol=k.get("symbol")),
    "seasonal_harvest":      lambda df, ind, **k: _check_seasonal_harvest(df, ind, symbol=k.get("symbol")),
    "coffee_weather_spike":  lambda df, ind, **k: _check_coffee_weather_spike(df, ind, symbol=k.get("symbol")),
    "sugar_carry":            lambda df, ind, **k: _check_sugar_carry(df, ind, symbol=k.get("symbol")),
    "cotton_mean_rev":       lambda df, ind, **k: _check_cotton_mean_rev(df, ind, symbol=k.get("symbol")),
    "cattle_cot_long":       lambda df, ind, **k: _check_cattle_cot_long(df, ind, symbol=k.get("symbol")),
    "range_reversal":        lambda df, ind, **k: _check_range_reversal(df, ind),
    "breakout_retest":       lambda df, ind, **k: _check_breakout_retest(df, ind),
}


def _precompute(df: pd.DataFrame) -> dict:
    close = df["close"]
    e9 = ema(close, 9); e21 = ema(close, 21); e50 = ema(close, 50)
    r = rsi(close, 14)
    _, _, mhist = macd(close)
    bb_up, bb_mid, bb_lo = bollinger(close, 20, 2.0)
    vol = df["volume"].astype(float)
    vol_ma = vol.rolling(20).mean()
    atr_s = atr(df, 14)
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else last
    bb_up_last = float(bb_up.iloc[-1]) if not pd.isna(bb_up.iloc[-1]) else last
    bb_lo_last = float(bb_lo.iloc[-1]) if not pd.isna(bb_lo.iloc[-1]) else last
    bb_mid_last = float(bb_mid.iloc[-1]) if not pd.isna(bb_mid.iloc[-1]) else last
    bb_width_pct = float((bb_up_last - bb_lo_last) / last * 100) if last else 0.0
    atr_last = float(atr_s.iloc[-1]) if not pd.isna(atr_s.iloc[-1]) else max(last * 0.01, 1e-6)
    vol_last = float(vol.iloc[-1])
    vol_avg = float(vol_ma.iloc[-1]) if not pd.isna(vol_ma.iloc[-1]) else max(vol_last, 1)
    vol_ratio = vol_last / (vol_avg + 1e-9)
    r_last = float(r.iloc[-1]) if not pd.isna(r.iloc[-1]) else 50.0
    mhist_last = float(mhist.iloc[-1]) if not pd.isna(mhist.iloc[-1]) else 0.0
    mhist_prev = float(mhist.iloc[-2]) if len(mhist) > 1 and not pd.isna(mhist.iloc[-2]) else 0.0
    trend_up = e50.iloc[-1] < last
    return {
        "e9": e9, "e21": e21, "e50": e50,
        "r": r, "mhist": mhist, "bb_up": bb_up, "bb_mid": bb_mid, "bb_lo": bb_lo,
        "atr_s": atr_s,
        "last": last, "prev": prev,
        "bb_up_last": bb_up_last, "bb_lo_last": bb_lo_last, "bb_mid_last": bb_mid_last,
        "bb_width_pct": bb_width_pct,
        "atr_last": atr_last,
        "vol_last": vol_last, "vol_avg": vol_avg, "vol_ratio": vol_ratio,
        "r_last": r_last, "mhist_last": mhist_last, "mhist_prev": mhist_prev,
        "trend_up": bool(trend_up),
    }


def score_strategies(symbol: str, df: pd.DataFrame, *, tf: str = "1h", context: dict | None = None,
                     strategy_filter: list[str] | set[str] | None = None) -> dict:
    """Return {strategy_id: {"score":..., "direction":..., "note":..., "signals":{...}}}.

    `strategy_filter` limits which strategies to actually score (big speedup in backtests
    that only enable one strategy at a time).
    """
    if df is None or len(df) < 60:
        return {k: {"score": 0.0, "direction": "NONE", "note": "insufficient data", "signals": {}} for k in STRATEGIES}

    targets = set(strategy_filter) if strategy_filter is not None else set(STRATEGIES.keys())
    ind = _precompute(df)
    common = {
        "close": ind["last"],
        "rsi": ind["r_last"],
        "macd_hist": ind["mhist_last"],
        "atr": ind["atr_last"],
        "bb_upper": ind["bb_up_last"],
        "bb_mid": ind["bb_mid_last"],
        "bb_lower": ind["bb_lo_last"],
        "ema9": float(ind["e9"].iloc[-1]),
        "ema21": float(ind["e21"].iloc[-1]),
        "ema50": float(ind["e50"].iloc[-1]),
        "vol_ratio": ind["vol_ratio"],
    }

    out: dict = {}
    for sid, spec in STRATEGIES.items():
        if sid not in targets:
            out[sid] = {"score": 0.0, "direction": "NONE", "note": "filtered out", "signals": common}
            continue
        # Market filter
        markets = spec.get("markets", ["all"])
        if "all" not in markets and symbol not in markets:
            out[sid] = {"score": 0.0, "direction": "NONE", "note": "market not in spec", "signals": common}
            continue
        # Timeframe filter
        tfs = spec.get("timeframes", [])
        if tfs and tf not in tfs:
            out[sid] = {"score": 0.0, "direction": "NONE", "note": f"tf {tf} not in spec", "signals": common}
            continue
        try:
            fn = _CHECKER.get(sid)
            if fn is None:
                out[sid] = {"score": 0.0, "direction": "NONE", "note": "no checker", "signals": common}
                continue
            res = fn(df, ind, symbol=symbol, tf=tf)
        except Exception as e:
            res = {"score": 0.0, "direction": "NONE", "note": f"error: {e}"}
        res["signals"] = common
        out[sid] = res

    return out


# ─── Market snapshot ────────────────────────────────────────────────────
def get_market_snapshot(symbol: str, tf: str = "1h") -> dict:
    df = load_bars(symbol, tf, 300)
    if df is None:
        df = load_bars(symbol, "1d", 300)
        tf = "1d"
    if df is None:
        return {"symbol": symbol, "error": "no data"}

    scores = score_strategies(symbol, df, tf=tf)
    last = float(df["close"].iloc[-1])
    look = min(24, len(df) - 1)
    prev_ref = df["close"].iloc[-look - 1]
    chg_pct = (last - prev_ref) / prev_ref * 100 if prev_ref else 0.0

    return {
        "symbol": symbol,
        "name": FUTURES.get(symbol, symbol),
        "last": last,
        "change_24h_pct": chg_pct,
        "bars": len(df),
        "timeframe": tf,
        "strategies": scores,
        "indicators": scores[next(iter(scores))]["signals"] if scores else {},
        "as_of": df["datetime"].iloc[-1].isoformat() if "datetime" in df else None,
    }


def get_market_overview(symbols: list[str] | None = None, tf: str = "1h") -> dict:
    if symbols is None:
        symbols = list(FUTURES.keys())
    return {s: get_market_snapshot(s, tf) for s in symbols}


# ─── Claude hourly report ───────────────────────────────────────────────
def generate_hourly_report_with_ai(market_data: dict, trades: list | None = None) -> str:
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
        return local_summary + "\n\n[anthropic SDK not installed]"

    client = anthropic.Anthropic(api_key=api_key)
    prompt = f"""You are analyzing the US futures market for a day trader.

Snapshot ({len(market_data)} contracts):
{json.dumps(market_data, indent=2, default=str)[:6000]}

Recent closed trades:
{json.dumps(trades[-10:], indent=2, default=str)[:2000]}

Give a concise (<250 words) report covering:
1. Overall risk environment (risk-on vs risk-off)
2. 2-3 contracts with the best setups right now, and which strategy to use
3. One thing the trader should watch out for
Be plain-spoken, no markdown, no fluff.
"""
    try:
        msg = client.messages.create(
            model="claude-opus-4-7",
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
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ai", action="store_true")
    args = ap.parse_args()

    if args.all or (not args.symbol):
        overview = get_market_overview(tf=args.tf)
        for sym, snap in overview.items():
            if "error" in snap:
                print(f"{sym:<6}  {snap['error']}")
                continue
            print(f"\n{sym}  {snap['name']}  last={snap['last']:,.2f}  24h={snap['change_24h_pct']:+.2f}%  tf={snap['timeframe']}")
            for sid, s in snap["strategies"].items():
                if s["score"] > 0:
                    print(f"   {sid:<22} {s['direction']:<5} {s['score']:>5.1f}   {s['note']}")
        if args.ai:
            print("\n" + "=" * 55)
            print(generate_hourly_report_with_ai(overview))
    else:
        snap = get_market_snapshot(args.symbol, args.tf)
        if "error" in snap:
            print(f"{args.symbol}: {snap['error']}")
            return
        print(f"{snap['symbol']}  {snap['name']}  last={snap['last']:,.2f}  tf={snap['timeframe']}")
        for sid, s in snap["strategies"].items():
            if s["score"] > 0:
                print(f"  {sid:<22} {s['direction']:<5} {s['score']:>5.1f}   {s['note']}")


if __name__ == "__main__":
    main()

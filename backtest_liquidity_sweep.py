#!/usr/bin/env python3
"""
backtest_liquidity_sweep.py — Comprehensive backtest of the Liquidity Sweep
& Reclaim strategy on Gold (GC=F) across all available timeframes.

Strategy from @_market_decoded_ Instagram reel:
  1. IDENTIFY LIQUIDITY — find equal highs (or lows) forming a liquidity pool
  2. WAIT FOR SWEEP    — price breaks above (or below), trapping breakout traders
  3. CONFIRM RECLAIM   — price closes back inside → enter reversal

Usage:
  python backtest_liquidity_sweep.py                # full run, all timeframes
  python backtest_liquidity_sweep.py --email        # run + email report to baldwetcoby
  python backtest_liquidity_sweep.py --tf 1d        # single timeframe
"""

from __future__ import annotations

import argparse
import json
import math
import os
import smtplib
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import numpy as np
import pandas as pd

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    MPL_OK = True
except Exception:
    MPL_OK = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from futures_config import DB_PATH
from market_analyzer import atr, ema, rsi, load_bars

SYMBOL = "GC=F"
POINT_VALUE = 100.0    # gold futures: $100 per point
TICK_SIZE = 0.10
FEE_PER_SIDE = 2.50
SLIPPAGE_TICKS = 1
STARTING_BALANCE = 10_000.0

# Optimized parameters per timeframe (from parameter sweep)
OPTIMIZED_PARAMS = {
    "15m": {"lookback": 10, "tol_mult": 0.5, "min_touches": 2, "target_mult": 3.0, "trend_filter": False},
    "1h":  {"lookback": 30, "tol_mult": 0.3, "min_touches": 3, "target_mult": 1.8, "trend_filter": False},
    "1d":  {"lookback": 10, "tol_mult": 0.5, "min_touches": 2, "target_mult": 1.8, "trend_filter": True},
}

OUT_DIR = os.path.join(BASE_DIR, "backtests")
os.makedirs(OUT_DIR, exist_ok=True)


# ─── Load ALL gold data (not the limited bar counts) ─────────────────────
def load_all_gold(timeframe: str) -> pd.DataFrame | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(
            "SELECT datetime, open, high, low, close, volume FROM candles "
            "WHERE symbol = ? AND timeframe = ? ORDER BY datetime ASC",
            conn, params=(SYMBOL, timeframe),
        )
        conn.close()
    except Exception as e:
        print(f"DB error: {e}")
        return None
    if df.empty or len(df) < 60:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df


# ─── Indicator helpers ───────────────────────────────────────────────────
def compute_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df["high"]; low = df["low"]; close = df["close"]
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean()


# ─── Core strategy detection ─────────────────────────────────────────────
def detect_sweep_reclaim(df: pd.DataFrame, i: int, atr_val: float,
                         lookback: int = 20, tol_mult: float = 0.3,
                         min_touches: int = 2, target_mult: float = 2.5,
                         trend_filter: bool = False,
                         ema50_series: pd.Series | None = None) -> dict | None:
    """At bar index i, check if bars [i-lookback-1 .. i] form a sweep-reclaim.

    Returns a trade dict or None.
    """
    if i < lookback + 2 or i >= len(df):
        return None
    if atr_val <= 0:
        return None

    tol = atr_val * tol_mult

    # Window for finding equal highs/lows (excludes the last 2 bars: sweep + reclaim)
    window_start = max(0, i - lookback - 1)
    window_end = i - 1  # exclude sweep_bar and current_bar
    window = df.iloc[window_start:window_end]

    if len(window) < 5:
        return None

    sweep_bar = df.iloc[i - 1]
    current_bar = df.iloc[i]

    # EMA50 for trend filter
    ema50_val = None
    if trend_filter and ema50_series is not None and i < len(ema50_series):
        v = ema50_series.iloc[i]
        if not pd.isna(v):
            ema50_val = float(v)

    # ── BEARISH SWEEP (above equal highs → SHORT) ──
    highs = window["high"].values
    resistance = np.max(highs)
    touches = int(np.sum(np.abs(highs - resistance) < tol))

    if touches >= min_touches:
        sweep_high = float(sweep_bar["high"])
        if sweep_high > resistance:
            sweep_pen = (sweep_high - resistance) / atr_val
            if sweep_pen >= 0.05:
                current_close = float(current_bar["close"])
                if current_close < resistance:
                    # Trend filter: skip shorts when price is above EMA50
                    if trend_filter and ema50_val is not None and current_close > ema50_val:
                        pass  # filtered out
                    else:
                        reclaim_depth = (resistance - current_close) / atr_val
                        wick = (sweep_high - float(sweep_bar["close"])) / atr_val

                        score = 55 + min((touches - 2) * 4, 12) + min(sweep_pen * 12, 15) \
                                + min(reclaim_depth * 10, 10) + min(wick * 5, 8)

                        stop = sweep_high + atr_val * 0.2
                        target = current_close - atr_val * target_mult

                        return {
                            "direction": "short",
                            "entry_price": current_close,
                            "stop_price": stop,
                            "target_price": target,
                            "entry_time": str(current_bar["datetime"]),
                            "score": min(score, 100),
                            "touches": touches,
                            "resistance": resistance,
                            "sweep_high": sweep_high,
                            "sweep_penetration_atr": round(sweep_pen, 3),
                            "reclaim_depth_atr": round(reclaim_depth, 3),
                        }

    # ── BULLISH SWEEP (below equal lows → LONG) ──
    lows = window["low"].values
    support = np.min(lows)
    touches_lo = int(np.sum(np.abs(lows - support) < tol))

    if touches_lo >= min_touches:
        sweep_low = float(sweep_bar["low"])
        if sweep_low < support:
            sweep_pen = (support - sweep_low) / atr_val
            if sweep_pen >= 0.05:
                current_close = float(current_bar["close"])
                if current_close > support:
                    # Trend filter: skip longs when price is below EMA50
                    if trend_filter and ema50_val is not None and current_close < ema50_val:
                        pass  # filtered out
                    else:
                        reclaim_depth = (current_close - support) / atr_val
                        wick = (float(sweep_bar["close"]) - sweep_low) / atr_val

                        score = 55 + min((touches_lo - 2) * 4, 12) + min(sweep_pen * 12, 15) \
                                + min(reclaim_depth * 10, 10) + min(wick * 5, 8)

                        stop = sweep_low - atr_val * 0.2
                        target = current_close + atr_val * target_mult

                        return {
                            "direction": "long",
                            "entry_price": current_close,
                            "stop_price": stop,
                            "target_price": target,
                            "entry_time": str(current_bar["datetime"]),
                            "score": min(score, 100),
                            "touches": touches_lo,
                            "support": support,
                            "sweep_low": sweep_low,
                            "sweep_penetration_atr": round(sweep_pen, 3),
                            "reclaim_depth_atr": round(reclaim_depth, 3),
                        }

    return None


# ─── Walk-forward backtester ─────────────────────────────────────────────
def run_backtest(df: pd.DataFrame, timeframe: str,
                 risk_pct: float = 0.01,
                 lookback: int = 20,
                 tol_mult: float = 0.3,
                 min_touches: int = 2,
                 target_mult: float = 2.5,
                 trend_filter: bool = False) -> dict:
    """Walk bar-by-bar through the dataframe, detect signals, manage trades."""

    atr_series = compute_atr(df, 14)
    ema50_series = ema(df["close"], 50) if trend_filter else None
    balance = STARTING_BALANCE
    trades = []
    position = None  # dict with open trade info
    equity_curve = [STARTING_BALANCE]
    equity_dates = [df["datetime"].iloc[0]]

    min_start = max(60, lookback + 5)

    for i in range(min_start, len(df)):
        bar = df.iloc[i]
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])
        bar_time = str(bar["datetime"])
        atr_val = float(atr_series.iloc[i]) if not pd.isna(atr_series.iloc[i]) else 0

        # ── Manage open position ──
        if position is not None:
            hit_stop = False
            hit_target = False

            if position["direction"] == "short":
                hit_stop = bar_high >= position["stop_price"]
                hit_target = bar_low <= position["target_price"]
            else:
                hit_stop = bar_low <= position["stop_price"]
                hit_target = bar_high >= position["target_price"]

            if hit_stop and hit_target:
                # Conservative: assume stop fills first
                exit_price = position["stop_price"]
                reason = "stop"
            elif hit_stop:
                exit_price = position["stop_price"]
                reason = "stop"
            elif hit_target:
                exit_price = position["target_price"]
                reason = "target"
            else:
                exit_price = None
                reason = None

            if exit_price is not None:
                slip = SLIPPAGE_TICKS * TICK_SIZE
                if position["direction"] == "short":
                    fill = exit_price + slip
                    gross = (position["entry_fill"] - fill) * POINT_VALUE * position["contracts"]
                else:
                    fill = exit_price - slip
                    gross = (fill - position["entry_fill"]) * POINT_VALUE * position["contracts"]

                fees = FEE_PER_SIDE * 2 * position["contracts"]
                pnl = gross - fees
                pnl_pct = (pnl / balance) * 100 if balance > 0 else 0
                balance += pnl

                trade_record = {
                    **position,
                    "exit_time": bar_time,
                    "exit_price": fill,
                    "pnl_dollars": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                    "fees": round(fees, 2),
                    "exit_reason": reason,
                    "bars_held": i - position["entry_bar_idx"],
                }
                trades.append(trade_record)
                position = None

        # ── Check for new signal (only if flat) ──
        if position is None and atr_val > 0:
            signal = detect_sweep_reclaim(df, i, atr_val, lookback, tol_mult, min_touches,
                                         target_mult, trend_filter, ema50_series)

            if signal is not None:
                slip = SLIPPAGE_TICKS * TICK_SIZE
                entry_price = signal["entry_price"]
                fill = entry_price + slip if signal["direction"] == "long" else entry_price - slip

                stop_dist = abs(fill - signal["stop_price"])
                loss_per_contract = stop_dist * POINT_VALUE
                if loss_per_contract > 0:
                    risk_dollars = balance * risk_pct
                    contracts = max(round(risk_dollars / loss_per_contract, 1), 0.1)

                    position = {
                        "direction": signal["direction"],
                        "entry_price": entry_price,
                        "entry_fill": fill,
                        "stop_price": signal["stop_price"],
                        "target_price": signal["target_price"],
                        "contracts": contracts,
                        "entry_time": signal["entry_time"],
                        "entry_bar_idx": i,
                        "score": signal["score"],
                        "touches": signal["touches"],
                        "sweep_penetration_atr": signal["sweep_penetration_atr"],
                    }

        equity_curve.append(balance)
        equity_dates.append(bar["datetime"])

    # Close any open position at end
    if position is not None:
        bar = df.iloc[-1]
        bar_close = float(bar["close"])
        slip = SLIPPAGE_TICKS * TICK_SIZE
        if position["direction"] == "short":
            fill = bar_close + slip
            gross = (position["entry_fill"] - fill) * POINT_VALUE * position["contracts"]
        else:
            fill = bar_close - slip
            gross = (fill - position["entry_fill"]) * POINT_VALUE * position["contracts"]
        fees = FEE_PER_SIDE * 2 * position["contracts"]
        pnl = gross - fees
        balance += pnl
        trade_record = {
            **position,
            "exit_time": str(bar["datetime"]),
            "exit_price": fill,
            "pnl_dollars": round(pnl, 2),
            "pnl_pct": round((pnl / (balance - pnl)) * 100, 4) if (balance - pnl) > 0 else 0,
            "fees": round(fees, 2),
            "exit_reason": "end_of_test",
            "bars_held": len(df) - 1 - position["entry_bar_idx"],
        }
        trades.append(trade_record)
        equity_curve.append(balance)
        equity_dates.append(bar["datetime"])

    return {
        "timeframe": timeframe,
        "symbol": SYMBOL,
        "bars_analyzed": len(df),
        "date_range": f"{df['datetime'].iloc[0]} → {df['datetime'].iloc[-1]}",
        "trades": trades,
        "equity_curve": equity_curve,
        "equity_dates": equity_dates,
        "final_balance": balance,
    }


# ─── Metrics computation ─────────────────────────────────────────────────
def compute_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "final_balance": STARTING_BALANCE,
            "profit_factor": 0.0, "max_drawdown_pct": 0.0, "sharpe": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
            "avg_bars_held": 0, "avg_rr_actual": 0.0,
            "long_trades": 0, "short_trades": 0,
            "long_win_rate": 0.0, "short_win_rate": 0.0,
            "long_pnl": 0.0, "short_pnl": 0.0,
            "max_consecutive_wins": 0, "max_consecutive_losses": 0,
            "target_exits": 0, "stop_exits": 0, "target_pct": 0.0,
        }

    pnls = np.array([t["pnl_dollars"] for t in trades], dtype=float)
    wins_mask = pnls > 0
    total_pnl = float(pnls.sum())
    wins = int(wins_mask.sum())
    losses = int((~wins_mask).sum())
    win_rate = (wins / len(pnls) * 100) if len(pnls) else 0

    gross_win = float(pnls[wins_mask].sum()) if wins else 0
    gross_loss = float(-pnls[~wins_mask].sum()) if losses else 0
    pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0)

    # Equity curve and drawdown
    equity = STARTING_BALANCE + np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.where(peak > 0, peak, 1)
    max_dd = float(dd.max() * 100) if len(dd) else 0

    # Sharpe
    if pnls.std() > 0:
        per_trade = pnls / STARTING_BALANCE
        sharpe = float((per_trade.mean() / per_trade.std()) * math.sqrt(252))
    else:
        sharpe = 0.0

    avg_win = float(pnls[wins_mask].mean()) if wins else 0
    avg_loss = float(pnls[~wins_mask].mean()) if losses else 0

    # Direction breakdown
    long_trades = [t for t in trades if t["direction"] == "long"]
    short_trades = [t for t in trades if t["direction"] == "short"]
    long_wins = sum(1 for t in long_trades if t["pnl_dollars"] > 0)
    short_wins = sum(1 for t in short_trades if t["pnl_dollars"] > 0)

    # Consecutive wins/losses
    max_con_w = max_con_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1; cur_l = 0
            max_con_w = max(max_con_w, cur_w)
        else:
            cur_l += 1; cur_w = 0
            max_con_l = max(max_con_l, cur_l)

    # Exit reasons
    target_exits = sum(1 for t in trades if t.get("exit_reason") == "target")
    stop_exits = sum(1 for t in trades if t.get("exit_reason") == "stop")

    # Average R:R realized
    avg_rr = (avg_win / abs(avg_loss)) if avg_loss != 0 else 0

    return {
        "trades": len(pnls),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 2),
        "final_balance": round(STARTING_BALANCE + total_pnl, 2),
        "profit_factor": round(pf, 3) if math.isfinite(pf) else 0.0,
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 3),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "best_trade": round(float(pnls.max()), 2),
        "worst_trade": round(float(pnls.min()), 2),
        "avg_bars_held": round(np.mean([t.get("bars_held", 0) for t in trades]), 1),
        "avg_rr_actual": round(avg_rr, 2),
        "long_trades": len(long_trades),
        "short_trades": len(short_trades),
        "long_win_rate": round(long_wins / len(long_trades) * 100, 1) if long_trades else 0,
        "short_win_rate": round(short_wins / len(short_trades) * 100, 1) if short_trades else 0,
        "long_pnl": round(sum(t["pnl_dollars"] for t in long_trades), 2),
        "short_pnl": round(sum(t["pnl_dollars"] for t in short_trades), 2),
        "max_consecutive_wins": max_con_w,
        "max_consecutive_losses": max_con_l,
        "target_exits": target_exits,
        "stop_exits": stop_exits,
        "target_pct": round(target_exits / len(pnls) * 100, 1) if len(pnls) else 0,
    }


# ─── Yearly breakdown ────────────────────────────────────────────────────
def yearly_breakdown(trades: list[dict]) -> dict:
    by_year = {}
    for t in trades:
        try:
            yr = pd.to_datetime(t["entry_time"]).year
        except Exception:
            continue
        if yr not in by_year:
            by_year[yr] = []
        by_year[yr].append(t)

    result = {}
    for yr in sorted(by_year.keys()):
        m = compute_metrics(by_year[yr])
        result[yr] = {
            "trades": m["trades"],
            "win_rate": m["win_rate"],
            "total_pnl": m["total_pnl"],
            "profit_factor": m["profit_factor"],
            "sharpe": m["sharpe"],
            "max_drawdown_pct": m["max_drawdown_pct"],
        }
    return result


# ─── Equity curve chart ──────────────────────────────────────────────────
def plot_equity(equity: list, dates: list, title: str, out_path: str) -> str | None:
    if not MPL_OK or len(equity) < 2:
        return None
    fig, ax = plt.subplots(figsize=(12, 5))
    x = range(len(equity))
    ax.plot(x, equity, "-", color="#3fb950", linewidth=1.2)
    ax.fill_between(x, equity, STARTING_BALANCE,
                    where=[e >= STARTING_BALANCE for e in equity],
                    color="#3fb950", alpha=0.15)
    ax.fill_between(x, equity, STARTING_BALANCE,
                    where=[e < STARTING_BALANCE for e in equity],
                    color="#f85149", alpha=0.15)
    ax.axhline(STARTING_BALANCE, color="#8b949e", linestyle="--", linewidth=0.8)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel("Bar #")
    ax.set_ylabel("Equity ($)")
    ax.grid(True, alpha=0.15)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_monthly_returns(trades: list[dict], title: str, out_path: str) -> str | None:
    if not MPL_OK or not trades:
        return None
    by_month = {}
    for t in trades:
        try:
            dt = pd.to_datetime(t["entry_time"])
            key = dt.strftime("%Y-%m")
        except Exception:
            continue
        by_month.setdefault(key, 0)
        by_month[key] += t["pnl_dollars"]

    months = sorted(by_month.keys())
    pnls = [by_month[m] for m in months]

    fig, ax = plt.subplots(figsize=(14, 4))
    colors = ["#3fb950" if p >= 0 else "#f85149" for p in pnls]
    ax.bar(range(len(months)), pnls, color=colors, alpha=0.8)
    ax.set_xticks(range(len(months)))
    ax.set_xticklabels(months, rotation=45, ha="right", fontsize=6)
    ax.axhline(0, color="#8b949e", linewidth=0.8)
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_ylabel("P&L ($)")
    ax.grid(True, alpha=0.15, axis="y")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def plot_trade_scatter(trades: list[dict], title: str, out_path: str) -> str | None:
    if not MPL_OK or not trades:
        return None
    pnls = [t["pnl_dollars"] for t in trades]
    colors = ["#3fb950" if p >= 0 else "#f85149" for p in pnls]
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.scatter(range(len(pnls)), pnls, c=colors, s=15, alpha=0.7)
    ax.axhline(0, color="#8b949e", linewidth=0.8)
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("P&L ($)")
    ax.grid(True, alpha=0.15)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ─── Text report builder ─────────────────────────────────────────────────
def build_text_report(all_results: list[dict]) -> str:
    lines = []
    lines.append("=" * 78)
    lines.append("  GOLD LIQUIDITY SWEEP & RECLAIM STRATEGY — BACKTEST REPORT")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    lines.append("=" * 78)
    lines.append("")
    lines.append("STRATEGY OVERVIEW")
    lines.append("-" * 40)
    lines.append("Source: @_market_decoded_ Instagram")
    lines.append("Concept: Institutional liquidity sweep reversal")
    lines.append("  1. Identify equal highs/lows (liquidity pool)")
    lines.append("  2. Wait for price to sweep through (trap breakout traders)")
    lines.append("  3. Enter reversal when price reclaims back inside")
    lines.append("  Stop: Above/below the sweep extreme + 0.2x ATR buffer")
    lines.append("  Target: ATR-based (optimized per timeframe)")
    lines.append("  Risk: 1% of equity per trade")
    lines.append(f"  Starting balance: ${STARTING_BALANCE:,.0f}")
    lines.append("")
    lines.append("OPTIMIZED PARAMETERS (from 192-combination sweep)")
    lines.append("-" * 55)
    for tf, p in OPTIMIZED_PARAMS.items():
        lines.append(f"  {tf.upper()}: lookback={p['lookback']}, tol={p['tol_mult']}x ATR, "
                    f"min_touches={p['min_touches']}, target={p['target_mult']}x ATR, "
                    f"trend_filter={'EMA50' if p['trend_filter'] else 'none'}")
    lines.append("")

    for result in all_results:
        tf = result["timeframe"]
        metrics = result["metrics"]
        trades = result["trades"]
        yearly = result.get("yearly", {})

        lines.append("=" * 78)
        lines.append(f"  TIMEFRAME: {tf.upper()}  |  {result['symbol']}  |  {result['bars_analyzed']:,} bars")
        lines.append(f"  Date range: {result['date_range']}")
        lines.append("=" * 78)
        lines.append("")

        # Core metrics
        lines.append(f"  Total Trades:          {metrics['trades']}")
        lines.append(f"  Wins / Losses:         {metrics['wins']} / {metrics['losses']}")
        lines.append(f"  Win Rate:              {metrics['win_rate']:.1f}%")
        lines.append(f"  Total P&L:             ${metrics['total_pnl']:>+,.2f}")
        lines.append(f"  Final Balance:         ${metrics['final_balance']:>,.2f}")
        lines.append(f"  Profit Factor:         {metrics['profit_factor']:.3f}")
        lines.append(f"  Sharpe Ratio:          {metrics['sharpe']:.3f}")
        lines.append(f"  Max Drawdown:          {metrics['max_drawdown_pct']:.2f}%")
        lines.append("")

        # Trade details
        lines.append(f"  Avg Win:               ${metrics['avg_win']:>+,.2f}")
        lines.append(f"  Avg Loss:              ${metrics['avg_loss']:>+,.2f}")
        lines.append(f"  Avg R:R Realized:      {metrics['avg_rr_actual']:.2f}")
        lines.append(f"  Best Trade:            ${metrics['best_trade']:>+,.2f}")
        lines.append(f"  Worst Trade:           ${metrics['worst_trade']:>+,.2f}")
        lines.append(f"  Avg Bars Held:         {metrics['avg_bars_held']:.1f}")
        lines.append("")

        # Direction breakdown
        lines.append(f"  Long Trades:           {metrics['long_trades']}  "
                     f"(WR {metrics['long_win_rate']:.1f}%, P&L ${metrics['long_pnl']:>+,.2f})")
        lines.append(f"  Short Trades:          {metrics['short_trades']}  "
                     f"(WR {metrics['short_win_rate']:.1f}%, P&L ${metrics['short_pnl']:>+,.2f})")
        lines.append("")

        # Exit analysis
        lines.append(f"  Target Exits:          {metrics['target_exits']} ({metrics['target_pct']:.1f}%)")
        lines.append(f"  Stop Exits:            {metrics['stop_exits']}")
        lines.append(f"  Max Consec Wins:       {metrics['max_consecutive_wins']}")
        lines.append(f"  Max Consec Losses:     {metrics['max_consecutive_losses']}")
        lines.append("")

        # Yearly breakdown
        if yearly:
            lines.append("  YEARLY BREAKDOWN")
            lines.append(f"  {'Year':<6} {'Trades':>7} {'WR':>7} {'P&L':>12} {'PF':>7} {'Sharpe':>7} {'Max DD':>8}")
            lines.append("  " + "-" * 56)
            for yr, ym in yearly.items():
                lines.append(f"  {yr:<6} {ym['trades']:>7} {ym['win_rate']:>6.1f}% "
                            f"${ym['total_pnl']:>+10,.2f} {ym['profit_factor']:>6.2f} "
                            f"{ym['sharpe']:>6.2f} {ym['max_drawdown_pct']:>7.1f}%")
            lines.append("")

        # Top 10 trades
        if trades:
            sorted_trades = sorted(trades, key=lambda t: t["pnl_dollars"], reverse=True)
            lines.append("  TOP 5 WINNING TRADES")
            lines.append(f"  {'Entry Time':<22} {'Dir':<6} {'Entry':>10} {'Exit':>10} {'P&L':>12} {'Reason':<8}")
            lines.append("  " + "-" * 70)
            for t in sorted_trades[:5]:
                lines.append(f"  {t['entry_time']:<22} {t['direction']:<6} "
                            f"{t['entry_fill']:>10,.2f} {t['exit_price']:>10,.2f} "
                            f"${t['pnl_dollars']:>+10,.2f} {t.get('exit_reason',''):8}")
            lines.append("")

            lines.append("  WORST 5 LOSING TRADES")
            lines.append(f"  {'Entry Time':<22} {'Dir':<6} {'Entry':>10} {'Exit':>10} {'P&L':>12} {'Reason':<8}")
            lines.append("  " + "-" * 70)
            for t in sorted_trades[-5:]:
                lines.append(f"  {t['entry_time']:<22} {t['direction']:<6} "
                            f"{t['entry_fill']:>10,.2f} {t['exit_price']:>10,.2f} "
                            f"${t['pnl_dollars']:>+10,.2f} {t.get('exit_reason',''):8}")
            lines.append("")

    # Summary comparison
    lines.append("=" * 78)
    lines.append("  CROSS-TIMEFRAME COMPARISON")
    lines.append("=" * 78)
    lines.append(f"  {'TF':<5} {'Trades':>7} {'WR':>7} {'P&L':>14} {'PF':>7} {'Sharpe':>7} {'Max DD':>8} {'Avg RR':>7}")
    lines.append("  " + "-" * 64)
    for r in all_results:
        m = r["metrics"]
        lines.append(f"  {r['timeframe']:<5} {m['trades']:>7} {m['win_rate']:>6.1f}% "
                    f"${m['total_pnl']:>+12,.2f} {m['profit_factor']:>6.2f} "
                    f"{m['sharpe']:>6.2f} {m['max_drawdown_pct']:>7.1f}% {m['avg_rr_actual']:>6.2f}")
    lines.append("")

    # Verdict
    best = max(all_results, key=lambda r: r["metrics"]["total_pnl"]) if all_results else None
    if best:
        bm = best["metrics"]
        lines.append("  VERDICT")
        lines.append("  " + "-" * 40)
        if bm["total_pnl"] > 0 and bm["profit_factor"] > 1.0:
            lines.append(f"  Best timeframe: {best['timeframe'].upper()} — profitable with "
                        f"PF {bm['profit_factor']:.2f} and Sharpe {bm['sharpe']:.2f}")
            if bm["win_rate"] < 50:
                lines.append("  Note: Win rate is below 50% but the strategy compensates with")
                lines.append(f"  a favorable avg R:R of {bm['avg_rr_actual']:.2f}")
        elif bm["total_pnl"] > 0:
            lines.append(f"  Marginally profitable on {best['timeframe'].upper()} but profit factor")
            lines.append(f"  of {bm['profit_factor']:.2f} suggests inconsistency. Use with caution.")
        else:
            lines.append("  Strategy was not profitable on any timeframe in this backtest.")
            lines.append("  Consider parameter tuning or combining with additional filters.")
    lines.append("")
    lines.append("=" * 78)

    return "\n".join(lines)


# ─── Email sender ─────────────────────────────────────────────────────────
def send_email_report(text_report: str, chart_paths: list[str]):
    env_path = os.path.join(BASE_DIR, ".env")
    env = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")

    gmail_user = env.get("GMAIL_USER", os.environ.get("GMAIL_USER", ""))
    gmail_pass = env.get("GMAIL_APP_PASSWORD", os.environ.get("GMAIL_APP_PASSWORD", ""))
    recipient = env.get("BALDWETCOBY_EMAIL_TO",
                       os.environ.get("BALDWETCOBY_EMAIL_TO", "baldwetcoby@gmail.com"))

    if not gmail_user or not gmail_pass:
        print("Missing GMAIL_USER or GMAIL_APP_PASSWORD — skipping email.")
        return False

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = recipient
    msg["Subject"] = "[FUTURES] Gold Liquidity Sweep & Reclaim — Backtest Report"

    msg.attach(MIMEText(text_report, "plain"))

    for path in chart_paths:
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                img = MIMEImage(f.read())
                img.add_header("Content-Disposition", "attachment",
                             filename=os.path.basename(path))
                msg.attach(img)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, recipient, msg.as_string())
        print(f"Report emailed to {recipient}")
        return True
    except Exception as e:
        print(f"Email failed: {e}")
        return False


# ─── Main ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Backtest Liquidity Sweep & Reclaim on Gold")
    ap.add_argument("--tf", default=None, help="Single timeframe (15m, 1h, 1d)")
    ap.add_argument("--email", action="store_true", help="Email report to baldwetcoby")
    ap.add_argument("--lookback", type=int, default=20, help="Lookback window for equal highs/lows")
    ap.add_argument("--min-touches", type=int, default=2, help="Min touches at resistance/support")
    ap.add_argument("--tol", type=float, default=0.3, help="ATR tolerance multiplier for equal levels")
    args = ap.parse_args()

    timeframes = [args.tf] if args.tf else ["15m", "1h", "1d"]
    all_results = []
    chart_paths = []

    print(f"{'=' * 60}")
    print(f"  Gold Liquidity Sweep & Reclaim — Backtest")
    print(f"  Timeframes: {', '.join(timeframes)}")
    print(f"  Lookback: {args.lookback} bars, Min touches: {args.min_touches}")
    print(f"{'=' * 60}")
    print()

    for tf in timeframes:
        print(f"Loading GC=F {tf} data...")
        df = load_all_gold(tf)
        if df is None:
            print(f"  No data for {tf}, skipping.")
            continue

        print(f"  {len(df):,} bars: {df['datetime'].iloc[0]} → {df['datetime'].iloc[-1]}")

        # Use optimized parameters when --lookback wasn't explicitly set
        opt = OPTIMIZED_PARAMS.get(tf, {})
        lb = args.lookback if args.lookback != 20 else opt.get("lookback", 20)
        tol = args.tol if args.tol != 0.3 else opt.get("tol_mult", 0.3)
        mt = args.min_touches if args.min_touches != 2 else opt.get("min_touches", 2)
        tgt = opt.get("target_mult", 2.5)
        trend = opt.get("trend_filter", False)

        print(f"  Params: lookback={lb}, tol={tol}, min_touches={mt}, "
              f"target={tgt}x ATR, trend_filter={trend}")
        print(f"  Running walk-forward backtest...")

        t0 = time.time()
        result = run_backtest(df, tf,
                            lookback=lb,
                            min_touches=mt,
                            tol_mult=tol,
                            target_mult=tgt,
                            trend_filter=trend)
        dur = time.time() - t0

        metrics = compute_metrics(result["trades"])
        yearly = yearly_breakdown(result["trades"])
        result["metrics"] = metrics
        result["yearly"] = yearly

        emoji = "+" if metrics["total_pnl"] > 0 else "-" if metrics["total_pnl"] < 0 else "="
        print(f"  [{emoji}] {tf}: {metrics['trades']} trades | "
              f"WR {metrics['win_rate']:.1f}% | P&L ${metrics['total_pnl']:>+,.2f} | "
              f"PF {metrics['profit_factor']:.2f} | Sharpe {metrics['sharpe']:.2f} | "
              f"{dur:.1f}s")

        # Generate charts
        eq_path = plot_equity(
            result["equity_curve"], result["equity_dates"],
            f"Liquidity Sweep & Reclaim — GC {tf.upper()} Equity Curve",
            os.path.join(OUT_DIR, f"equity_liquidity_sweep_GC_{tf}.png"),
        )
        if eq_path:
            chart_paths.append(eq_path)

        monthly_path = plot_monthly_returns(
            result["trades"],
            f"Monthly P&L — GC {tf.upper()}",
            os.path.join(OUT_DIR, f"monthly_liquidity_sweep_GC_{tf}.png"),
        )
        if monthly_path:
            chart_paths.append(monthly_path)

        scatter_path = plot_trade_scatter(
            result["trades"],
            f"Trade P&L Scatter — GC {tf.upper()}",
            os.path.join(OUT_DIR, f"scatter_liquidity_sweep_GC_{tf}.png"),
        )
        if scatter_path:
            chart_paths.append(scatter_path)

        # Save per-timeframe JSON
        json_path = os.path.join(OUT_DIR, f"liquidity_sweep_GC_{tf}.json")
        with open(json_path, "w") as f:
            # Strip large equity arrays for JSON
            save_data = {
                "strategy": "liquidity_sweep_reclaim",
                "symbol": SYMBOL,
                "timeframe": tf,
                "bars": result["bars_analyzed"],
                "date_range": result["date_range"],
                "metrics": metrics,
                "yearly": yearly,
                "trades": result["trades"],
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            json.dump(save_data, f, indent=2, default=str)
        print(f"  Saved: {json_path}")

        all_results.append(result)
        print()

    if not all_results:
        print("No results to report.")
        return

    # Build report
    report = build_text_report(all_results)
    report_path = os.path.join(OUT_DIR, "liquidity_sweep_report.txt")
    with open(report_path, "w") as f:
        f.write(report)
    print(f"Report saved: {report_path}")
    print()
    print(report)

    # Email if requested
    if args.email:
        send_email_report(report, chart_paths)


if __name__ == "__main__":
    main()

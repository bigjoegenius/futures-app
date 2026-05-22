#!/usr/bin/env python3
"""
optimize_liquidity_sweep.py — Parameter sweep for the Liquidity Sweep strategy.

Tests combinations of lookback, tolerance, min_touches, target_mult, and
whether to add a 50-EMA trend filter (only take shorts below EMA50, longs above).
"""

from __future__ import annotations

import itertools
import os
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from futures_config import DB_PATH
from backtest_liquidity_sweep import (
    load_all_gold, compute_atr, compute_metrics,
    POINT_VALUE, TICK_SIZE, FEE_PER_SIDE, SLIPPAGE_TICKS, STARTING_BALANCE,
)
from market_analyzer import ema


def detect_sweep_reclaim_v2(df, i, atr_val, lookback=20, tol_mult=0.3,
                            min_touches=2, target_mult=2.5,
                            trend_filter=False, ema50=None):
    if i < lookback + 2 or i >= len(df) or atr_val <= 0:
        return None

    tol = atr_val * tol_mult
    window_start = max(0, i - lookback - 1)
    window_end = i - 1
    window = df.iloc[window_start:window_end]
    if len(window) < 5:
        return None

    sweep_bar = df.iloc[i - 1]
    current_bar = df.iloc[i]
    current_close = float(current_bar["close"])

    # Trend filter
    ema50_val = None
    if trend_filter and ema50 is not None and i < len(ema50) and not pd.isna(ema50.iloc[i]):
        ema50_val = float(ema50.iloc[i])

    # ── BEARISH SWEEP ──
    highs = window["high"].values
    resistance = np.max(highs)
    touches = int(np.sum(np.abs(highs - resistance) < tol))

    if touches >= min_touches:
        sweep_high = float(sweep_bar["high"])
        if sweep_high > resistance:
            sweep_pen = (sweep_high - resistance) / atr_val
            if sweep_pen >= 0.05 and current_close < resistance:
                # Trend filter: only short below EMA50
                if trend_filter and ema50_val is not None and current_close > ema50_val:
                    pass  # skip this short — price above EMA50
                else:
                    stop = sweep_high + atr_val * 0.2
                    target = current_close - atr_val * target_mult
                    return {
                        "direction": "short",
                        "entry_price": current_close,
                        "stop_price": stop,
                        "target_price": target,
                        "entry_time": str(current_bar["datetime"]),
                    }

    # ── BULLISH SWEEP ──
    lows = window["low"].values
    support = np.min(lows)
    touches_lo = int(np.sum(np.abs(lows - support) < tol))

    if touches_lo >= min_touches:
        sweep_low = float(sweep_bar["low"])
        if sweep_low < support:
            sweep_pen = (support - sweep_low) / atr_val
            if sweep_pen >= 0.05 and current_close > support:
                # Trend filter: only long above EMA50
                if trend_filter and ema50_val is not None and current_close < ema50_val:
                    pass  # skip this long — price below EMA50
                else:
                    stop = sweep_low - atr_val * 0.2
                    target = current_close + atr_val * target_mult
                    return {
                        "direction": "long",
                        "entry_price": current_close,
                        "stop_price": stop,
                        "target_price": target,
                        "entry_time": str(current_bar["datetime"]),
                    }

    return None


def run_backtest_v2(df, lookback=20, tol_mult=0.3, min_touches=2,
                    target_mult=2.5, trend_filter=False, risk_pct=0.01):
    atr_series = compute_atr(df, 14)
    ema50_series = ema(df["close"], 50) if trend_filter else None

    balance = STARTING_BALANCE
    trades = []
    position = None

    min_start = max(60, lookback + 5)

    for i in range(min_start, len(df)):
        bar = df.iloc[i]
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])
        bar_time = str(bar["datetime"])
        atr_val = float(atr_series.iloc[i]) if not pd.isna(atr_series.iloc[i]) else 0

        if position is not None:
            if position["direction"] == "short":
                hit_stop = bar_high >= position["stop_price"]
                hit_target = bar_low <= position["target_price"]
            else:
                hit_stop = bar_low <= position["stop_price"]
                hit_target = bar_high >= position["target_price"]

            exit_price = reason = None
            if hit_stop and hit_target:
                exit_price = position["stop_price"]; reason = "stop"
            elif hit_stop:
                exit_price = position["stop_price"]; reason = "stop"
            elif hit_target:
                exit_price = position["target_price"]; reason = "target"

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
                balance += pnl
                trades.append({"pnl_dollars": round(pnl, 2), "exit_reason": reason,
                              "direction": position["direction"]})
                position = None

        if position is None and atr_val > 0:
            signal = detect_sweep_reclaim_v2(
                df, i, atr_val, lookback, tol_mult, min_touches,
                target_mult, trend_filter, ema50_series,
            )
            if signal is not None:
                slip = SLIPPAGE_TICKS * TICK_SIZE
                fill = signal["entry_price"] + slip if signal["direction"] == "long" else signal["entry_price"] - slip
                stop_dist = abs(fill - signal["stop_price"])
                loss_per_contract = stop_dist * POINT_VALUE
                if loss_per_contract > 0:
                    contracts = max(round((balance * risk_pct) / loss_per_contract, 1), 0.1)
                    position = {
                        "direction": signal["direction"],
                        "entry_fill": fill,
                        "stop_price": signal["stop_price"],
                        "target_price": signal["target_price"],
                        "contracts": contracts,
                    }

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
        trades.append({"pnl_dollars": round(pnl, 2), "exit_reason": "end",
                      "direction": position["direction"]})

    return trades


def main():
    timeframes = ["15m", "1h", "1d"]
    lookbacks = [10, 15, 20, 30]
    tol_mults = [0.2, 0.3, 0.5]
    min_touches_list = [2, 3]
    target_mults = [1.8, 2.0, 2.5, 3.0]
    trend_filters = [False, True]

    combos = list(itertools.product(
        lookbacks, tol_mults, min_touches_list, target_mults, trend_filters
    ))

    print(f"Testing {len(combos)} parameter combinations per timeframe...")
    print(f"{'TF':<5} {'LB':>4} {'Tol':>5} {'MT':>3} {'TGT':>5} {'Trend':>6} "
          f"{'Trades':>7} {'WR':>6} {'P&L':>12} {'PF':>6} {'Sharpe':>7} {'MaxDD':>7}")
    print("-" * 80)

    all_best = {}

    for tf in timeframes:
        df = load_all_gold(tf)
        if df is None:
            print(f"No data for {tf}")
            continue

        best_pf = {"pnl": -999999, "params": None, "metrics": None}
        best_sharpe = {"sharpe": -999, "params": None, "metrics": None}

        for lb, tol, mt, tgt, trend in combos:
            trades = run_backtest_v2(df, lb, tol, mt, tgt, trend)
            if not trades:
                continue
            m = compute_metrics(trades)
            if m["trades"] < 10:
                continue

            if m["total_pnl"] > best_pf["pnl"]:
                best_pf = {"pnl": m["total_pnl"], "params": (lb, tol, mt, tgt, trend), "metrics": m}
            if m["sharpe"] > best_sharpe["sharpe"]:
                best_sharpe = {"sharpe": m["sharpe"], "params": (lb, tol, mt, tgt, trend), "metrics": m}

        if best_pf["params"]:
            lb, tol, mt, tgt, trend = best_pf["params"]
            m = best_pf["metrics"]
            print(f"{tf:<5} {lb:>4} {tol:>5.1f} {mt:>3} {tgt:>5.1f} {'yes' if trend else 'no':>6} "
                  f"{m['trades']:>7} {m['win_rate']:>5.1f}% ${m['total_pnl']:>+10,.2f} "
                  f"{m['profit_factor']:>5.2f} {m['sharpe']:>6.2f} {m['max_drawdown_pct']:>6.1f}%  <- best P&L")

        if best_sharpe["params"] and best_sharpe["params"] != best_pf.get("params"):
            lb, tol, mt, tgt, trend = best_sharpe["params"]
            m = best_sharpe["metrics"]
            print(f"{tf:<5} {lb:>4} {tol:>5.1f} {mt:>3} {tgt:>5.1f} {'yes' if trend else 'no':>6} "
                  f"{m['trades']:>7} {m['win_rate']:>5.1f}% ${m['total_pnl']:>+10,.2f} "
                  f"{m['profit_factor']:>5.2f} {m['sharpe']:>6.2f} {m['max_drawdown_pct']:>6.1f}%  <- best Sharpe")

        all_best[tf] = {"best_pnl": best_pf, "best_sharpe": best_sharpe}

    print()
    print("=" * 60)
    print("OPTIMAL PARAMETERS PER TIMEFRAME")
    print("=" * 60)
    for tf, bests in all_best.items():
        bp = bests["best_pnl"]
        if bp["params"]:
            lb, tol, mt, tgt, trend = bp["params"]
            m = bp["metrics"]
            print(f"\n{tf.upper()} (Best P&L):")
            print(f"  lookback={lb}, tol_mult={tol}, min_touches={mt}, "
                  f"target_mult={tgt}, trend_filter={trend}")
            print(f"  Trades={m['trades']}, WR={m['win_rate']:.1f}%, "
                  f"P&L=${m['total_pnl']:+,.2f}, PF={m['profit_factor']:.2f}, "
                  f"Sharpe={m['sharpe']:.2f}, MaxDD={m['max_drawdown_pct']:.1f}%")


if __name__ == "__main__":
    main()

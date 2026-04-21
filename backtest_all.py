#!/usr/bin/env python3
"""
backtest_all.py — Run every (strategy × market × timeframe) combination from the catalog.

Outputs:
  - backtest_results table in futures.db (one row per combo)
  - backtest_trades table in futures.db (every trade with news tags)
  - backtests/<combo>.json — per-combo summary + trade list
  - backtests/equity_<combo>.png — equity curve

Usage:
  python backtest_all.py              # run everything
  python backtest_all.py --top 20     # only the top-20 most-history combos
  python backtest_all.py --symbol ES=F # one market
  python backtest_all.py --strategy donchian_20
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MPL_OK = True
except Exception:
    MPL_OK = False

from futures_config import DB_PATH, FUTURES, TIMEFRAMES
from market_analyzer import STRATEGIES, load_bars
from strategy_engine import backtest_symbol


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "backtests")
os.makedirs(OUT_DIR, exist_ok=True)

NEWS_PATH = os.path.join(BASE_DIR, "news_fixtures.json")

# ─── Session buckets (ET hours, Mon-Fri) ────────────────────────────────
# Futures are ~23/5 not 24/7. Liquidity clusters in US RTH (9:30-16:00 ET).
# Bucket a trade's entry timestamp so we can tell whether a strategy
# actually earns outside US hours or just gets chopped up in thin books.
SESSION_BUCKETS = [
    ("Weekend",    None, None),  # any Sat/Sun bar
    ("Asia",       18,   3),     # 6pm ET -> 3am ET (wraps midnight)
    ("London",     3,    8),     # 3am ET -> 8am ET
    ("US_PreOpen", 8,    9),     # 8am-9:30am approximated to 8-9
    ("US_RTH",     9,    16),    # 9am-4pm ET (regular trading hours)
    ("US_Post",    16,   18),    # 4pm-6pm ET (post-close thin book)
]


def session_for(ts_iso: str) -> str:
    """Return the session bucket for a trade entry timestamp (ISO string)."""
    if not ts_iso:
        return "Unknown"
    try:
        t = pd.to_datetime(ts_iso, utc=True)
    except Exception:
        return "Unknown"
    if pd.isna(t):
        return "Unknown"
    # ET offset (approximate — does not DST-switch; acceptable for bucketing)
    t_et = t.tz_convert("America/New_York")
    if t_et.weekday() >= 5:
        return "Weekend"
    h = t_et.hour
    for name, start, end in SESSION_BUCKETS:
        if start is None:
            continue
        if start <= end:
            if start <= h < end:
                return name
        else:
            if h >= start or h < end:
                return name
    return "Other"


# ─── Schema ─────────────────────────────────────────────────────────────
def ensure_schema():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            strategy TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            bars INTEGER,
            trades INTEGER,
            wins INTEGER,
            losses INTEGER,
            win_rate REAL,
            total_pnl REAL,
            final_balance REAL,
            profit_factor REAL,
            max_drawdown REAL,
            sharpe REAL,
            avg_win REAL,
            avg_loss REAL,
            duration_sec REAL,
            completed_at TEXT,
            UNIQUE(run_id, strategy, symbol, timeframe)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            strategy TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            direction TEXT,
            entry_time TEXT,
            entry_price REAL,
            exit_time TEXT,
            exit_price REAL,
            stop_price REAL,
            target_price REAL,
            contracts REAL,
            pnl_dollars REAL,
            pnl_pct REAL,
            fees REAL,
            exit_reason TEXT,
            news_tags TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_btr_lookup ON backtest_results(strategy, symbol, timeframe)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_btt_lookup ON backtest_trades(strategy, symbol, timeframe)")
    conn.commit()
    conn.close()


# ─── News overlay ───────────────────────────────────────────────────────
def load_news() -> dict:
    try:
        with open(NEWS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def news_tags_for_trade(news: dict, entry_iso: str, exit_iso: str) -> str:
    """Return a comma-separated list of event tags overlapping the trade window."""
    try:
        start = pd.to_datetime(entry_iso)
        end = pd.to_datetime(exit_iso) if exit_iso else start
    except Exception:
        return ""
    if pd.isna(start):
        return ""
    if pd.isna(end):
        end = start
    window_start = start - pd.Timedelta(hours=4)
    window_end = end + pd.Timedelta(hours=4)
    tags = []
    for dstr in news.get("fomc_dates", []):
        d = pd.Timestamp(dstr).tz_localize(None) if not str(dstr).endswith("00") else pd.Timestamp(dstr)
        if window_start <= d <= window_end:
            tags.append(f"FOMC({dstr})")
    for dstr in news.get("wasde_dates", []):
        d = pd.Timestamp(dstr)
        if window_start <= d <= window_end:
            tags.append(f"WASDE({dstr})")
    for ev in news.get("major_events", []):
        d = pd.Timestamp(ev.get("date"))
        if pd.isna(d):
            continue
        if window_start <= d <= window_end:
            tags.append(f"{ev.get('event','?')}({ev.get('date')})")
    for dstr in news.get("opec_meetings", []):
        d = pd.Timestamp(dstr)
        if window_start <= d <= window_end:
            tags.append(f"OPEC({dstr})")
    return ",".join(tags[:8])  # cap to keep columns tidy


# ─── Metrics ────────────────────────────────────────────────────────────
def compute_metrics(trades: list[dict], starting_balance: float = 10_000.0) -> dict:
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "final_balance": starting_balance,
            "profit_factor": 0.0, "max_drawdown": 0.0, "sharpe": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0,
        }
    pnls = np.array([t.get("pnl_dollars", 0.0) for t in trades], dtype=float)
    wins_mask = pnls > 0
    total_pnl = float(pnls.sum())
    wins = int(wins_mask.sum())
    losses = int((~wins_mask).sum())
    win_rate = (wins / len(pnls) * 100.0) if len(pnls) else 0.0

    gross_win = float(pnls[wins_mask].sum())
    gross_loss = float(-pnls[~wins_mask].sum())
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)

    # Equity curve
    equity = starting_balance + np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.where(peak > 0, peak, 1)
    max_dd = float(dd.max() * 100) if len(dd) else 0.0

    # Sharpe (per-trade, annualized assumption 252 trades/year for dailyish)
    if pnls.std() > 0:
        per_trade = pnls / starting_balance
        sharpe = float((per_trade.mean() / per_trade.std()) * math.sqrt(252))
    else:
        sharpe = 0.0

    avg_win = float(pnls[wins_mask].mean()) if wins else 0.0
    avg_loss = float(pnls[~wins_mask].mean()) if losses else 0.0

    return {
        "trades": len(pnls), "wins": wins, "losses": losses, "win_rate": win_rate,
        "total_pnl": total_pnl, "final_balance": starting_balance + total_pnl,
        "profit_factor": round(profit_factor, 3) if math.isfinite(profit_factor) else 0.0,
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 3),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
    }


# ─── Equity curve PNG ───────────────────────────────────────────────────
def plot_equity(trades: list[dict], title: str, out_path: str, starting_balance: float = 10_000.0):
    if not MPL_OK or not trades:
        return
    pnls = [t.get("pnl_dollars", 0) for t in trades]
    equity = [starting_balance] + list(np.array(pnls).cumsum() + starting_balance)
    x = list(range(len(equity)))
    plt.figure(figsize=(8, 3.5))
    plt.plot(x, equity, "-", color="#3fb950", linewidth=1.4)
    plt.fill_between(x, equity, starting_balance,
                      where=[e >= starting_balance for e in equity],
                      color="#3fb950", alpha=0.15)
    plt.fill_between(x, equity, starting_balance,
                      where=[e < starting_balance for e in equity],
                      color="#f85149", alpha=0.15)
    plt.axhline(starting_balance, color="#8b949e", linestyle="--", linewidth=0.8)
    plt.title(title, fontsize=10)
    plt.xlabel("Trade #"); plt.ylabel("Equity $")
    plt.grid(True, alpha=0.15)
    plt.tight_layout()
    plt.savefig(out_path, dpi=100)
    plt.close()


# ─── Main runner ────────────────────────────────────────────────────────
def bars_for_tf(tf: str) -> int:
    """How many bars to use for a backtest per timeframe (balanced for runtime).

    Lower numbers give faster runs but less statistical significance.
    Even at these reduced counts, a 500-bar run on 1d is ~2 years of data."""
    return {
        "1m": 2000,
        "5m": 3000,
        "15m": 2000,
        "1h": 2000,
        "1d": 1500,
    }.get(tf, 1000)


def valid_combos(symbol_filter: str | None = None, strategy_filter: str | None = None) -> list[tuple]:
    """Yield (strategy, symbol, timeframe) tuples that the catalog says are valid."""
    combos = []
    all_symbols = list(FUTURES.keys())
    for sid, spec in STRATEGIES.items():
        if strategy_filter and sid != strategy_filter:
            continue
        strat_markets = spec.get("markets", ["all"])
        strat_tfs = spec.get("timeframes", [])
        markets = all_symbols if "all" in strat_markets else strat_markets
        for sym in markets:
            if symbol_filter and sym != symbol_filter:
                continue
            for tf in strat_tfs:
                combos.append((sid, sym, tf))
    return combos


def run_one(run_id: str, strategy: str, symbol: str, tf: str, news: dict) -> dict:
    t0 = time.time()
    bars = bars_for_tf(tf)
    rep = backtest_symbol(
        symbol=symbol, timeframe=tf, bars=bars,
        strategies=[strategy],
        starting_balance=10_000.0,
        risk_mode="moderate",
    )
    dur = time.time() - t0
    if "error" in rep:
        return {"strategy": strategy, "symbol": symbol, "timeframe": tf,
                "error": rep["error"], "duration_sec": dur}

    trades = rep.get("trades_detail", [])
    metrics = compute_metrics(trades)
    combo_key = f"{strategy}_{symbol.replace('=F','')}_{tf}"

    # Tag trades with news + session bucket
    for t in trades:
        t["news_tags"] = news_tags_for_trade(news, t.get("entry_time", ""), t.get("exit_time", ""))
        t["session"] = session_for(t.get("entry_time", ""))

    # Persist
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO backtest_results "
        "(run_id, strategy, symbol, timeframe, bars, trades, wins, losses, win_rate, total_pnl, "
        " final_balance, profit_factor, max_drawdown, sharpe, avg_win, avg_loss, duration_sec, completed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, strategy, symbol, tf, bars,
         metrics["trades"], metrics["wins"], metrics["losses"], metrics["win_rate"],
         metrics["total_pnl"], metrics["final_balance"], metrics["profit_factor"],
         metrics["max_drawdown"], metrics["sharpe"], metrics["avg_win"], metrics["avg_loss"],
         dur, datetime.now(timezone.utc).isoformat())
    )
    for t in trades:
        conn.execute(
            "INSERT INTO backtest_trades "
            "(run_id, strategy, symbol, timeframe, direction, entry_time, entry_price, "
            " exit_time, exit_price, stop_price, target_price, contracts, pnl_dollars, pnl_pct, "
            " fees, exit_reason, news_tags) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, strategy, symbol, tf,
             t.get("direction"), t.get("entry_time"), t.get("entry_price"),
             t.get("exit_time"), t.get("exit_price"), t.get("stop_price"), t.get("target_price"),
             t.get("contracts"), t.get("pnl_dollars"), t.get("pnl_pct"),
             t.get("fees"), t.get("exit_reason"), t.get("news_tags", ""))
        )
    conn.commit()
    conn.close()

    # Per-combo JSON
    json_path = os.path.join(OUT_DIR, f"{combo_key}.json")
    with open(json_path, "w") as f:
        json.dump({
            "strategy": strategy, "symbol": symbol, "timeframe": tf, "bars": bars,
            "metrics": metrics, "trades": trades,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2, default=str)

    # Equity curve (skip if zero trades)
    if trades and MPL_OK:
        png_path = os.path.join(OUT_DIR, f"equity_{combo_key}.png")
        plot_equity(trades, f"{strategy} {symbol} {tf}", png_path)

    out = {"strategy": strategy, "symbol": symbol, "timeframe": tf,
           "duration_sec": round(dur, 2), **metrics}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    ensure_schema()
    news = load_news()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    combos = valid_combos(args.symbol, args.strategy)
    if args.top:
        combos = combos[: args.top]

    print(f"Running {len(combos)} (strategy × symbol × tf) combinations...")
    results = []
    for i, (sid, sym, tf) in enumerate(combos, 1):
        try:
            r = run_one(run_id, sid, sym, tf, news)
            results.append(r)
            prefix = f"[{i:>3}/{len(combos)}]"
            if "error" in r:
                print(f"{prefix} {sid} {sym} {tf}  ERROR: {r['error']}")
            else:
                emoji = "🟢" if r["total_pnl"] > 0 else ("🔴" if r["total_pnl"] < 0 else "⚪")
                print(f"{prefix} {emoji} {sid:<22} {sym:<6} {tf:<4}  "
                      f"trades={r['trades']:>4}  WR={r['win_rate']:>5.1f}%  "
                      f"P&L=${r['total_pnl']:>+9.2f}  PF={r['profit_factor']:>5.2f}  "
                      f"{r['duration_sec']}s")
        except Exception as e:
            print(f"[{i}/{len(combos)}] {sid} {sym} {tf}  EXCEPTION: {e}")

    # Summary
    wins = [r for r in results if r.get("total_pnl", 0) > 0]
    losses = [r for r in results if r.get("total_pnl", 0) <= 0 and "error" not in r]
    print(f"\n{'=' * 70}")
    print(f"Run {run_id}: {len(results)} combos | profitable={len(wins)} losing={len(losses)}")
    if wins:
        top5 = sorted(wins, key=lambda r: r["total_pnl"], reverse=True)[:5]
        print("\nTop 5 by total P&L:")
        for r in top5:
            print(f"  {r['strategy']:<22} {r['symbol']:<6} {r['timeframe']:<4}  "
                  f"${r['total_pnl']:>+9.2f}  WR {r['win_rate']:.1f}%  Sharpe {r['sharpe']:.2f}")
    print(f"{'=' * 70}")

    # ─── Session breakdown across all trades in this run ────────────────
    try:
        report_session_breakdown(run_id)
    except Exception as e:
        print(f"(session breakdown failed: {e})")


def report_session_breakdown(run_id: str) -> None:
    """Aggregate this run's trades by session bucket and print a summary."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT strategy, symbol, timeframe, entry_time, pnl_dollars "
        "FROM backtest_trades WHERE run_id=?",
        (run_id,)
    ).fetchall()
    conn.close()
    if not rows:
        return

    by_session: dict[str, dict] = {}
    by_strategy_session: dict[tuple, dict] = {}
    for strat, sym, tf, entry_time, pnl in rows:
        sess = session_for(entry_time)
        pnl_val = float(pnl or 0)
        b = by_session.setdefault(sess, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["pnl"] += pnl_val
        if pnl_val > 0:
            b["wins"] += 1
        sk = (strat, sess)
        sb = by_strategy_session.setdefault(sk, {"n": 0, "wins": 0, "pnl": 0.0})
        sb["n"] += 1
        sb["pnl"] += pnl_val
        if pnl_val > 0:
            sb["wins"] += 1

    print("\nSession breakdown — all trades in this run")
    print(f"{'Session':<12} {'Trades':>7} {'WR':>7} {'Total P&L':>14} {'Avg/Trade':>12}")
    order = ["US_RTH", "US_PreOpen", "US_Post", "London", "Asia", "Weekend", "Other", "Unknown"]
    for sess in order:
        if sess not in by_session:
            continue
        b = by_session[sess]
        wr = (b["wins"] / b["n"] * 100) if b["n"] else 0.0
        avg = b["pnl"] / b["n"] if b["n"] else 0.0
        print(f"{sess:<12} {b['n']:>7} {wr:>6.1f}% ${b['pnl']:>+12.2f} ${avg:>+10.2f}")

    # Save a JSON for the Word report generator and downstream analysis
    out = {
        "run_id": run_id,
        "by_session": by_session,
        "by_strategy_session": {
            f"{s}__{sess}": v for (s, sess), v in by_strategy_session.items()
        },
    }
    out_path = os.path.join(OUT_DIR, f"session_breakdown_{run_id}.json")
    try:
        with open(out_path, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nSaved session breakdown → {out_path}")
    except Exception as e:
        print(f"(session JSON write failed: {e})")


if __name__ == "__main__":
    main()

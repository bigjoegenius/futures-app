#!/usr/bin/env python3
"""
strategy_engine.py — Paper-trading engine for the futures app.

What it does:
  - You call engine.step(symbol, df) every bar (or on a schedule).
  - It checks each enabled strategy for an entry signal.
  - It opens / manages / closes paper positions with stops + targets.
  - Closed trades get written to the `trades` table and also appended to
    trade_log.json (mirrors crypto-app's format).

Risk sizing:
  - Each trade risks a fixed % of account equity based on risk_mode:
      conservative = 0.5%, moderate = 1%, aggressive = 2%
  - Position size is computed from (risk_dollars / distance_to_stop_per_contract).
  - Stop is an ATR-based distance; target is 1.8x ATR by default (1R:1.8R).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd

from futures_config import DB_PATH, FUTURES
from market_analyzer import score_strategies, load_bars, atr, STRATEGIES


TRADE_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_log.json")


# ─── Contract specs (point value, tick) ─────────────────────────────────
# Approximations — these are the standard CME front-month point values.
CONTRACT_SPECS: dict[str, dict] = {
    # Equity indexes
    "ES=F":  {"name": "E-mini S&P 500",    "point_value": 50.0,  "tick": 0.25},
    "NQ=F":  {"name": "E-mini Nasdaq 100", "point_value": 20.0,  "tick": 0.25},
    "YM=F":  {"name": "E-mini Dow",        "point_value": 5.0,   "tick": 1.0},
    "RTY=F": {"name": "E-mini Russell",    "point_value": 50.0,  "tick": 0.10},
    # Energy
    "CL=F":  {"name": "WTI Crude Oil",     "point_value": 1000.0, "tick": 0.01},
    "NG=F":  {"name": "Natural Gas",       "point_value": 10000.0, "tick": 0.001},
    # Metals
    "GC=F":  {"name": "Gold",              "point_value": 100.0, "tick": 0.10},
    "SI=F":  {"name": "Silver",            "point_value": 5000.0, "tick": 0.005},
    "HG=F":  {"name": "Copper",            "point_value": 25000.0, "tick": 0.0005},
    # Bonds
    "ZB=F":  {"name": "30Y T-Bond",        "point_value": 1000.0, "tick": 1/32},
    "ZN=F":  {"name": "10Y T-Note",        "point_value": 1000.0, "tick": 1/64},
    # Ags
    "ZC=F":  {"name": "Corn",              "point_value": 50.0,  "tick": 0.25},
    "ZS=F":  {"name": "Soybeans",          "point_value": 50.0,  "tick": 0.25},
    "ZW=F":  {"name": "Wheat",             "point_value": 50.0,  "tick": 0.25},
    "KC=F":  {"name": "Coffee",            "point_value": 375.0, "tick": 0.05},
    "SB=F":  {"name": "Sugar",             "point_value": 1120.0, "tick": 0.01},
    "CT=F":  {"name": "Cotton",            "point_value": 500.0, "tick": 0.01},
    "LE=F":  {"name": "Live Cattle",       "point_value": 400.0, "tick": 0.025},
}

# Generic per-side commission + slippage estimate for paper trading
FEE_PER_CONTRACT_PER_SIDE = 2.50   # $ per contract, per open or close
SLIPPAGE_TICKS            = 1       # extra ticks against you on fill

RISK_MODES = {"conservative": 0.005, "moderate": 0.01, "aggressive": 0.02}
# All 28 strategies from market_analyzer.STRATEGIES catalog
DEFAULT_STRATEGIES = list(STRATEGIES.keys())


@dataclass
class TradeReport:
    symbol: str
    strategy: str
    direction: str              # "long" | "short"
    entry_time: str
    entry_price: float
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    stop_price: float = 0.0
    target_price: float = 0.0
    contracts: float = 0.0
    pnl_dollars: float = 0.0
    pnl_pct: float = 0.0
    fees: float = 0.0
    exit_reason: Optional[str] = None
    status: str = "open"
    # 0-100 probability this trade hits TP before SL, derived from the raw
    # strategy score. Populated at open; preserved through close.
    confidence: Optional[float] = None
    confidence_source: str = ""


@dataclass
class Position:
    symbol: str
    strategy: str
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    contracts: float
    entry_time: str
    atr_at_entry: float
    confidence: Optional[float] = None
    confidence_source: str = ""


class StrategyEngine:
    def __init__(
        self,
        starting_balance: float = 10_000.0,
        risk_mode: str = "moderate",
        enabled_strategies: list[str] | None = None,
        on_trade_closed: Callable[[TradeReport], None] | None = None,
    ):
        self.balance = float(starting_balance)
        self.starting_balance = float(starting_balance)
        self.risk_mode = risk_mode if risk_mode in RISK_MODES else "moderate"
        self.enabled: set[str] = set(enabled_strategies or DEFAULT_STRATEGIES)
        self.positions: dict[str, Position] = {}   # one position per symbol
        self.closed: list[TradeReport] = []
        self.on_trade_closed = on_trade_closed
        self.min_score_to_enter = 60.0
        self._last_entry_bar: dict[str, str] = {}

    # ── Config ─────────────────────────────────────────────────────────
    def set_risk(self, mode: str) -> None:
        if mode in RISK_MODES:
            self.risk_mode = mode

    def enable(self, strategy: str) -> None:
        self.enabled.add(strategy)

    def disable(self, strategy: str) -> None:
        self.enabled.discard(strategy)

    # ── Core loop ──────────────────────────────────────────────────────
    def step(self, symbol: str, df: pd.DataFrame, timeframe: str = "1h",
             live_price: Optional[float] = None) -> None:
        """Run one tick of the engine for one symbol.

        `live_price`, when provided, is used as the fill price for new market
        entries and widens the high/low envelope for stop/target checks so
        they can trigger mid-bar. Leave it None for backtests (walk-forward
        replay must use only closed-bar data)."""
        if df is None or len(df) < 60:
            return
        spec = CONTRACT_SPECS.get(symbol)
        if not spec:
            return

        bar_close = float(df["close"].iloc[-1])
        bar_high = float(df["high"].iloc[-1])
        bar_low = float(df["low"].iloc[-1])
        lp = float(live_price) if live_price is not None else None
        # For market entries, fill at the live tick when it's available;
        # otherwise fall back to the last bar's close (legacy behavior).
        last = lp if lp is not None else bar_close
        # Widen the bar envelope with the live tick so stops/targets can
        # trigger between bar boundaries.
        high = max(bar_high, lp) if lp is not None else bar_high
        low = min(bar_low, lp) if lp is not None else bar_low
        now = df["datetime"].iloc[-1].isoformat() if "datetime" in df else datetime.now(timezone.utc).isoformat()

        # One entry per bar per symbol — without this, a 5-min step loop over
        # 1h bars re-opens identical trades when the bar already bracketed the target.
        is_new_bar = self._last_entry_bar.get(symbol) != now

        pos = self.positions.get(symbol)
        if pos is not None:
            # Check EVERY bar from entry onward, not just the latest. Without
            # this, a position whose TP/SL was hit on any bar other than the
            # current one gets stuck open forever (2026-04-22 phantom-open bug).
            self._sweep_manage_position(pos, df, spec)
            if symbol not in self.positions:
                pass  # was closed during sweep
            else:
                # Fallback to last-bar check in case sweep couldn't resolve
                # (e.g. entry_time not found in df)
                self._manage_position(pos, high, low, last, now, spec)
                if symbol in self.positions:
                    return

        if not is_new_bar:
            return
        self._last_entry_bar[symbol] = now

        scores = score_strategies(symbol, df, tf=timeframe, strategy_filter=self.enabled)
        candidates = []
        for sid, info in scores.items():
            if sid not in self.enabled:
                continue
            if info["direction"] == "NONE":
                continue
            if info["score"] < self.min_score_to_enter:
                continue
            candidates.append((sid, info))

        if not candidates:
            return

        # pick the highest-scoring candidate
        candidates.sort(key=lambda kv: kv[1]["score"], reverse=True)
        sid, info = candidates[0]
        atr_val = float(info["signals"].get("atr", max(last * 0.01, spec["tick"] * 4)))
        raw_score = float(info.get("score", 0.0))
        self._open_position(symbol, sid, info["direction"].lower(), last, atr_val, now, spec,
                            raw_score=raw_score)

    # ── Confidence estimation ─────────────────────────────────────────
    def _estimate_confidence(self, strategy: str, direction: str,
                             raw_score: float) -> tuple[Optional[float], str]:
        """
        Produce a 0-100 probability-of-success estimate from the raw
        strategy score.
        """
        if raw_score is None:
            return None, ""
        raw = max(0.0, min(100.0, float(raw_score)))
        return round(raw, 1), "raw"

    # ── Position management ────────────────────────────────────────────
    def _open_position(self, symbol, strategy, direction, price, atr_val, ts, spec,
                        *, raw_score: float = 0.0):
        # Per-strategy stop/target multipliers (fall back to 1.2/1.8)
        scfg = STRATEGIES.get(strategy, {})
        stop_mult = float(scfg.get("stop_atr_mult", 1.2))
        target_mult = float(scfg.get("target_atr_mult", 1.8))
        stop_dist = max(atr_val * stop_mult, spec["tick"] * 8)
        target_dist = atr_val * target_mult if target_mult >= 1.0 else stop_dist * target_mult
        if direction == "long":
            stop = price - stop_dist
            target = price + target_dist
        else:
            stop = price + stop_dist
            target = price - target_dist

        risk_dollars = self.balance * RISK_MODES[self.risk_mode]
        # $ loss per contract if stop hits
        loss_per_contract = stop_dist * spec["point_value"]
        if loss_per_contract <= 0:
            return
        contracts = risk_dollars / loss_per_contract
        # Round down to nearest 0.1 contracts (fractional allowed for paper)
        contracts = max(round(contracts, 1), 0.1)

        # Apply slippage on entry
        slip = SLIPPAGE_TICKS * spec["tick"]
        fill_price = price + slip if direction == "long" else price - slip

        confidence, conf_source = self._estimate_confidence(strategy, direction, raw_score)

        self.positions[symbol] = Position(
            symbol=symbol,
            strategy=strategy,
            direction=direction,
            entry_price=fill_price,
            stop_price=stop,
            target_price=target,
            contracts=contracts,
            entry_time=ts,
            atr_at_entry=atr_val,
            confidence=confidence,
            confidence_source=conf_source,
        )

    def _manage_position(self, pos: Position, bar_high, bar_low, bar_close, ts, spec):
        """Check if stop or target was hit on this bar. Close if so."""
        hit_stop = (pos.direction == "long" and bar_low <= pos.stop_price) or \
                   (pos.direction == "short" and bar_high >= pos.stop_price)
        hit_target = (pos.direction == "long" and bar_high >= pos.target_price) or \
                     (pos.direction == "short" and bar_low <= pos.target_price)

        if hit_stop and hit_target:
            # Assume stop fills first (conservative)
            self._close_position(pos, pos.stop_price, ts, "stop", spec)
        elif hit_stop:
            self._close_position(pos, pos.stop_price, ts, "stop", spec)
        elif hit_target:
            self._close_position(pos, pos.target_price, ts, "target", spec)

    def _sweep_manage_position(self, pos, df: "pd.DataFrame", spec) -> None:
        """Walk every bar from the position's entry time forward and close at
        the FIRST bar whose high/low breaches stop or target. This is the
        correct implementation of 'has the trade exited yet' — the previous
        last-bar-only version missed exits that happened on intermediate
        bars, leaving phantom-open positions in memory.

        Bars are assumed ordered oldest→newest with a 'datetime' column.
        """
        if df is None or df.empty:
            return
        # Find the first bar whose datetime is > entry_time. That's the first
        # bar that could have closed the trade (entry bar itself doesn't —
        # the position opened at its close).
        try:
            entry_ts = pd.to_datetime(pos.entry_time, utc=True, errors="coerce")
            if pd.isna(entry_ts):
                return
            times = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
        except Exception:
            return
        # Iterate bars after entry. Close on the first TP/SL breach we find.
        after_mask = times > entry_ts
        if not after_mask.any():
            return
        sub = df[after_mask]
        for _, bar in sub.iterrows():
            try:
                high = float(bar["high"])
                low = float(bar["low"])
                bts = bar.get("datetime") if hasattr(bar, "get") else bar["datetime"]
                ts = bts.isoformat() if hasattr(bts, "isoformat") else str(bts)
            except Exception:
                continue
            hit_stop = (pos.direction == "long" and low <= pos.stop_price) or \
                       (pos.direction == "short" and high >= pos.stop_price)
            hit_target = (pos.direction == "long" and high >= pos.target_price) or \
                         (pos.direction == "short" and low <= pos.target_price)
            if hit_stop and hit_target:
                self._close_position(pos, pos.stop_price, ts, "stop", spec)
                return
            if hit_stop:
                self._close_position(pos, pos.stop_price, ts, "stop", spec)
                return
            if hit_target:
                self._close_position(pos, pos.target_price, ts, "target", spec)
                return

    def close_all(self, reason: str = "manual") -> None:
        """Force-close every open position at its last known price."""
        for sym, pos in list(self.positions.items()):
            df = load_bars(sym, "1h", 5) or load_bars(sym, "1d", 5)
            if df is None or df.empty:
                continue
            last = float(df["close"].iloc[-1])
            spec = CONTRACT_SPECS.get(sym)
            if spec:
                self._close_position(pos, last, datetime.now(timezone.utc).isoformat(), reason, spec)

    def _close_position(self, pos: Position, exit_price: float, ts: str, reason: str, spec: dict):
        slip = SLIPPAGE_TICKS * spec["tick"]
        fill = exit_price - slip if pos.direction == "long" else exit_price + slip

        if pos.direction == "long":
            gross = (fill - pos.entry_price) * spec["point_value"] * pos.contracts
        else:
            gross = (pos.entry_price - fill) * spec["point_value"] * pos.contracts

        fees = FEE_PER_CONTRACT_PER_SIDE * 2 * pos.contracts
        pnl = gross - fees
        pnl_pct = (pnl / self.balance) * 100 if self.balance else 0.0
        self.balance += pnl

        tr = TradeReport(
            symbol=pos.symbol,
            strategy=pos.strategy,
            direction=pos.direction,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            exit_time=ts,
            exit_price=fill,
            stop_price=pos.stop_price,
            target_price=pos.target_price,
            contracts=pos.contracts,
            pnl_dollars=pnl,
            pnl_pct=pnl_pct,
            fees=fees,
            exit_reason=reason,
            status="closed",
            confidence=pos.confidence,
            confidence_source=pos.confidence_source,
        )
        self.closed.append(tr)
        self._persist_trade(tr)
        del self.positions[pos.symbol]
        if self.on_trade_closed:
            try:
                self.on_trade_closed(tr)
            except Exception:
                pass

    # ── Persistence ────────────────────────────────────────────────────
    def _persist_trade(self, tr: TradeReport) -> None:
        # SQLite
        try:
            conn = sqlite3.connect(DB_PATH)
            # Tolerate older schemas that don't yet have the confidence columns.
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN confidence REAL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN confidence_source TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute("""
                INSERT INTO trades (symbol, strategy, direction, entry_time, entry_price,
                                    exit_time, exit_price, stop_price, target_price,
                                    contracts, pnl_dollars, pnl_pct, fees, exit_reason, status,
                                    confidence, confidence_source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (tr.symbol, tr.strategy, tr.direction, tr.entry_time, tr.entry_price,
                  tr.exit_time, tr.exit_price, tr.stop_price, tr.target_price,
                  tr.contracts, tr.pnl_dollars, tr.pnl_pct, tr.fees, tr.exit_reason, tr.status,
                  tr.confidence, tr.confidence_source))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[strategy_engine] sqlite write failed: {e}")

        # JSON append
        try:
            log = []
            if os.path.exists(TRADE_LOG_PATH):
                with open(TRADE_LOG_PATH) as f:
                    raw = f.read().strip()
                    if raw:
                        log = json.loads(raw)
            log.append(asdict(tr))
            with open(TRADE_LOG_PATH, "w") as f:
                json.dump(log, f, indent=2)
        except Exception as e:
            print(f"[strategy_engine] json write failed: {e}")

    # ── Reporting ──────────────────────────────────────────────────────
    def get_session_report(self) -> dict:
        wins = [t for t in self.closed if t.pnl_dollars > 0]
        losses = [t for t in self.closed if t.pnl_dollars <= 0]
        total_pnl = sum(t.pnl_dollars for t in self.closed)
        return {
            "balance": self.balance,
            "starting_balance": self.starting_balance,
            "total_pnl": total_pnl,
            "total_pnl_pct": (total_pnl / self.starting_balance * 100) if self.starting_balance else 0.0,
            "trades": len(self.closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(self.closed) * 100) if self.closed else 0.0,
            "open_positions": len(self.positions),
            "risk_mode": self.risk_mode,
            "enabled_strategies": sorted(self.enabled),
        }

    def open_positions_summary(self) -> list[dict]:
        return [
            {
                "symbol": p.symbol, "strategy": p.strategy, "direction": p.direction,
                "entry_price": p.entry_price, "stop": p.stop_price, "target": p.target_price,
                "contracts": p.contracts, "entry_time": p.entry_time,
            }
            for p in self.positions.values()
        ]


# ─── Simple backtest / test harness ─────────────────────────────────────
def backtest_symbol(symbol: str, timeframe: str = "1d", bars: int = 500,
                    strategies: list[str] | None = None,
                    starting_balance: float = 10_000.0,
                    risk_mode: str = "moderate") -> dict:
    """Walk-forward backtest on one symbol. Returns session report + trade list."""
    df = load_bars(symbol, timeframe, bars)
    if df is None:
        return {"error": f"no data for {symbol} {timeframe}"}

    eng = StrategyEngine(
        starting_balance=starting_balance,
        risk_mode=risk_mode,
        enabled_strategies=strategies,
    )
    # Walk bar by bar so entries/exits use only past data
    for i in range(60, len(df)):
        sub = df.iloc[: i + 1].copy()
        eng.step(symbol, sub, timeframe=timeframe)
    eng.close_all("end_of_test")
    rep = eng.get_session_report()
    rep["trades_detail"] = [asdict(t) for t in eng.closed]
    return rep


def main():
    ap = argparse.ArgumentParser(description="Futures paper trading engine")
    ap.add_argument("--test", action="store_true", help="Run a walk-forward backtest")
    ap.add_argument("--symbol", default="ES=F")
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--bars", type=int, default=500)
    args = ap.parse_args()

    if args.test:
        print(f"Backtesting {args.symbol} {args.timeframe} ({args.bars} bars)")
        rep = backtest_symbol(args.symbol, args.timeframe, args.bars)
        for k, v in rep.items():
            if isinstance(v, float):
                print(f"  {k:<22} {v:.2f}")
            else:
                print(f"  {k:<22} {v}")
    else:
        eng = StrategyEngine()
        print("Engine initialized. Use --test to run a backtest.")
        print(f"Risk mode: {eng.risk_mode}")
        print(f"Enabled strategies: {sorted(eng.enabled)}")


if __name__ == "__main__":
    main()

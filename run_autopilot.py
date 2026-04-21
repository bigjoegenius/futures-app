#!/usr/bin/env python3
"""
run_autopilot.py — Headless daemon tying everything together.

Responsibilities:
  1. Keep fresh data flowing:
       - trigger live_prices snapshot every 30s
       - call fetch_data for 1h bars every 15 minutes
  2. Run the paper StrategyEngine: step() every 5 minutes across all symbols
  3. Run AutoPilot (AI strategy selector) every hour
  4. Optionally send email alerts on trade events / daily summary
  5. Tail an `autopilot_log.txt` file for web_controller.py to display

Modes:
  python run_autopilot.py                  # full loop
  python run_autopilot.py --dry-run        # 60-sec self-check, then exit
  python run_autopilot.py --risk conservative --balance 25000
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

from futures_config import DB_PATH, FUTURES
from market_analyzer import load_bars, get_market_overview, generate_hourly_report_with_ai
from strategy_engine import StrategyEngine, TradeReport, CONTRACT_SPECS
from autopilot import AutoPilot
import news_provider
import live_prices


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "autopilot_log.txt")

STEP_INTERVAL_SEC       = 5 * 60      # run strategy_engine.step() every 5 min
FETCH_INTERVAL_SEC      = 15 * 60     # refresh daily+1h candles every 15 min
LIVE_POLL_INTERVAL_SEC  = 30          # poll live prices every 30s
AUTOPILOT_INTERVAL_SEC  = 60 * 60     # AI strategy selection every hour
REPORT_HOUR_ET          = 16          # send daily email at 4pm ET


# ─── Logging ───────────────────────────────────────────────────────────
def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── Email ─────────────────────────────────────────────────────────────
def send_email(subject: str, body: str) -> None:
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("REPORT_EMAIL_TO")
    if not (user and pw and to):
        log("(email not configured — skipping)")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw)
            s.send_message(msg)
        log(f"email sent: {subject}")
    except Exception as e:
        log(f"email failed: {e}")


# ─── Workers ───────────────────────────────────────────────────────────
class LivePriceWorker(threading.Thread):
    def __init__(self, stop_event: threading.Event):
        super().__init__(daemon=True, name="live-prices")
        self.stop_event = stop_event

    def run(self):
        symbols = list(FUTURES.keys())
        conn = sqlite3.connect(DB_PATH)
        while not self.stop_event.is_set():
            try:
                live_prices.poll_once(symbols, conn, verbose=False)
            except Exception as e:
                log(f"live_prices error: {e}")
            for _ in range(LIVE_POLL_INTERVAL_SEC):
                if self.stop_event.is_set():
                    break
                time.sleep(1)
        conn.close()


class DataRefreshWorker(threading.Thread):
    def __init__(self, stop_event: threading.Event):
        super().__init__(daemon=True, name="data-refresh")
        self.stop_event = stop_event

    def run(self):
        # Sleep a beat so we don't hammer yfinance at startup
        time.sleep(5)
        while not self.stop_event.is_set():
            try:
                log("refreshing 1h + 1d candles...")
                subprocess.run(
                    [sys.executable, os.path.join(BASE_DIR, "fetch_data.py"),
                     "--timeframe", "1h"],
                    check=False, cwd=BASE_DIR, capture_output=True, timeout=300,
                )
                subprocess.run(
                    [sys.executable, os.path.join(BASE_DIR, "fetch_data.py")],
                    check=False, cwd=BASE_DIR, capture_output=True, timeout=300,
                )
            except Exception as e:
                log(f"fetch_data error: {e}")
            for _ in range(FETCH_INTERVAL_SEC):
                if self.stop_event.is_set():
                    break
                time.sleep(1)


class StrategyWorker(threading.Thread):
    def __init__(self, engine: StrategyEngine, stop_event: threading.Event):
        super().__init__(daemon=True, name="strategy")
        self.engine = engine
        self.stop_event = stop_event

    def run(self):
        time.sleep(10)
        while not self.stop_event.is_set():
            for sym in FUTURES.keys():
                if sym not in CONTRACT_SPECS:
                    continue
                df = load_bars(sym, "1h", 300)
                if df is None:
                    continue
                try:
                    self.engine.step(sym, df)
                except Exception as e:
                    log(f"strategy step {sym} error: {e}")
            for _ in range(STEP_INTERVAL_SEC):
                if self.stop_event.is_set():
                    break
                time.sleep(1)


class DailyReportWorker(threading.Thread):
    def __init__(self, engine: StrategyEngine, stop_event: threading.Event):
        super().__init__(daemon=True, name="daily-report")
        self.engine = engine
        self.stop_event = stop_event
        self._sent_date = None

    def run(self):
        while not self.stop_event.is_set():
            try:
                now = datetime.now()
                # Crude ET check — no pytz dependency
                if now.hour == REPORT_HOUR_ET and self._sent_date != now.date():
                    self._send_daily()
                    self._sent_date = now.date()
            except Exception as e:
                log(f"daily report error: {e}")
            for _ in range(60):
                if self.stop_event.is_set():
                    break
                time.sleep(1)

    def _send_daily(self):
        rep = self.engine.get_session_report()
        overview = get_market_overview()
        ai_summary = generate_hourly_report_with_ai(overview, self.engine.closed[-15:])
        body = (
            f"Futures Autopilot Daily Summary\n"
            f"{'=' * 45}\n"
            f"Balance:        ${rep['balance']:,.2f}\n"
            f"Session P&L:    {rep['total_pnl_pct']:+.2f}%  (${rep['total_pnl']:+,.2f})\n"
            f"Trades:         {rep['trades']}  (win rate {rep['win_rate']:.1f}%)\n"
            f"Open positions: {rep['open_positions']}\n"
            f"Risk mode:      {rep['risk_mode']}\n"
            f"Strategies:     {', '.join(rep['enabled_strategies'])}\n\n"
            f"AI Report\n{'-' * 45}\n{ai_summary}\n"
        )
        send_email("Futures Autopilot — daily summary", body)


# ─── Trade alerts ──────────────────────────────────────────────────────
def on_trade_closed(tr: TradeReport) -> None:
    ok = tr.pnl_dollars >= 0
    line = (f"trade closed  {tr.symbol}  {tr.strategy}  {tr.direction}  "
            f"pnl={tr.pnl_dollars:+.2f} ({tr.pnl_pct:+.2f}%)  reason={tr.exit_reason}")
    log(line)
    # Only email outsized moves to avoid noise
    if abs(tr.pnl_pct) >= 1.0:
        send_email(f"Futures trade closed: {tr.symbol} {'+' if ok else ''}{tr.pnl_pct:.1f}%", line)


# ─── Main ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Futures autopilot daemon")
    ap.add_argument("--balance", type=float, default=10_000.0)
    ap.add_argument("--risk", default="moderate", choices=["conservative", "moderate", "aggressive"])
    ap.add_argument("--ai-interval", type=int, default=AUTOPILOT_INTERVAL_SEC,
                    help="Seconds between AI strategy selections")
    ap.add_argument("--dry-run", action="store_true",
                    help="Run for 60 seconds and exit (self-check)")
    args = ap.parse_args()

    # Sanity: make sure tables exist
    from db_setup import create_database
    create_database()

    engine = StrategyEngine(
        starting_balance=args.balance,
        risk_mode=args.risk,
        on_trade_closed=on_trade_closed,
    )
    pilot = AutoPilot(engine, interval_seconds=args.ai_interval,
                      on_status_update=lambda d: log(
                          f"autopilot decision: risk={d.get('risk_mode')} "
                          f"enabled={d.get('enabled')} ({d.get('source')})"))

    stop_event = threading.Event()

    log("=" * 55)
    log(f"Futures Autopilot starting  (balance=${args.balance:,.0f}, risk={args.risk})")
    log("=" * 55)

    workers = [
        LivePriceWorker(stop_event),
        DataRefreshWorker(stop_event),
        StrategyWorker(engine, stop_event),
        DailyReportWorker(engine, stop_event),
    ]
    for w in workers:
        w.start()

    # Seed the autopilot with an initial decision
    try:
        pilot.run_once()
    except Exception as e:
        log(f"initial autopilot error: {e}")
    pilot.start()

    try:
        if args.dry_run:
            log("dry-run: will exit in 60s")
            time.sleep(60)
        else:
            while True:
                time.sleep(30)
                # periodic pretrade context dump
                try:
                    pre = news_provider.get_pretrade_check()
                    if pre.get("summary"):
                        log(f"news: {pre['summary'][:200]}")
                except Exception:
                    pass
    except KeyboardInterrupt:
        log("shutdown requested")
    finally:
        log("stopping workers...")
        stop_event.set()
        pilot.stop()
        # Don't force-close open positions — they stay open for next run
        rep = engine.get_session_report()
        log(f"final: balance=${rep['balance']:,.2f}  trades={rep['trades']}  win={rep['win_rate']:.1f}%")


if __name__ == "__main__":
    main()

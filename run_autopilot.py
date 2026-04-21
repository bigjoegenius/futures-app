#!/usr/bin/env python3
"""
run_autopilot.py — Headless daemon tying everything together for the futures book.

Responsibilities:
  1. Keep fresh data flowing (live_prices every 30s, fetch_data every 15 min)
  2. Run the paper StrategyEngine: step() every 5 minutes across all 18 symbols
  3. Run AutoPilot (Claude AI strategy selector) every hour
  4. Load MiniMax insights (refreshed by its own service) + compare to Claude
  5. Daily 4pm ET summary email + high-conviction (rolling top 5% confidence)
     per-trade alerts to baldwetcoby@gmail.com. No per-trade email on every
     open/close — only the top-percentile ones.
  6. Tail an autopilot_log.txt file for web_controller.py to display

Modes:
  python run_autopilot.py                           # AutoPilot portfolio (AI-gated)
  python run_autopilot.py --portfolio static_all    # Static portfolio (all strategies always on)
  python run_autopilot.py --dry-run                 # 60-sec self-check, then exit
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
from market_analyzer import load_bars, get_market_overview, generate_hourly_report_with_ai, STRATEGIES
from strategy_engine import StrategyEngine, TradeReport, CONTRACT_SPECS, DEFAULT_STRATEGIES
from autopilot import AutoPilot
from confidence_tracker import ConfidenceTracker
import news_provider
import live_prices


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "autopilot_log.txt")
MINIMAX_JSON = os.path.join(BASE_DIR, "minimax_insights.json")
TRADE_LOG_JSON = os.path.join(BASE_DIR, "trade_log.json")
CONFIDENCE_WINDOW_JSON = os.path.join(BASE_DIR, "confidence_window.json")

STEP_INTERVAL_SEC       = 5 * 60
FETCH_INTERVAL_SEC      = 15 * 60
LIVE_POLL_INTERVAL_SEC  = 30
AUTOPILOT_INTERVAL_SEC  = 60 * 60
REPORT_HOUR_ET          = 16     # daily recap at 4pm ET

# Rolling window of recent trade confidence scores. New trades whose
# confidence lands in the top (100 - HIGH_CONVICTION_PERCENTILE)% fire
# a special alert. Cold start (< HIGH_CONVICTION_MIN_SAMPLE values)
# falls back to HIGH_CONVICTION_COLD_CUTOFF so alerts can still fire
# right after deploy.
HIGH_CONVICTION_PERCENTILE  = 95.0
HIGH_CONVICTION_MIN_SAMPLE  = 50
HIGH_CONVICTION_COLD_CUTOFF = 85.0

_confidence_tracker = ConfidenceTracker(CONFIDENCE_WINDOW_JSON, max_size=200)


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
def _email_recipient() -> str | None:
    # Futures-specific: baldwetcoby@gmail.com (user directive)
    return (os.environ.get("BALDWETCOBY_EMAIL_TO")
            or os.environ.get("FUTURES_EMAIL_TO")
            or "baldwetcoby@gmail.com")


def send_email(subject: str, body: str) -> None:
    user = os.environ.get("GMAIL_USER")
    pw   = os.environ.get("GMAIL_APP_PASSWORD")
    to   = _email_recipient()
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
        log(f"email sent to {to}: {subject}")
    except Exception as e:
        log(f"email failed: {e}")


# ─── Trade log JSON persistence ────────────────────────────────────────
def _save_trade_log(engine: StrategyEngine, portfolio: str) -> None:
    try:
        existing = []
        if os.path.exists(TRADE_LOG_JSON):
            try:
                with open(TRADE_LOG_JSON) as f:
                    existing = json.loads(f.read() or "[]")
            except Exception:
                existing = []
        # Remove previous entries from this portfolio
        kept = [t for t in existing if t.get("portfolio") != portfolio]
        # Append open + closed from this engine
        for p in engine.positions.values():
            kept.append({
                "portfolio": portfolio,
                "symbol": p.symbol, "coin": p.symbol,
                "strategy": p.strategy, "direction": p.direction,
                "entry_time": p.entry_time, "entry_price": p.entry_price,
                "stop_price": p.stop_price, "target_price": p.target_price,
                "num_contracts": p.contracts, "contracts": p.contracts,
                "confidence": p.confidence,
                "confidence_source": p.confidence_source,
                "status": "open",
            })
        for t in engine.closed[-200:]:
            kept.append({
                "portfolio": portfolio,
                "symbol": t.symbol, "coin": t.symbol,
                "strategy": t.strategy, "direction": t.direction,
                "entry_time": t.entry_time, "entry_price": t.entry_price,
                "exit_time": t.exit_time, "exit_price": t.exit_price,
                "stop_price": t.stop_price, "target_price": t.target_price,
                "num_contracts": t.contracts, "contracts": t.contracts,
                "pnl_dollars": t.pnl_dollars, "pnl_pct": t.pnl_pct,
                "fees": t.fees, "exit_reason": t.exit_reason,
                "confidence": t.confidence,
                "confidence_source": t.confidence_source,
                "status": "closed",
            })
        with open(TRADE_LOG_JSON, "w") as f:
            json.dump(kept, f, indent=2)
    except Exception as e:
        log(f"trade_log.json write failed: {e}")


# ─── MiniMax comparison ────────────────────────────────────────────────
_mm_last_mtime = 0
def _maybe_log_minimax():
    global _mm_last_mtime
    try:
        if not os.path.exists(MINIMAX_JSON):
            return
        mtime = os.path.getmtime(MINIMAX_JSON)
        if mtime <= _mm_last_mtime:
            return
        _mm_last_mtime = mtime
        with open(MINIMAX_JSON) as f:
            mm = json.load(f)
        sig = mm.get("structured_signals", {}) or {}
        log(f"MINIMAX: bias={sig.get('overall_bias','?')} "
            f"conf={sig.get('confidence',0)}% "
            f"action={sig.get('immediate_action','?')} "
            f"danger={sig.get('danger_level','?')} "
            f"tokens={mm.get('total_tokens_used', 0)}")
    except Exception as e:
        log(f"minimax read error: {e}")


# ─── Workers ───────────────────────────────────────────────────────────
class LivePriceWorker(threading.Thread):
    def __init__(self, stop_event):
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
    def __init__(self, stop_event):
        super().__init__(daemon=True, name="data-refresh")
        self.stop_event = stop_event
    def run(self):
        time.sleep(5)
        while not self.stop_event.is_set():
            try:
                log("refreshing 1h + 1d candles...")
                subprocess.run([sys.executable, os.path.join(BASE_DIR, "fetch_data.py"), "--timeframe", "1h"],
                               check=False, cwd=BASE_DIR, capture_output=True, timeout=300)
                subprocess.run([sys.executable, os.path.join(BASE_DIR, "fetch_data.py")],
                               check=False, cwd=BASE_DIR, capture_output=True, timeout=300)
            except Exception as e:
                log(f"fetch_data error: {e}")
            for _ in range(FETCH_INTERVAL_SEC):
                if self.stop_event.is_set():
                    break
                time.sleep(1)


class StrategyWorker(threading.Thread):
    def __init__(self, engine, portfolio, stop_event):
        super().__init__(daemon=True, name=f"strategy-{portfolio}")
        self.engine = engine
        self.portfolio = portfolio
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
                    self.engine.step(sym, df, timeframe="1h")
                except Exception as e:
                    log(f"[{self.portfolio}] strategy step {sym} error: {e}")
            _save_trade_log(self.engine, self.portfolio)
            _maybe_log_minimax()
            for _ in range(STEP_INTERVAL_SEC):
                if self.stop_event.is_set():
                    break
                time.sleep(1)


class DailyReportWorker(threading.Thread):
    def __init__(self, engine, portfolio, stop_event):
        super().__init__(daemon=True, name="daily-report")
        self.engine = engine
        self.portfolio = portfolio
        self.stop_event = stop_event
        self._sent_date = None
    def run(self):
        while not self.stop_event.is_set():
            try:
                now = datetime.now()
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
        tag = "[FUTURES DAILY]"
        subject = (f"{tag} {self.portfolio} — bal ${rep['balance']:,.0f} "
                   f"P&L {rep['total_pnl_pct']:+.2f}% ({rep['trades']} trades)")

        # Recent trades with confidence column
        recent_lines = []
        for t in self.engine.closed[-10:]:
            conf = f"{t.confidence:.0f}%" if t.confidence is not None else "—"
            recent_lines.append(
                f"  {t.symbol:<6} {t.direction.upper():<5} {t.strategy:<22} "
                f"pnl=${t.pnl_dollars:>+8.2f}  conf={conf}"
            )
        recent_block = "\n".join(recent_lines) if recent_lines else "  (no trades closed yet)"

        # Percentile snapshot
        p95 = _confidence_tracker.percentile(HIGH_CONVICTION_PERCENTILE)
        n = _confidence_tracker.sample_size()
        p95_str = f"{p95:.1f}" if p95 is not None else "—"
        body = (
            f"Futures {self.portfolio.upper()} Daily Summary — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"{'=' * 55}\n"
            f"Balance:        ${rep['balance']:,.2f}\n"
            f"Session P&L:    {rep['total_pnl_pct']:+.2f}%  (${rep['total_pnl']:+,.2f})\n"
            f"Trades:         {rep['trades']}  (win rate {rep['win_rate']:.1f}%)\n"
            f"Open positions: {rep['open_positions']}\n"
            f"Risk mode:      {rep['risk_mode']}\n"
            f"Strategies:     {', '.join(rep['enabled_strategies'][:10])}\n"
            f"Confidence window: {n} trades, {HIGH_CONVICTION_PERCENTILE:.0f}th pct = {p95_str}\n\n"
            f"Recent Trades\n{'-' * 55}\n{recent_block}\n\n"
            f"AI Market Report\n{'-' * 55}\n{ai_summary}\n"
        )
        send_email(subject, body)


# ─── High-conviction alert ─────────────────────────────────────────────
def _send_high_conviction_alert(portfolio: str, tr: TradeReport, reason: str) -> None:
    """Fire the special email for a top-percentile-confidence trade.

    Per user directive: baldwetcoby@gmail.com, tagged so it's obvious
    this is a rare event and filter-friendly in Gmail.
    """
    tag = "[FUTURES HIGH CONVICTION]"
    conf = tr.confidence if tr.confidence is not None else 0.0
    subject = (f"{tag} {tr.symbol} {tr.direction.upper()} @ {tr.entry_price:.4f} "
               f"— {conf:.0f}% confidence")
    body = (
        f"Futures {portfolio.upper()} — HIGH CONVICTION OPEN\n"
        f"{'=' * 55}\n"
        f"Symbol:      {tr.symbol}\n"
        f"Strategy:    {tr.strategy}\n"
        f"Direction:   {tr.direction.upper()}\n"
        f"Entry:       {tr.entry_price:.4f} at {tr.entry_time}\n"
        f"Contracts:   {tr.contracts}\n"
        f"Stop loss:   {tr.stop_price:.4f}\n"
        f"Take profit: {tr.target_price:.4f}\n"
        f"Confidence:  {conf:.1f}%  ({tr.confidence_source or 'n/a'})\n"
        f"Trigger:     {reason}\n"
    )
    send_email(subject, body)


# ─── Trade callbacks ───────────────────────────────────────────────────
def _make_callbacks(portfolio: str):
    def on_trade_opened(tr: TradeReport) -> None:
        conf_str = f"conf={tr.confidence:.1f}" if tr.confidence is not None else "conf=?"
        line = (f"OPENED  {tr.symbol}  {tr.strategy}  {tr.direction.upper()}  "
                f"entry={tr.entry_price:.4f}  stop={tr.stop_price:.4f}  target={tr.target_price:.4f}  "
                f"contracts={tr.contracts}  {conf_str}")
        log(f"[{portfolio}] {line}")
        # Only email when confidence is extreme (rolling top 5%).
        try:
            is_hc, reason = _confidence_tracker.is_top(
                tr.confidence,
                percentile=HIGH_CONVICTION_PERCENTILE,
                min_sample=HIGH_CONVICTION_MIN_SAMPLE,
                cold_cutoff=HIGH_CONVICTION_COLD_CUTOFF,
            )
            if is_hc:
                log(f"[{portfolio}] HIGH CONVICTION — {reason}")
                _send_high_conviction_alert(portfolio, tr, reason)
        except Exception as e:
            log(f"high-conviction check failed: {e}")

    def on_trade_closed(tr: TradeReport) -> None:
        ok = tr.pnl_dollars >= 0
        conf_str = f"conf={tr.confidence:.1f}" if tr.confidence is not None else "conf=?"
        line = (f"CLOSED  {tr.symbol}  {tr.strategy}  {tr.direction.upper()}  "
                f"pnl=${tr.pnl_dollars:+.2f} ({tr.pnl_pct:+.2f}%)  reason={tr.exit_reason}  {conf_str}")
        log(f"[{portfolio}] {line}")
        # Record the trade's confidence so the percentile window reflects
        # actual realized trades. No email — daily recap covers this.
        try:
            _confidence_tracker.record(tr.confidence)
        except Exception as e:
            log(f"confidence_tracker record failed: {e}")

    return on_trade_opened, on_trade_closed


# ─── Patch engine to also fire on_trade_opened ─────────────────────────
def _wire_engine_open_callback(engine: StrategyEngine, on_open):
    """StrategyEngine doesn't expose an on_open hook; wrap _open_position."""
    orig_open = engine._open_position
    def wrapped(symbol, strategy, direction, price, atr_val, ts, spec, *, raw_score: float = 0.0):
        orig_open(symbol, strategy, direction, price, atr_val, ts, spec, raw_score=raw_score)
        pos = engine.positions.get(symbol)
        if pos is not None:
            tr = TradeReport(
                symbol=pos.symbol, strategy=pos.strategy, direction=pos.direction,
                entry_time=pos.entry_time, entry_price=pos.entry_price,
                stop_price=pos.stop_price, target_price=pos.target_price,
                contracts=pos.contracts, status="open",
                confidence=pos.confidence,
                confidence_source=pos.confidence_source,
            )
            try:
                on_open(tr)
            except Exception as e:
                log(f"on_open callback error: {e}")
    engine._open_position = wrapped


# ─── Main ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Futures autopilot daemon")
    ap.add_argument("--balance", type=float, default=10_000.0)
    ap.add_argument("--risk", default="moderate", choices=["conservative", "moderate", "aggressive"])
    ap.add_argument("--portfolio", default="autopilot",
                    choices=["autopilot", "static_all"],
                    help="autopilot = Claude-gated strategies; static_all = all strategies always on")
    ap.add_argument("--ai-interval", type=int, default=AUTOPILOT_INTERVAL_SEC)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from db_setup import create_database
    create_database()

    # Seed the rolling confidence window from any prior closed trades on
    # this box so the top-5% gate doesn't start completely empty after
    # a restart. Only runs when the window itself is empty on disk.
    try:
        if _confidence_tracker.sample_size() == 0:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA busy_timeout=2000")
            try:
                rows = conn.execute(
                    "SELECT confidence FROM trades "
                    "WHERE confidence IS NOT NULL AND status='closed' "
                    "ORDER BY id DESC LIMIT 200"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            conn.close()
            seeded = 0
            for (c,) in rows:
                if c is not None:
                    _confidence_tracker.record(c)
                    seeded += 1
            if seeded:
                log(f"confidence window seeded with {seeded} historical trades")
    except Exception as e:
        log(f"confidence seed failed: {e}")

    on_open, on_close = _make_callbacks(args.portfolio)

    # Static: all 28 strategies enabled; AutoPilot: let AI decide (start with all)
    initial_enabled = list(DEFAULT_STRATEGIES) if args.portfolio == "static_all" else list(DEFAULT_STRATEGIES)

    engine = StrategyEngine(
        starting_balance=args.balance,
        risk_mode=args.risk,
        enabled_strategies=initial_enabled,
        on_trade_closed=on_close,
    )
    _wire_engine_open_callback(engine, on_open)

    stop_event = threading.Event()

    log("=" * 55)
    log(f"Futures {args.portfolio.upper()} starting  "
        f"(balance=${args.balance:,.0f}, risk={args.risk}, "
        f"strategies={len(initial_enabled)}, email→{_email_recipient()})")
    log("=" * 55)

    workers = [
        LivePriceWorker(stop_event),
        DataRefreshWorker(stop_event),
        StrategyWorker(engine, args.portfolio, stop_event),
        DailyReportWorker(engine, args.portfolio, stop_event),
    ]
    for w in workers:
        w.start()

    pilot = None
    if args.portfolio == "autopilot":
        pilot = AutoPilot(
            engine,
            interval_seconds=args.ai_interval,
            on_status_update=lambda d: log(
                f"autopilot decision: risk={d.get('risk_mode')} "
                f"n_enabled={len(d.get('enabled', []))} "
                f"conf={d.get('overall_confidence','?')}% "
                f"source={d.get('source')}"
            ),
        )
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
                # Periodic news sniff
                try:
                    pre = news_provider.get_pretrade_check() if hasattr(news_provider, "get_pretrade_check") else None
                    if pre and pre.get("summary"):
                        log(f"news: {pre['summary'][:200]}")
                except Exception:
                    pass
    except KeyboardInterrupt:
        log("shutdown requested")
    finally:
        log("stopping workers...")
        stop_event.set()
        if pilot:
            pilot.stop()
        _save_trade_log(engine, args.portfolio)
        rep = engine.get_session_report()
        log(f"final [{args.portfolio}]: balance=${rep['balance']:,.2f}  "
            f"trades={rep['trades']}  win={rep['win_rate']:.1f}%")


if __name__ == "__main__":
    main()

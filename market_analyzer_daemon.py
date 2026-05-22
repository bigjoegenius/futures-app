#!/usr/bin/env python3
"""
market_analyzer_daemon.py — periodic market analysis email for futures.

Mirrors the crypto-app daemon: builds a snapshot, asks Claude to write a
short analysis, emails it. Uses the local `claude` CLI exclusively
(Claude Code subscription auth via CLAUDE_CODE_OAUTH_TOKEN). No metered
API key.

Usage:
    python3 market_analyzer_daemon.py             # one-shot dry run
    python3 market_analyzer_daemon.py --send      # one-shot + email
    python3 market_analyzer_daemon.py --loop      # run forever on schedule
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "futures.db")
LOG_PATH = os.path.join(BASE_DIR, "market_analyzer_daemon.log")

ANALYZER_RECIPIENT = os.environ.get("BALDWETCOBY_EMAIL_TO", "baldwetcoby@gmail.com")
SUBJECT_TAG = "[FUTURES ANALYZER]"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
CLAUDE_MODEL = "sonnet"

LOOP_INTERVAL_SEC = 4 * 60 * 60  # 4 hours


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA busy_timeout=3000")
    conn.row_factory = sqlite3.Row
    return conn


def collect_snapshot() -> dict:
    snap = {"generated_at": datetime.now(timezone.utc).isoformat(),
            "prices": {}, "recent_trades": [], "open_positions": []}

    try:
        conn = _db()
        price_rows = conn.execute(
            "SELECT symbol, last, prev_close, updated_at FROM latest_prices"
        ).fetchall()
        for r in price_rows:
            last = r["last"]
            prev = r["prev_close"]
            chg = ((last - prev) / prev * 100) if (last and prev) else 0.0
            snap["prices"][r["symbol"]] = {"last": last, "change_pct": chg}

        cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        recent = conn.execute(
            "SELECT symbol, strategy, direction, entry_price, exit_price, "
            "pnl_dollars, fees, exit_reason, entry_time, exit_time, status, confidence "
            "FROM trades WHERE exit_time >= ? OR status='open' "
            "ORDER BY COALESCE(exit_time, entry_time) DESC LIMIT 40",
            (cutoff,)
        ).fetchall()
        for r in recent:
            t = dict(r)
            if t.get("status") == "open":
                snap["open_positions"].append(t)
            else:
                snap["recent_trades"].append(t)

        # Rolling P&L per market (last 7 days)
        rolling = {}
        week_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        roll_rows = conn.execute(
            "SELECT symbol, SUM(pnl_dollars) AS pnl, COUNT(*) AS n "
            "FROM trades WHERE status='closed' AND exit_time >= ? "
            "GROUP BY symbol ORDER BY pnl DESC",
            (week_cutoff,)
        ).fetchall()
        for r in roll_rows:
            rolling[r["symbol"]] = {"pnl": r["pnl"] or 0, "trades": r["n"] or 0}
        snap["rolling_7d"] = rolling

        conn.close()
    except Exception as e:
        log(f"snapshot DB error: {e}")

    return snap


def format_prompt(snap: dict) -> str:
    lines = []
    lines.append("You are an analyst reporting to a paper-trading futures bot's operator.")
    lines.append("Write a concise market analysis and tactical update across 18 markets.")
    lines.append("")
    lines.append("Structure your reply as plain prose under 700 words, 5 short sections:")
    lines.append("1. Overall risk regime — risk-on/risk-off, what 2-3 tells matter most")
    lines.append("2. Strength / weakness board — which markets are leading or lagging")
    lines.append("3. Open positions — each, whether the thesis still holds")
    lines.append("4. Next 6-24h outlook — key levels, which strategies look best")
    lines.append("5. Flags — correlations breaking, news risk, over-concentration")
    lines.append("No markdown, no preamble, no disclaimers. Just the sections.")
    lines.append("")

    lines.append(f"Generated at {snap.get('generated_at')}")
    lines.append("")

    lines.append("— Live prices —")
    for sym, p in (snap.get("prices") or {}).items():
        last = p.get("last")
        chg = p.get("change_pct", 0)
        if last is not None:
            lines.append(f"  {sym}: ${last}  {chg:+.2f}%")

    lines.append("")
    lines.append("— 7-day per-market P&L (paper) —")
    for sym, r in (snap.get("rolling_7d") or {}).items():
        lines.append(f"  {sym}: ${(r.get('pnl') or 0):+.2f}  ({r.get('trades',0)} trades)")

    opens = snap.get("open_positions") or []
    if opens:
        lines.append("")
        lines.append(f"— Open positions ({len(opens)}) —")
        for o in opens[:20]:
            lines.append(f"  {o.get('symbol')} {o.get('strategy')} {o.get('direction')} "
                         f"entry {o.get('entry_price')} conf {o.get('confidence')}")

    closed = snap.get("recent_trades") or []
    if closed:
        lines.append("")
        lines.append(f"— Recent closed ({len(closed)}, last 2 days) —")
        for t in closed[:20]:
            lines.append(f"  {t.get('symbol')} {t.get('strategy')} {t.get('direction')} "
                         f"pnl ${t.get('pnl_dollars'):+.2f}  reason {t.get('exit_reason')}")

    return "\n".join(lines)


# ─── Claude CLI call ──────────────────────────────────────────────────────

def call_claude_cli(prompt: str) -> str | None:
    # Strip ANTHROPIC_API_KEY so the CLI uses the OAuth subscription token
    # and never falls back to metered API spend.
    cli_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", "--model", CLAUDE_MODEL, "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=300, env=cli_env,
        )
        if r.returncode != 0:
            log(f"claude CLI exit {r.returncode}: {(r.stderr or '')[:200]}")
            return None
        return (r.stdout or "").strip() or None
    except subprocess.TimeoutExpired:
        log("claude CLI timed out (>300s)")
        return None
    except FileNotFoundError:
        log(f"claude CLI not found at {CLAUDE_BIN}")
        return None
    except Exception as e:
        log(f"claude CLI error: {e}")
        return None


def generate_analysis(snap: dict) -> tuple[str, str]:
    prompt = format_prompt(snap)
    text = call_claude_cli(prompt)
    if text:
        return text, "claude-cli"
    return "(No analysis — claude CLI unavailable. Check `claude` install on this host.)", "none"


def send_email(subject: str, body: str) -> bool:
    user = os.environ.get("GMAIL_USER", "")
    pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not (user and pw and ANALYZER_RECIPIENT):
        log("email not configured")
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = ANALYZER_RECIPIENT
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw)
            s.send_message(msg)
        log(f"email sent to {ANALYZER_RECIPIENT}: {subject}")
        return True
    except Exception as e:
        log(f"email failed: {e}")
        return False


def run_once(send: bool = True) -> None:
    snap = collect_snapshot()
    analysis, source = generate_analysis(snap)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rolling = snap.get("rolling_7d") or {}
    total_pnl_7d = sum((r.get("pnl") or 0) for r in rolling.values())
    subject = f"{SUBJECT_TAG} {now} — 7d P&L {total_pnl_7d:+,.0f} ({source})"

    body = (
        f"Futures Market Analyzer — {now}\n"
        f"{'=' * 55}\n"
        f"Source: {source}\n\n"
        f"{analysis}\n\n"
        f"{'-' * 55}\n"
        f"Open positions: {len(snap.get('open_positions') or [])}  "
        f"Recent closed (2d): {len(snap.get('recent_trades') or [])}\n"
    )

    log(f"analysis ready ({len(analysis)} chars, source={source})")
    print("\n" + "=" * 60)
    print(subject)
    print("=" * 60)
    print(body[:800])
    print("=" * 60)

    if send:
        send_email(subject, body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=LOOP_INTERVAL_SEC)
    args = ap.parse_args()

    run_once(send=args.send)
    if not args.loop:
        return

    log(f"looping every {args.interval}s")
    while True:
        time.sleep(args.interval)
        try:
            run_once(send=True)
        except Exception as e:
            log(f"loop run error: {e}")


if __name__ == "__main__":
    main()

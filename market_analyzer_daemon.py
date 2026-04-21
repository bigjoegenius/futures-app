#!/usr/bin/env python3
"""
market_analyzer_daemon.py — periodic market analysis email for futures.

Mirrors the crypto-app daemon: builds a snapshot, asks Claude to write a
short analysis, emails it. Uses the `claude` CLI (Claude Code subscription)
with the Anthropic SDK as a fallback.

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
MINIMAX_PATH = os.path.join(BASE_DIR, "minimax_insights.json")
LOG_PATH = os.path.join(BASE_DIR, "market_analyzer_daemon.log")

ANALYZER_RECIPIENT = os.environ.get("BALDWETCOBY_EMAIL_TO", "baldwetcoby@gmail.com")
SUBJECT_TAG = "[FUTURES ANALYZER]"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
CLAUDE_MODEL_FALLBACK = "claude-opus-4-7"

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
            "prices": {}, "recent_trades": [], "open_positions": [],
            "minimax": None}

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

    try:
        if os.path.exists(MINIMAX_PATH):
            with open(MINIMAX_PATH) as f:
                snap["minimax"] = json.load(f)
    except Exception as e:
        log(f"minimax read error: {e}")

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

    mm = snap.get("minimax") or {}
    sig = (mm.get("structured_signals") or {}) if isinstance(mm, dict) else {}
    if sig:
        lines.append("")
        lines.append("— MiniMax signals —")
        lines.append(f"  bias {sig.get('overall_bias')}  conf {sig.get('confidence')}  "
                     f"danger {sig.get('danger_level')}  action {sig.get('immediate_action')}")
        strategies = sig.get("strategies") or {}
        on = [k for k, v in strategies.items() if isinstance(v, dict) and v.get("enable")]
        if on:
            lines.append(f"  enabled: {', '.join(on)}")

    return "\n".join(lines)


# ─── Claude calls — same pattern as crypto daemon ────────────────────────

def _claude_cli_available() -> bool:
    if not os.path.exists(CLAUDE_BIN):
        return False
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", "Reply with the single word OK"],
            input="", capture_output=True, text=True, timeout=20
        )
        out = (r.stdout or "") + (r.stderr or "")
        if "Not logged in" in out or "Please run /login" in out:
            return False
        if r.returncode != 0:
            return False
        return "OK" in out.upper()
    except Exception:
        return False


def call_claude_cli(prompt: str) -> str | None:
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=180
        )
        if r.returncode != 0:
            log(f"claude CLI exit {r.returncode}: {(r.stderr or '')[:200]}")
            return None
        return (r.stdout or "").strip() or None
    except Exception as e:
        log(f"claude CLI error: {e}")
        return None


def call_claude_sdk(prompt: str) -> str | None:
    try:
        import anthropic
    except ImportError:
        log("anthropic SDK not installed")
        return None
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        log("ANTHROPIC_API_KEY not set")
        return None
    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=CLAUDE_MODEL_FALLBACK,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = []
        for block in resp.content:
            if getattr(block, "type", "") == "text":
                parts.append(block.text)
        return "\n".join(parts).strip() or None
    except Exception as e:
        log(f"anthropic SDK error: {e}")
        return None


def generate_analysis(snap: dict) -> tuple[str, str]:
    prompt = format_prompt(snap)
    if _claude_cli_available():
        log("using claude CLI (Claude Code subscription)")
        text = call_claude_cli(prompt)
        if text:
            return text, "claude-cli"
        log("claude CLI returned no output; falling back to SDK")
    else:
        log("claude CLI unavailable or not logged in; using SDK fallback")
    text = call_claude_sdk(prompt)
    if text:
        return text, "anthropic-sdk"
    return "(No analysis — neither claude CLI nor ANTHROPIC_API_KEY was usable.)", "none"


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

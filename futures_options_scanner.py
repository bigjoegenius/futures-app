#!/usr/bin/env python3
"""
futures_options_scanner.py — Scan EMA cross + liquidity sweep signals on
ES/NQ/GC futures, map to options recommendations, and email alerts.

Runs on adamserver as a systemd timer (every 30 min during market hours).
Reads candle data from futures.db, scores the two target strategies across
three timeframes, and when a signal fires above the threshold it builds an
options recommendation and sends an HTML email styled like the catalyst-options
high-conviction alert.

Also writes a JSON file (futures_options_picks.json) that the morning brief
reads to render the "Futures options picks" section.

Usage:
  python futures_options_scanner.py           # one scan, email if signal
  python futures_options_scanner.py --dry-run # scan + print, no email
  python futures_options_scanner.py --loop    # repeat every 30 min
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
from pathlib import Path

import numpy as np
import pandas as pd

from futures_config import DB_PATH, FUTURES
from market_analyzer import (
    load_bars, score_strategies, atr, ema,
    STRATEGIES,
)

# ─── Config ────────────────────────────────────────────────────────────

SCAN_SYMBOLS = ["ES=F", "NQ=F", "GC=F"]
SCAN_STRATEGIES = ["ema_cross", "liquidity_sweep_reclaim"]
SCAN_TIMEFRAMES = ["15m", "1h", "1d"]

# EMA cross loses money on gold (PF 0.76-1.01, 57% DD). Gold only gets sweep.
SYMBOL_STRATEGY_EXCLUDE = {
    ("GC=F", "ema_cross"),
}

SCORE_THRESHOLD = 60
HIGH_CONVICTION_THRESHOLD = 75

PICKS_FILE = Path(os.path.dirname(__file__)) / "futures_options_picks.json"
COOLDOWN_FILE = Path(os.path.dirname(__file__)) / "futures_options_cooldown.json"
COOLDOWN_HOURS = 4

MAX_TRADE_COST = 500

# For gold, use GLD ETF options on Schwab (much easier than CME futures options).
# GLD price ≈ gold_price / 10. Strike intervals: $1 near ATM, $5 further out.
# 1 GLD contract = 100 shares = ~10 oz gold exposure (same as MGC micro futures).
#
# For ES/NQ, keep CME futures options (Joe's Apex futures account).
OPTIONS_UNDERLYING = {
    "ES=F": {
        "name": "E-mini S&P 500", "options_root": "ES", "exchange": "CME",
        "multiplier": 50, "std_expirations": "Mon/Wed/Fri weeklies + monthly",
        "micro_root": "MES", "micro_multiplier": 5,
        "micro_note": "Micro E-mini S&P (MES) = 1/10th the size",
        "broker": "Apex Trading (futures)",
        "uses_etf_proxy": False,
    },
    "NQ=F": {
        "name": "E-mini Nasdaq 100", "options_root": "NQ", "exchange": "CME",
        "multiplier": 20, "std_expirations": "Mon/Wed/Fri weeklies + monthly",
        "micro_root": "MNQ", "micro_multiplier": 2,
        "micro_note": "Micro E-mini Nasdaq (MNQ) = 1/10th the size",
        "broker": "Apex Trading (futures)",
        "uses_etf_proxy": False,
    },
    "GC=F": {
        "name": "Gold (via GLD ETF)", "options_root": "GLD", "exchange": "NYSE/Schwab",
        "multiplier": 100, "std_expirations": "weekly + monthly",
        "micro_root": "GLD", "micro_multiplier": 100,  # No micro needed — GLD is already small
        "micro_note": "",
        "broker": "Schwab (equity options)",
        "uses_etf_proxy": True,
        "etf_ratio": 10.0,  # GLD price ≈ gold price / 10
    },
}


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [{level}] {msg}", flush=True)


# ─── Cooldown — avoid re-alerting on the same signal ──────────────────

def _load_cooldown() -> dict:
    if COOLDOWN_FILE.exists():
        try:
            return json.loads(COOLDOWN_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_cooldown(cd: dict) -> None:
    COOLDOWN_FILE.write_text(json.dumps(cd, indent=2))


def _cooldown_key(symbol: str, strategy: str, tf: str, direction: str) -> str:
    return f"{symbol}|{strategy}|{tf}|{direction}"


def _is_on_cooldown(cd: dict, key: str) -> bool:
    ts = cd.get(key)
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts)
        return (datetime.now(timezone.utc) - last).total_seconds() < COOLDOWN_HOURS * 3600
    except Exception:
        return False


# ─── Options recommendation logic ─────────────────────────────────────

def _recommend_option(symbol: str, direction: str, price: float,
                      atr_val: float, score: float, strategy: str,
                      tf: str) -> dict:
    """Build an options recommendation from a futures signal.

    Returns a dict with contract_type, strike, expiry_guide, entry/target/stop
    premiums (estimated), and sizing guidance.
    """
    spec = OPTIONS_UNDERLYING[symbol]
    contract_type = "CALL" if direction == "LONG" else "PUT"

    # Expiration guide based on timeframe
    if tf == "15m":
        expiry_guide = "0 DTE (same day)"
        expiry_days = 0
        horizon = "intraday"
    elif tf == "1h":
        expiry_guide = "1-3 DTE (this week)"
        expiry_days = 2
        horizon = "1-3 days"
    else:  # 1d
        expiry_guide = "5-10 DTE (next week)"
        expiry_days = 7
        horizon = "1-2 weeks"

    # For ETF proxies (GLD for gold), convert the futures price + ATR to ETF terms
    uses_etf = spec.get("uses_etf_proxy", False)
    if uses_etf:
        ratio = spec["etf_ratio"]
        etf_price = price / ratio
        etf_atr = atr_val / ratio
        work_price = etf_price
        work_atr = etf_atr
    else:
        work_price = price
        work_atr = atr_val

    # Strike intervals
    if symbol == "ES=F":
        interval = 5
    elif symbol == "NQ=F":
        interval = 25
    elif uses_etf:  # GLD
        interval = 1  # $1 near ATM, $5 further — use $1 for max flexibility
    else:
        interval = 5

    # Push OTM enough to keep premium affordable. Start slightly OTM and widen
    # if the cost estimate exceeds the budget.
    multiplier = spec["multiplier"]
    use_micro = False
    micro_note = ""

    for attempt_offset_atr in [0.5, 0.8, 1.2, 1.5]:
        offset_points = work_atr * attempt_offset_atr
        if direction == "LONG":
            raw_strike = work_price + offset_points
        else:
            raw_strike = work_price - offset_points
        strike = round(raw_strike / interval) * interval

        offset_ratio = abs(strike - work_price) / (work_atr + 1e-9)
        time_factor = 0.25 if expiry_days <= 1 else 0.45 if expiry_days <= 3 else 0.65
        est_premium = max(0.10 if uses_etf else 0.50,
                          work_atr * max(0.05, (1 - offset_ratio * 0.6)) * time_factor)

        cost_full = est_premium * multiplier
        if cost_full <= MAX_TRADE_COST:
            break
    else:
        # Full-size too expensive even far OTM → use micro (futures only; ETF
        # is already small enough that this branch shouldn't trigger for GLD)
        use_micro = True
        micro_mult = spec["micro_multiplier"]
        micro_note = spec["micro_note"]
        multiplier = micro_mult
        offset_points = work_atr * 0.5
        if direction == "LONG":
            raw_strike = work_price + offset_points
        else:
            raw_strike = work_price - offset_points
        strike = round(raw_strike / interval) * interval
        offset_ratio = abs(strike - work_price) / (work_atr + 1e-9)
        time_factor = 0.25 if expiry_days <= 1 else 0.45 if expiry_days <= 3 else 0.65
        est_premium = max(0.10 if uses_etf else 0.50,
                          work_atr * max(0.05, (1 - offset_ratio * 0.6)) * time_factor)

    est_entry_premium = round(est_premium, 2)
    cost_per = est_entry_premium * multiplier
    contracts = max(1, min(3, int(MAX_TRADE_COST / cost_per))) if cost_per > 0 else 1
    total_cost = contracts * cost_per

    # Delta estimate from how far OTM we are (use work_price so ETF math is correct)
    otm_pct = abs(strike - work_price) / work_price * 100
    if otm_pct < 0.3:
        delta_est = "~0.45-0.50"
    elif otm_pct < 0.8:
        delta_est = "~0.35-0.40"
    elif otm_pct < 1.5:
        delta_est = "~0.25-0.30"
    else:
        delta_est = "~0.15-0.20"

    # Target/stop premiums
    target_mult = STRATEGIES[strategy]["target_atr_mult"]
    stop_mult = STRATEGIES[strategy]["stop_atr_mult"]
    est_target_premium = round(est_entry_premium * (1 + target_mult * 0.6), 2)
    est_stop_premium = round(max(est_entry_premium * 0.50, 0.10), 2)

    options_root = spec["micro_root"] if use_micro else spec["options_root"]

    return {
        "symbol": symbol,
        "underlying_name": spec["name"],
        "contract_type": contract_type,
        "strike": strike,
        "expiry_guide": expiry_guide,
        "expiry_days": expiry_days,
        "horizon": horizon,
        "delta_est": delta_est,
        "contracts": contracts,
        "est_entry_premium": est_entry_premium,
        "est_target_premium": est_target_premium,
        "est_stop_premium": est_stop_premium,
        "total_cost_est": round(total_cost, 2),
        "exchange": spec["exchange"],
        "options_root": options_root,
        "is_micro": use_micro,
        "micro_note": micro_note,
        "broker": spec.get("broker", ""),
        "uses_etf_proxy": uses_etf,
        "etf_price": round(work_price, 2) if uses_etf else None,
        "strategy": strategy,
        "strategy_name": STRATEGIES[strategy]["name"],
        "timeframe": tf,
        "direction": direction,
        "score": round(score, 1),
        "underlying_price": round(price, 2),
        "atr": round(atr_val, 2),
        "conviction_tier": "HIGH" if score >= HIGH_CONVICTION_THRESHOLD else "MODERATE",
    }


def _build_trade_explanation(rec: dict) -> str:
    """Plain-English explanation of what the signal means."""
    sym_name = "gold" if rec.get("uses_etf_proxy") else rec["underlying_name"]
    direction = rec["direction"]
    strat = rec["strategy"]
    tf = rec["timeframe"]
    ct = rec["contract_type"]

    etf_note = ""
    if rec.get("uses_etf_proxy"):
        etf_note = (f" GLD tracks gold's price (1 GLD ≈ gold ÷ 10), so the {ct} "
                    f"moves the same direction as a gold-futures {ct}.")

    if strat == "ema_cross":
        if direction == "LONG":
            return (
                f"The 9-period EMA is above the 21-period EMA on the {tf} chart, "
                f"and price is above the 50 EMA. That's a bullish stack — momentum, "
                f"short-term trend, and long-term trend all pointing up on {sym_name}. "
                f"The CALL profits if {sym_name} keeps moving up.{etf_note}"
            )
        else:
            return (
                f"The 9-period EMA is below the 21-period EMA on the {tf} chart, "
                f"and price is below the 50 EMA. That's a bearish stack — momentum, "
                f"short-term trend, and long-term trend all pointing down on {sym_name}. "
                f"The PUT profits if {sym_name} keeps falling.{etf_note}"
            )
    else:  # liquidity_sweep_reclaim
        if direction == "SHORT":
            return (
                f"Price on {sym_name} pushed ABOVE a cluster of equal highs "
                f"(taking out stop orders), then immediately reversed and closed "
                f"back below. That's a 'liquidity sweep' — institutions trapped "
                f"breakout buyers, and the failed breakout usually leads to a sharp "
                f"move down. The PUT profits from that reversal.{etf_note}"
            )
        else:
            return (
                f"Price on {sym_name} pushed BELOW a cluster of equal lows "
                f"(taking out stop orders), then immediately reversed and closed "
                f"back above. That's a 'liquidity sweep' — institutions trapped "
                f"breakout sellers, and the failed breakdown usually leads to a sharp "
                f"move up. The CALL profits from that reversal.{etf_note}"
            )


def _build_exit_rules(rec: dict) -> list[dict]:
    """Structured exit rules — list of {rule, when, action}."""
    tf = rec["timeframe"]
    target = rec["est_target_premium"]
    stop = rec["est_stop_premium"]

    rules = []

    # 1. Profit target — always present
    rules.append({
        "type": "TARGET",
        "color": "#3fb950",
        "icon": "✅",
        "when": f"Premium reaches ~${target:.2f} per contract (+{int((target/rec['est_entry_premium']-1)*100)}%)",
        "action": "Sell to close — lock in the profit",
    })

    # 2. Hard stop — always present
    rules.append({
        "type": "STOP LOSS",
        "color": "#f85149",
        "icon": "🛑",
        "when": f"Premium drops to ~${stop:.2f} (~50% loss) OR underlying breaks the strategy stop",
        "action": "Sell to close — cut the loss, do not 'wait it out'",
    })

    # 3. Timeframe-specific time stop
    if tf == "15m":
        rules.append({
            "type": "TIME STOP",
            "color": "#eab308",
            "icon": "⏰",
            "when": "By 3:45 PM ET — no matter what",
            "action": "Sell to close. 0 DTE options go to ZERO after expiry. NEVER hold overnight.",
        })
        rules.append({
            "type": "QUICK FADE",
            "color": "#8b949e",
            "icon": "⌛",
            "when": "Still flat (no movement) 2 hours after entry",
            "action": "Strongly consider closing — most winners on this timeframe move within the first hour. "
                      "Median winner: 4-9 hrs, median loser: 30 min.",
        })
    elif tf == "1h":
        rules.append({
            "type": "TIME STOP",
            "color": "#eab308",
            "icon": "⏰",
            "when": "Morning of expiration day (if not yet hit target/stop)",
            "action": "Sell to close. Theta accelerates the last day — don't get caught.",
        })
        rules.append({
            "type": "TYPICAL HOLD",
            "color": "#8b949e",
            "icon": "⌛",
            "when": "Median winner: 17-20 hours · median loser: 9-13 hours",
            "action": "If still flat after a full session, reassess the original thesis.",
        })
    else:  # 1d
        rules.append({
            "type": "SCALE OUT",
            "color": "#58a6ff",
            "icon": "📈",
            "when": f"Premium reaches +50% (~${rec['est_entry_premium']*1.5:.2f})",
            "action": "Sell HALF of your contracts. Trail the stop on the rest to your entry price (breakeven).",
        })
        rules.append({
            "type": "TIME STOP",
            "color": "#eab308",
            "icon": "⏰",
            "when": "2 days before expiration if still open and underwater",
            "action": "Close everything. Theta crushes premium in the final week.",
        })
        rules.append({
            "type": "TYPICAL HOLD",
            "color": "#8b949e",
            "icon": "⌛",
            "when": "Median hold: 7 days · Winners avg 10-11 days, losers 7-8 days",
            "action": "Plan around this — don't expect a same-week resolution.",
        })

    return rules


def _build_news_watch(rec: dict) -> list[str]:
    """What news to watch out for that could kill this trade."""
    symbol = rec["symbol"]
    direction = rec["direction"]
    items = []

    # Universal events
    items.append("FOMC rate decisions, Powell speeches, CPI/PPI releases — can flip any direction in seconds")
    items.append("Geopolitical headlines (Middle East, China, Russia/Ukraine) — risk-off can sink longs / spike gold")

    if symbol in ("ES=F", "NQ=F"):
        items.append("Big tech earnings (NVDA, AAPL, MSFT, META, GOOGL, AMZN) — single names can swing both indexes")
        items.append("Unemployment claims (Thu 8:30 AM ET), NFP (1st Fri of month)")
        if direction == "LONG":
            items.append("🔴 EXIT EARLY: surprise hawkish Fed, hot inflation, recession data, war escalation")
        else:
            items.append("🔴 EXIT EARLY: dovish Fed surprise, ceasefire news, blowout earnings beat")
    elif symbol == "GC=F":
        items.append("Dollar (DXY) moves — gold trades inverse to the dollar")
        items.append("Real yields (10Y TIPS) — rising real yields = gold headwind")
        items.append("Central bank gold buying news (esp. China, Russia, India)")
        if direction == "LONG":
            items.append("🔴 EXIT EARLY: hot CPI + rising yields + strong dollar all at once")
        else:
            items.append("🔴 EXIT EARLY: war escalation, banking crisis, surprise rate cut")

    return items


def _build_skip_conditions(rec: dict) -> list[str]:
    """When NOT to enter this trade."""
    tf = rec["timeframe"]
    items = []

    # Universal
    items.append(f"Live quote is more than 20% above the estimated premium (${rec['est_entry_premium']:.2f}) — "
                 f"you're chasing, signal already moved")
    items.append("Bid-ask spread is wider than 15% of mid-price — illiquid, you'll get filled badly")
    items.append("Major scheduled event (FOMC, CPI, earnings) in the next 2 hours — IV crush risk after")

    if tf == "15m":
        items.append("It's after 2:00 PM ET — not enough time left for 0 DTE to work")
        items.append("Friday afternoon — weekend theta + Sunday gap risk on the underlying")
    elif tf == "1h":
        items.append("Friday after 12 PM ET — weekly options expire same day, theta accelerating fast")
    else:
        items.append("Less than 5 DTE available — pick a different week's expiration if possible")

    return items


# ─── Email ─────────────────────────────────────────────────────────────

def _admin_recipient() -> str:
    return (
        os.environ.get("BALDWETCOBY_EMAIL_TO")
        or os.environ.get("GMAIL_USER")
        or "baldwetcoby@gmail.com"
    )


def send_alert_email(rec: dict, signal_note: str) -> bool:
    user = os.environ.get("GMAIL_USER", "")
    pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not user or not pw:
        log("email skipped — GMAIL creds missing", "WARN")
        return False

    to = _admin_recipient()
    ct_letter = rec["contract_type"][0]
    contract_label = f"{rec['options_root']} {rec['strike']}{ct_letter}"
    tier = rec["conviction_tier"]
    score_int = int(rec["score"])
    ct = rec["contract_type"]
    direction_word = "UP" if rec["direction"] == "LONG" else "DOWN"

    # Build all the content pieces
    explanation = _build_trade_explanation(rec)
    exit_rules = _build_exit_rules(rec)
    news_items = _build_news_watch(rec)
    skip_conditions = _build_skip_conditions(rec)

    # Limit price for the order — pay no more than 10% above estimated premium
    limit_price = round(rec["est_entry_premium"] * 1.10, 2)

    subject = (
        f"[FUTURES OPTIONS] BUY {rec['symbol']} {rec['strike']} {ct} "
        f"({rec['expiry_guide']}) · ${rec['total_cost_est']:.0f} · {score_int}% {tier}"
    )

    # ─── Plain text version ───
    rules_text = "\n".join(
        f"  {r['icon']}  {r['type']}\n"
        f"      WHEN: {r['when']}\n"
        f"      DO:   {r['action']}"
        for r in exit_rules
    )
    news_text = "\n".join(f"  • {n}" for n in news_items)
    skip_text = "\n".join(f"  • {s}" for s in skip_conditions)
    micro_line = (f"\n  NOTE: Using {rec['micro_note']} to keep cost under ${MAX_TRADE_COST}."
                  if rec.get("is_micro") else "")

    text = (
        f"FUTURES OPTIONS ALERT — {rec['symbol']} ({direction_word} bet)\n"
        f"{'='*60}\n\n"

        f"TL;DR\n{'-'*60}\n"
        f"  BUY {rec['contracts']} contract{'s' if rec['contracts'] > 1 else ''} of "
        f"{rec['options_root']} {rec['strike']} {ct} "
        f"expiring {rec['expiry_guide']}\n"
        f"  Pay no more than ~${limit_price:.2f}/contract (use a LIMIT order)\n"
        f"  Total estimated cost: ~${rec['total_cost_est']:.2f}\n"
        f"  Conviction: {score_int}/100 ({tier}){micro_line}\n\n"

        f"WHAT THIS TRADE IS\n{'-'*60}\n"
        f"  {explanation}\n\n"

        f"RISK\n{'-'*60}\n"
        f"  This is a LONG {ct} (you BUY it). Max loss = the premium you pay "
        f"(~${rec['total_cost_est']:.2f} total). You cannot lose more than that.\n"
        f"  Broker: {rec.get('broker', 'your broker')}. Use a LIMIT order — "
        f"do not pay more than ~${limit_price:.2f}/contract.\n\n"

        f"WHEN TO SELL (in priority order)\n{'-'*60}\n"
        f"{rules_text}\n\n"

        f"NEWS / EVENTS TO WATCH\n{'-'*60}\n"
        f"{news_text}\n\n"

        f"DON'T ENTER IF\n{'-'*60}\n"
        f"{skip_text}\n\n"

        f"CONTEXT\n{'-'*60}\n"
        f"  Underlying: {rec['underlying_name']} @ ${rec['underlying_price']:,.2f}\n"
        f"  Daily ATR: ${rec['atr']:.2f} (typical day's range)\n"
        f"  Strategy: {rec['strategy_name']} on {rec['timeframe']} chart\n"
        f"  Signal detail: {signal_note}\n"
        f"  Approx delta: {rec['delta_est']}\n\n"

        f"Premiums are ESTIMATES from ATR. Real quotes will vary — "
        f"always check the live bid/ask before placing the order.\n"
        f"Paper-trade signal — not financial advice.\n"
    )

    # ─── HTML version ───
    tier_bg = "#22c55e" if tier == "HIGH" else "#eab308"
    ct_color = "#3fb950" if ct == "CALL" else "#f85149"
    arrow = "▲" if rec["direction"] == "LONG" else "▼"

    # Exit rules HTML
    rules_html = ""
    for r in exit_rules:
        rules_html += f"""
        <tr>
          <td style="padding:8px 10px;border-bottom:1px solid #21262d;vertical-align:top;width:100px;">
            <div style="color:{r['color']};font-weight:700;font-size:12px;">{r['icon']} {r['type']}</div>
          </td>
          <td style="padding:8px 10px;border-bottom:1px solid #21262d;vertical-align:top;">
            <div style="color:#e6edf3;font-size:12px;margin-bottom:2px;"><b>When:</b> {html_escape(r['when'])}</div>
            <div style="color:#8b949e;font-size:12px;"><b>Do:</b> {html_escape(r['action'])}</div>
          </td>
        </tr>
        """

    # News HTML
    news_html = "".join(
        f'<li style="margin-bottom:4px;">{html_escape(n)}</li>'
        for n in news_items
    )

    # Skip conditions HTML
    skip_html = "".join(
        f'<li style="margin-bottom:4px;">{html_escape(s)}</li>'
        for s in skip_conditions
    )

    micro_html = ""
    if rec.get("is_micro"):
        micro_html = (
            f'<div style="background:rgba(234,179,8,0.10);border-left:3px solid #eab308;'
            f'padding:8px 12px;margin-top:10px;border-radius:4px;font-size:12px;color:#e6edf3;">'
            f'<b>⚠ Using micro contracts:</b> {html_escape(rec["micro_note"])} '
            f'to keep total cost under ${MAX_TRADE_COST}.'
            f'</div>'
        )

    html_body = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;background:#0d1117;color:#e6edf3;padding:20px;margin:0;">
<div style="max-width:680px;margin:0 auto;background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;">

  <!-- Header badges -->
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;flex-wrap:wrap;">
    <span style="background:#bc8cff;color:#0d1117;padding:4px 12px;border-radius:4px;font-weight:700;font-size:12px;letter-spacing:0.5px;">
      FUTURES OPTIONS
    </span>
    <span style="color:#8b949e;font-size:12px;">Score {score_int}/100</span>
    <span style="background:{tier_bg};color:#0d1117;padding:3px 10px;border-radius:3px;font-weight:700;font-size:11px;letter-spacing:0.5px;">
      {tier}
    </span>
    <span style="color:#8b949e;font-size:12px;">{rec['timeframe']} signal · {rec['horizon']} trade</span>
  </div>

  <!-- TL;DR action box (the most important part) -->
  <div style="background:#0d1117;border:2px solid {ct_color};border-radius:10px;padding:16px;margin-bottom:16px;">
    <div style="color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
      The trade
    </div>
    <div style="font-size:20px;font-weight:700;color:{ct_color};margin-bottom:8px;">
      {arrow} BUY {rec['contracts']} × {rec['options_root']} {rec['strike']} {ct}
    </div>
    <div style="color:#e6edf3;font-size:14px;margin-bottom:4px;">
      Expires <b>{rec['expiry_guide']}</b> · Limit ~<b>${limit_price:.2f}</b>/contract
    </div>
    <div style="color:#eab308;font-size:14px;font-weight:600;">
      Total cost: ~${rec['total_cost_est']:.2f}
    </div>
    {micro_html}
  </div>

  <!-- Underlying context -->
  <div style="background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:12px 14px;margin-bottom:16px;font-size:13px;">
    <b style="color:#58a6ff;">{rec['symbol']}</b> — {html_escape(rec['underlying_name'])} @ ${rec['underlying_price']:,.2f}
    <span style="color:#8b949e;"> · ATR ${rec['atr']:.2f}</span>
    {"<span style='color:#8b949e;'> · <b style='color:#e6edf3;'>GLD ≈ $" + f"{rec['etf_price']:.2f}" + "</b> (gold ÷ 10)</span>" if rec.get('uses_etf_proxy') else ""}
    <br>
    <span style="color:#8b949e;font-size:12px;">
      Signal: <b style="color:#e6edf3;">{html_escape(rec['strategy_name'])}</b> ({rec['timeframe']} chart)
      · {html_escape(signal_note)}
    </span>
  </div>

  <!-- What this trade is -->
  <h3 style="color:#d29922;margin:18px 0 8px 0;font-size:14px;">📖 What this trade is</h3>
  <p style="margin:0;line-height:1.6;font-size:13px;color:#c9d1d9;">
    {html_escape(explanation)}
  </p>

  <!-- Risk / safety -->
  <div style="background:rgba(63,185,80,0.10);border-left:3px solid #3fb950;padding:10px 14px;margin:16px 0;border-radius:4px;font-size:13px;color:#c9d1d9;line-height:1.5;">
    <b style="color:#3fb950;">✓ Long {ct} — your loss is capped at the premium paid (~${rec['total_cost_est']:.2f} total).</b>
    You cannot lose more than that no matter how far the underlying moves against you.
    Broker: <b>{html_escape(rec.get('broker', 'your broker'))}</b>. Use a LIMIT order at ~${limit_price:.2f}/contract — never market.
  </div>

  <!-- Exit rules — the big one -->
  <h3 style="color:#d29922;margin:20px 0 8px 0;font-size:14px;">🎯 When to sell (in priority order)</h3>
  <div style="background:#0d1117;border:1px solid #30363d;border-radius:8px;overflow:hidden;">
    <table style="width:100%;border-collapse:collapse;">
      {rules_html}
    </table>
  </div>

  <!-- News watch -->
  <h3 style="color:#d29922;margin:20px 0 8px 0;font-size:14px;">📰 News &amp; events to watch</h3>
  <p style="margin:0 0 6px 0;color:#8b949e;font-size:12px;">
    These can move the underlying enough to invalidate the signal. Close early if you see surprise news against your direction.
  </p>
  <ul style="margin:0;padding-left:22px;line-height:1.5;font-size:12px;color:#c9d1d9;">
    {news_html}
  </ul>

  <!-- Don't enter if -->
  <h3 style="color:#f85149;margin:20px 0 8px 0;font-size:14px;">⛔ Don't enter if</h3>
  <ul style="margin:0;padding-left:22px;line-height:1.5;font-size:12px;color:#c9d1d9;">
    {skip_html}
  </ul>

  <!-- Footer -->
  <div style="margin-top:24px;padding-top:16px;border-top:1px solid #30363d;">
    <p style="color:#484f58;font-size:11px;text-align:center;margin:0 0 4px 0;">
      Premiums are <b>estimates from ATR</b> — real bid/ask will differ. Always check live quotes.
    </p>
    <p style="color:#484f58;font-size:11px;text-align:center;margin:0;">
      Paper-trade signal — not financial advice. Do your own due diligence.
    </p>
  </div>

</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
        log(f"email sent → {to} :: {subject[:80]}")
        return True
    except Exception as e:
        log(f"email send failed: {e}", "ERROR")
        return False


def html_escape(s) -> str:
    """Minimal HTML escape for safety in email bodies."""
    import html as _html
    return _html.escape(str(s) if s is not None else "")


# ─── Scanner ───────────────────────────────────────────────────────────

def scan_once(dry_run: bool = False) -> list[dict]:
    """Run one scan pass. Returns list of recommendations that fired."""
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc - timedelta(hours=4)  # approximate ET
    log(f"scanning {len(SCAN_SYMBOLS)} symbols × {len(SCAN_TIMEFRAMES)} timeframes "
        f"× {len(SCAN_STRATEGIES)} strategies")

    cd = _load_cooldown()
    fired: list[dict] = []

    for symbol in SCAN_SYMBOLS:
        for tf in SCAN_TIMEFRAMES:
            df = load_bars(symbol, tf, 300)
            if df is None or len(df) < 60:
                log(f"  {symbol}/{tf}: insufficient data ({len(df) if df is not None else 0} bars)")
                continue

            scores = score_strategies(
                symbol, df, tf=tf,
                strategy_filter=set(SCAN_STRATEGIES),
            )

            for strat in SCAN_STRATEGIES:
                if (symbol, strat) in SYMBOL_STRATEGY_EXCLUDE:
                    continue

                result = scores.get(strat, {})
                score = result.get("score", 0)
                direction = result.get("direction", "NONE")
                note = result.get("note", "")

                if score < SCORE_THRESHOLD or direction == "NONE":
                    continue

                key = _cooldown_key(symbol, strat, tf, direction)
                if _is_on_cooldown(cd, key):
                    log(f"  {symbol}/{tf}/{strat}: score={score:.0f} {direction} — COOLDOWN (skip)")
                    continue

                log(f"  {symbol}/{tf}/{strat}: score={score:.0f} {direction} — SIGNAL!")

                price = float(df["close"].iloc[-1])
                atr_s = atr(df, 14)
                atr_val = float(atr_s.iloc[-1]) if not pd.isna(atr_s.iloc[-1]) else price * 0.01

                rec = _recommend_option(symbol, direction, price, atr_val,
                                        score, strat, tf)
                rec["signal_note"] = note
                rec["scan_time"] = now_utc.isoformat()

                fired.append(rec)

                if not dry_run:
                    send_alert_email(rec, note)
                    cd[key] = now_utc.isoformat()
                else:
                    log(f"    [DRY RUN] would email: BUY {rec['contract_type']} "
                        f"{rec['options_root']} {rec['strike']} ({rec['expiry_guide']})")

    if not dry_run:
        _save_cooldown(cd)

    # Persist picks for the morning brief
    _save_picks(fired)

    log(f"scan complete — {len(fired)} signal(s) fired")
    return fired


def _save_picks(picks: list[dict]) -> None:
    """Merge new picks into the JSON file the morning brief reads.
    Keep picks from the last 24 hours (covers overnight + pre-brief scan)."""
    existing = []
    if PICKS_FILE.exists():
        try:
            existing = json.loads(PICKS_FILE.read_text())
        except Exception:
            existing = []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    def _is_recent(p):
        try:
            return datetime.fromisoformat(p.get("scan_time", "")) >= cutoff
        except (ValueError, TypeError):
            return False

    recent_existing = [p for p in existing if _is_recent(p)]

    # Deduplicate by symbol+strategy+tf+direction; newer picks override older
    seen = set()
    merged = []
    for p in picks + recent_existing:  # new first so they win dedup
        key = (p["symbol"], p["strategy"], p["timeframe"], p["direction"])
        if key not in seen:
            seen.add(key)
            merged.append(p)
    merged.sort(key=lambda p: p.get("score", 0), reverse=True)

    PICKS_FILE.write_text(json.dumps(merged, indent=2))
    log(f"saved {len(merged)} pick(s) → {PICKS_FILE}")


# ─── Main ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Futures options signal scanner")
    ap.add_argument("--dry-run", action="store_true",
                    help="scan + print, no email")
    ap.add_argument("--loop", action="store_true",
                    help="repeat every 30 minutes")
    args = ap.parse_args()

    if args.loop:
        log("starting loop mode (30 min interval)")
        while True:
            try:
                scan_once(dry_run=args.dry_run)
            except Exception as e:
                log(f"scan error: {e}", "ERROR")
            time.sleep(1800)
    else:
        results = scan_once(dry_run=args.dry_run)
        if results:
            for r in results:
                print(f"\n{'='*60}")
                print(f"  {r['symbol']} {r['contract_type']} {r['strike']} "
                      f"({r['expiry_guide']})")
                print(f"  Strategy: {r['strategy_name']} ({r['timeframe']})")
                print(f"  Score: {r['score']}/100 ({r['conviction_tier']})")
                print(f"  Est. premium: ${r['est_entry_premium']:.2f} "
                      f"× {r['contracts']} = ${r['total_cost_est']:.2f}")
                print(f"  Target: ${r['est_target_premium']:.2f}  "
                      f"Stop: ${r['est_stop_premium']:.2f}")
                print(f"  Signal: {r['signal_note']}")
        else:
            print("No signals above threshold.")


if __name__ == "__main__":
    main()

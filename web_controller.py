#!/usr/bin/env python3
"""
web_controller.py — Futures Server Controller (Flask + PWA)

Serves the single-page app (templates/index.html + static/*) and a REST API
that the web/mobile dashboard and the desktop controller talk to.

Endpoints:
  GET  /                          -- SPA
  GET  /manifest.json             -- PWA manifest
  GET  /sw.js                     -- service worker
  GET  /api/status                -- session + autopilot summary
  GET  /api/prices                -- latest polled prices
  GET  /api/trades                -- closed trade history + open positions
  GET  /api/candles/<symbol>      -- OHLCV for charting
  GET  /api/ai-overview           -- latest autopilot decision + market scores
  GET  /api/news-digest           -- macro headlines
  GET  /api/log                   -- tail of autopilot_log.txt
  GET  /api/health                -- basic diagnostics
  GET  /api/services              -- service status (systemd if present)
  POST /api/services/<name>/<action>  -- start|stop|restart
  POST /api/autopilot/run-now     -- force one autopilot decision

Auth:
  Two tokens: WEB_CONTROLLER_TOKEN (full admin) and WEB_VIEWER_TOKEN (read-only).
  If missing from .env, random tokens are generated and printed at boot.
  Pass as Authorization: Bearer <token>  or  ?token=<token>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, render_template

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

from futures_config import DB_PATH, FUTURES, TIMEFRAMES
from market_analyzer import get_market_overview, load_bars
from strategy_engine import CONTRACT_SPECS
import news_provider


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_PATH = os.path.join(BASE_DIR, "trade_log.json")
MINIMAX_INSIGHTS = os.path.join(BASE_DIR, "minimax_insights.json")
AUTOPILOT_LOG = os.path.join(BASE_DIR, "autopilot_log.txt")


app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)


# ─── Auth ──────────────────────────────────────────────────────────────
ADMIN_TOKEN = os.environ.get("WEB_CONTROLLER_TOKEN", "")
VIEWER_TOKEN = os.environ.get("WEB_VIEWER_TOKEN", "")

if not ADMIN_TOKEN:
    ADMIN_TOKEN = secrets.token_urlsafe(32)
    print("\n" + "=" * 60)
    print(f"  WEB_CONTROLLER_TOKEN not set — generated: {ADMIN_TOKEN}")
    print(f"  Add to .env:  WEB_CONTROLLER_TOKEN={ADMIN_TOKEN}")
    print("=" * 60)

if not VIEWER_TOKEN:
    VIEWER_TOKEN = secrets.token_urlsafe(32)
    print(f"  WEB_VIEWER_TOKEN not set — generated: {VIEWER_TOKEN}")
    print(f"  Add to .env:  WEB_VIEWER_TOKEN={VIEWER_TOKEN}\n")


def get_role() -> str | None:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.args.get("token", "")
    if token and token == ADMIN_TOKEN:
        return "admin"
    if token and token == VIEWER_TOKEN:
        return "viewer"
    return None


def require_role(*roles):
    def deco(fn):
        @wraps(fn)
        def inner(*args, **kw):
            role = get_role()
            if role not in roles:
                return jsonify({"error": "unauthorized"}), 401
            request.role = role
            return fn(*args, **kw)
        return inner
    return deco


# ─── Service control ───────────────────────────────────────────────────
ALLOWED_SERVICES = {
    "futures-autopilot":   "AI trading autopilot",
    "futures-live-prices": "Live price poller",
    "futures-analyzer":    "Market analyzer",
    "futures-minimax":     "MiniMax 10-min AI",
    "futures-collector":   "yfinance candle collector",
}


def _systemd_available() -> bool:
    try:
        return subprocess.run(["systemctl", "--version"],
                              capture_output=True, timeout=2).returncode == 0
    except Exception:
        return False


def _svc_status(name: str) -> dict:
    if not _systemd_available():
        return {"name": name, "state": "unavailable", "desc": ALLOWED_SERVICES[name]}
    try:
        p = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=5,
        )
        state = p.stdout.strip() or "unknown"
        return {"name": name, "state": state, "desc": ALLOWED_SERVICES[name]}
    except Exception as e:
        return {"name": name, "state": f"error: {e}", "desc": ALLOWED_SERVICES[name]}


# ─── Helpers ───────────────────────────────────────────────────────────
def _read_trade_log() -> list:
    if not os.path.exists(TRADE_LOG_PATH):
        return []
    try:
        with open(TRADE_LOG_PATH) as f:
            raw = f.read().strip()
            return json.loads(raw) if raw else []
    except Exception:
        return []


def _read_latest_prices() -> dict:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT symbol, last, day_open, day_high, day_low, prev_close, "
            "volume, updated_at FROM latest_prices"
        )
        out = {}
        for r in cur.fetchall():
            sym, last, o, h, l, pc, v, ts = r
            pct = ((last - pc) / pc * 100) if (last and pc) else 0.0
            out[sym] = {
                "symbol": sym, "name": FUTURES.get(sym, sym),
                "last": last, "open": o, "high": h, "low": l, "prev_close": pc,
                "change_pct": pct, "volume": v, "updated_at": ts,
            }
        conn.close()
        return out
    except Exception:
        return {}


def _read_autopilot_log(lines: int = 100) -> str:
    if not os.path.exists(AUTOPILOT_LOG):
        return ""
    try:
        with open(AUTOPILOT_LOG) as f:
            content = f.readlines()
        return "".join(content[-lines:])
    except Exception:
        return ""


def _read_last_autopilot_decision() -> dict | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT ts, risk_mode, enabled, reasoning, ai_model "
            "FROM autopilot_log ORDER BY id DESC LIMIT 1"
        )
        r = cur.fetchone()
        conn.close()
        if not r:
            return None
        return {
            "ts": r[0], "risk_mode": r[1],
            "enabled": r[2].split(",") if r[2] else [],
            "reasoning": r[3], "ai_model": r[4],
        }
    except Exception:
        return None


def _session_summary() -> dict:
    trades = _read_trade_log()
    closed = [t for t in trades if t.get("status") == "closed"]
    wins = [t for t in closed if t.get("pnl_dollars", 0) > 0]
    total_pnl = sum(t.get("pnl_dollars", 0) for t in closed)
    starting = 10_000.0
    return {
        "balance": starting + total_pnl,
        "starting_balance": starting,
        "total_pnl": total_pnl,
        "total_pnl_pct": (total_pnl / starting * 100) if starting else 0.0,
        "trades": len(closed),
        "wins": len(wins),
        "win_rate": (len(wins) / len(closed) * 100) if closed else 0.0,
    }


# ─── Routes: static / SPA ──────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json")


@app.route("/sw.js")
def service_worker():
    return send_from_directory(app.static_folder, "sw.js")


# ─── Routes: API ───────────────────────────────────────────────────────
@app.route("/api/status")
@require_role("admin", "viewer")
def api_status():
    session = _session_summary()
    last_ai = _read_last_autopilot_decision()
    return jsonify({
        "session": session,
        "autopilot": last_ai,
        "role": request.role,
        "as_of": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/prices")
@require_role("admin", "viewer")
def api_prices():
    return jsonify(_read_latest_prices())


@app.route("/api/trades")
@require_role("admin", "viewer")
def api_trades():
    trades = _read_trade_log()
    return jsonify({
        "trades": trades,
        "open": [t for t in trades if t.get("status") == "open"],
        "closed": [t for t in trades if t.get("status") == "closed"],
    })


@app.route("/api/candles/<path:symbol>")
@require_role("admin", "viewer")
def api_candles(symbol: str):
    tf = request.args.get("timeframe", "1h")
    limit = int(request.args.get("limit", 300))
    if symbol not in FUTURES:
        return jsonify({"error": "unknown symbol"}), 400
    if tf not in TIMEFRAMES:
        return jsonify({"error": "unknown timeframe"}), 400
    df = load_bars(symbol, tf, limit)
    if df is None:
        return jsonify({"candles": []})
    rows = df[["datetime", "open", "high", "low", "close", "volume"]].copy()
    rows["datetime"] = rows["datetime"].astype(str)
    return jsonify({
        "symbol": symbol, "name": FUTURES[symbol], "timeframe": tf,
        "candles": rows.to_dict(orient="records"),
    })


@app.route("/api/ai-overview")
@require_role("admin", "viewer")
def api_ai_overview():
    overview = get_market_overview()
    decision = _read_last_autopilot_decision()
    minimax = None
    if os.path.exists(MINIMAX_INSIGHTS):
        try:
            with open(MINIMAX_INSIGHTS) as f:
                minimax = json.load(f)
        except Exception:
            minimax = None
    return jsonify({"overview": overview, "autopilot": decision, "minimax": minimax})


@app.route("/api/news-digest")
@require_role("admin", "viewer")
def api_news():
    try:
        return jsonify(news_provider.get_digest())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/log")
@require_role("admin", "viewer")
def api_log():
    lines = int(request.args.get("lines", 100))
    return jsonify({"log": _read_autopilot_log(lines)})


@app.route("/api/health")
@require_role("admin", "viewer")
def api_health():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM candles")
    candle_ct = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM trades")
    trade_ct = cur.fetchone()[0]
    cur.execute("SELECT MAX(updated_at) FROM latest_prices")
    last_tick = cur.fetchone()[0]
    conn.close()
    return jsonify({
        "db_path": DB_PATH,
        "candle_count": candle_ct,
        "trade_count": trade_ct,
        "last_live_tick": last_tick,
        "systemd": _systemd_available(),
        "python": sys.version.split()[0],
    })


@app.route("/api/services")
@require_role("admin", "viewer")
def api_services():
    return jsonify({"services": [_svc_status(n) for n in ALLOWED_SERVICES]})


@app.route("/api/services/<name>/<action>", methods=["POST"])
@require_role("admin")
def api_service_action(name: str, action: str):
    if name not in ALLOWED_SERVICES:
        return jsonify({"error": "unknown service"}), 400
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "bad action"}), 400
    if not _systemd_available():
        return jsonify({"error": "systemd not available on this host"}), 400
    try:
        p = subprocess.run(
            ["sudo", "systemctl", action, name],
            capture_output=True, text=True, timeout=15,
        )
        return jsonify({"ok": p.returncode == 0, "stdout": p.stdout, "stderr": p.stderr})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/autopilot/run-now", methods=["POST"])
@require_role("admin")
def api_autopilot_run_now():
    """Fire a single AI decision without waiting for the hourly timer."""
    try:
        from strategy_engine import StrategyEngine
        from autopilot import AutoPilot
        eng = StrategyEngine()
        ap_ = AutoPilot(eng)
        decision = ap_.run_once()
        return jsonify({"ok": True, "decision": decision})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─── Main ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Futures Controller web app")
    ap.add_argument("--port", type=int, default=5100)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    from db_setup import create_database
    create_database()

    print(f"Futures Controller running on http://{args.host}:{args.port}")
    print(f"Admin token:  {ADMIN_TOKEN}")
    print(f"Viewer token: {VIEWER_TOKEN}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
minimax_insights.py — MiniMax dual-model AI insights for the 18-market futures book.

Mirrors the crypto-app architecture: step 1 = M2.7 deep analysis (text),
step 2 = M2.5 structured JSON signals per-strategy. Falls back to a local
stub if MINIMAX_API_KEY is missing so downstream readers never break.

Output: minimax_insights.json (read by run_autopilot.py + web_controller.py)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from futures_config import DB_PATH, FUTURES
from market_analyzer import get_market_overview, load_bars, STRATEGIES


INSIGHTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minimax_insights.json")
LOG_PATH      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minimax_insights_log.txt")
API_BASE      = "https://api.minimax.io/v1/text/chatcompletion_v2"

MODEL_DEEP    = "MiniMax-M2.7"
MODEL_SIGNALS = "MiniMax-M2.5"

MAX_DEEP_TOKENS   = 4000
MAX_SIGNAL_TOKENS = 1500


# ─── API helper ─────────────────────────────────────────────────────────
def _call(prompt: str, api_key: str, model: str, max_tokens: int) -> tuple[str, int]:
    """Return (content, total_tokens_used)."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        API_BASE, data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        return (f"[minimax http {e.code}: {e.read().decode(errors='ignore')[:200]}]", 0)
    except Exception as e:
        return (f"[minimax error: {e}]", 0)

    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        total = int(usage.get("total_tokens", 0) or 0)
        return content, total
    except Exception:
        return raw[:4000], 0


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return {}


# ─── Prompt builders ────────────────────────────────────────────────────
def _build_deep_prompt(overview: dict) -> str:
    """Compact market table + strategy scores across all 18 symbols, request 10-point analysis."""
    rows = []
    for sym, snap in overview.items():
        if "error" in snap:
            continue
        ind = snap.get("indicators", {}) or {}
        best = max(snap.get("strategies", {}).items(),
                   key=lambda kv: kv[1].get("score", 0), default=(None, None))
        bname, binfo = best
        rows.append(
            f"{sym} {snap.get('name','')} last={snap.get('last',0):.4f} "
            f"24h={snap.get('change_24h_pct',0):+.2f}% "
            f"RSI={ind.get('rsi',50):.0f} "
            f"MACD_hist={ind.get('macd_hist',0):+.4f} "
            f"vol_ratio={ind.get('vol_ratio',1):.2f}x "
            f"best_strat={bname} ({binfo['direction'] if binfo else ''}, "
            f"{binfo['score'] if binfo else 0:.0f})"
        )

    return f"""You are a senior US-futures analyst serving a 24/7 paper-trading autopilot across 18 contracts
(indexes ES/NQ/YM/RTY, energy CL/NG, metals GC/SI/HG, bonds ZB/ZN, grains ZC/ZS/ZW, softs KC/SB/CT, cattle LE).

Current market snapshot (1h bars, top strategy score per symbol):
{chr(10).join(rows)}

Deliver a 10-point analysis (under 1500 words total, plain prose, no markdown):

1. OVERALL RISK REGIME — risk-on vs risk-off vs mixed, with the 2-3 strongest tells.
2. CORRELATIONS & DIVERGENCES — any markets that normally move together and aren't (e.g. HG vs ES; GC vs bonds).
3. TREND STRENGTH — which markets are in clean trends vs chopping ranges.
4. VOLUME & VOLATILITY — any notable vol expansion or contraction worth trading.
5. PATTERN RECOGNITION — chart patterns forming (flags, H&S, double tops, squeezes).
6. TOP 3 LONG OPPORTUNITIES — with entry, stop, target rationale.
7. TOP 3 SHORT OPPORTUNITIES — same format.
8. MARKETS TO AVOID — which look like traps or noise right now.
9. KEY LEVELS — support/resistance by symbol, prices not words.
10. TIMING — which strategies should be PRIORITIZED vs DISABLED this cycle, and why.

Be specific, no hedging adjectives. Cite price levels."""


def _build_signals_prompt(overview: dict, deep_excerpt: str) -> str:
    """Ask M2.5 for a structured JSON the autopilot + UI can consume directly."""
    sym_list = list(overview.keys())[:18]
    strat_keys = list(STRATEGIES.keys())

    return f"""You are converting a long-form analysis into structured trading signals.

Available strategy keys (score 0-100 each, enable true/false):
{', '.join(strat_keys)}

Available symbols:
{', '.join(sym_list)}

Recent deep analysis (first 3000 chars):
{deep_excerpt[:3000]}

Return ONLY a JSON object with exactly this shape (no markdown):
{{
  "overall_bias": "BULLISH" | "BEARISH" | "NEUTRAL",
  "confidence": 0-100,
  "regime": "STRONG_BULL" | "BULL" | "NEUTRAL" | "BEAR" | "STRONG_BEAR",
  "danger_level": "LOW" | "MEDIUM" | "HIGH" | "EXTREME",
  "risk_mode": "conservative" | "moderate" | "aggressive",
  "immediate_action": "WAIT" | "ENTER_LONG" | "ENTER_SHORT" | "MANAGE_EXISTING",
  "strategies": {{
    "<strategy_key>": {{"enable": true/false, "score": 0-100, "reason": "<15 words"}},
    ...one per strategy in the list above...
  }},
  "key_levels": {{
    "<symbol>": {{"strong_support": <price>, "support": <price>, "resistance": <price>, "strong_resistance": <price>}},
    ...for the top 6 most-actionable symbols...
  }},
  "scenarios": [
    {{"name": "...", "probability": 0-100, "target_symbol": "...", "target_price": <price>, "trigger": "...", "action": "..."}},
    ...3 scenarios ordered by probability...
  ]
}}"""


# ─── Runner ─────────────────────────────────────────────────────────────
def run_once() -> dict:
    t0 = time.time()
    ts = datetime.now(timezone.utc).isoformat()
    overview = get_market_overview(tf="1h")

    market_state = {}
    for sym, snap in overview.items():
        if "error" in snap:
            continue
        ind = snap.get("indicators", {}) or {}
        market_state[sym] = {
            "price": snap.get("last"),
            "change_24h_pct": snap.get("change_24h_pct"),
            "rsi": ind.get("rsi"),
            "macd_hist": ind.get("macd_hist"),
            "vol_ratio": ind.get("vol_ratio"),
            "atr": ind.get("atr"),
        }

    key = os.environ.get("MINIMAX_API_KEY", "").strip()

    if not key:
        # Local stub — rank strategies by best score across all symbols
        best_per_strat = {}
        for snap in overview.values():
            if "error" in snap:
                continue
            for sid, s in snap.get("strategies", {}).items():
                best_per_strat[sid] = max(best_per_strat.get(sid, 0), s.get("score", 0))
        structured = {
            "overall_bias": "NEUTRAL",
            "confidence": 30,
            "regime": "NEUTRAL",
            "danger_level": "MEDIUM",
            "risk_mode": "moderate",
            "immediate_action": "WAIT",
            "strategies": {sid: {"enable": sc >= 55, "score": int(sc), "reason": "local heuristic"}
                             for sid, sc in best_per_strat.items()},
            "key_levels": {},
            "scenarios": [],
        }
        out = {
            "generated_at": ts,
            "cycle_duration_seconds": round(time.time() - t0, 2),
            "total_tokens_used": 0,
            "token_breakdown": {"analysis_in": 0, "analysis_out": 0, "signals_in": 0, "signals_out": 0},
            "market_state": market_state,
            "analysis_report": "[no MINIMAX_API_KEY — stub output]",
            "structured_signals": structured,
            "open_trades": [],
            "source": "stub",
        }
    else:
        # Step 1: deep analysis
        deep_prompt = _build_deep_prompt(overview)
        deep_text, deep_tokens = _call(deep_prompt, key, MODEL_DEEP, MAX_DEEP_TOKENS)
        # Step 2: structured signals
        signals_prompt = _build_signals_prompt(overview, deep_text)
        signals_text, signals_tokens = _call(signals_prompt, key, MODEL_SIGNALS, MAX_SIGNAL_TOKENS)
        structured = _extract_json(signals_text) or {"overall_bias": "NEUTRAL"}

        out = {
            "generated_at": ts,
            "cycle_duration_seconds": round(time.time() - t0, 2),
            "total_tokens_used": deep_tokens + signals_tokens,
            "token_breakdown": {
                "analysis_in": len(deep_prompt) // 4,      # rough
                "analysis_out": deep_tokens,
                "signals_in": len(signals_prompt) // 4,
                "signals_out": signals_tokens,
            },
            "market_state": market_state,
            "analysis_report": deep_text,
            "structured_signals": structured,
            "open_trades": [],
            "source": "minimax",
        }

    try:
        with open(INSIGHTS_PATH, "w") as f:
            json.dump(out, f, indent=2, default=str)
    except Exception as e:
        print(f"[minimax] insight write failed: {e}")
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts}  bias={out['structured_signals'].get('overall_bias','?')}  "
                    f"danger={out['structured_signals'].get('danger_level','?')}  "
                    f"tokens={out['total_tokens_used']}  "
                    f"source={out['source']}\n")
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="Keep running")
    ap.add_argument("--once", action="store_true", help="Run one pass and exit")
    ap.add_argument("--interval", type=int, default=600, help="Seconds between runs (default 600 = 10min)")
    args = ap.parse_args()

    out = run_once()
    sig = out.get("structured_signals", {}) or {}
    print(f"[{out['generated_at']}] bias={sig.get('overall_bias','?')}  "
          f"danger={sig.get('danger_level','?')}  "
          f"source={out['source']}  "
          f"tokens={out['total_tokens_used']}  "
          f"duration={out['cycle_duration_seconds']}s")

    if not args.loop or args.once:
        return
    try:
        while True:
            time.sleep(args.interval)
            out = run_once()
            sig = out.get("structured_signals", {}) or {}
            print(f"[{out['generated_at']}] bias={sig.get('overall_bias','?')}  "
                  f"tokens={out['total_tokens_used']}")
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()

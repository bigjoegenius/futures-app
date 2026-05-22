#!/usr/bin/env python3
"""
autopilot.py — AI strategy selector for the futures app.

Every hour (configurable):
  1. Build a market snapshot (all symbols, current strategy scores)
  2. Pipe it through the local `claude` CLI (Claude Code subscription) and
     let the AI decide:
       - which strategies should be enabled this cycle
       - which risk mode (conservative / moderate / aggressive)
  3. Apply the AI's choices back to the StrategyEngine.

Auth is the CLI's CLAUDE_CODE_OAUTH_TOKEN (subscription) — no metered API key.
If the CLI is unavailable, falls back to "enable top-scoring strategies".
All decisions are logged to the `autopilot_log` table so you can audit them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from futures_config import DB_PATH, FUTURES
from market_analyzer import STRATEGIES, get_market_overview
from strategy_engine import StrategyEngine, RISK_MODES, DEFAULT_STRATEGIES


CONTROLLABLE_STRATEGIES = list(STRATEGIES.keys())


class AutoPilot:
    def __init__(
        self,
        strategy_engine: StrategyEngine,
        interval_seconds: int = 3600,
        on_status_update: Optional[Callable[[dict], None]] = None,
    ):
        self.engine = strategy_engine
        self.interval = int(interval_seconds)
        self.on_status_update = on_status_update
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_decision: Optional[dict] = None
        self.ai_calls = 0
        self.model_name = "sonnet"

    # ── Lifecycle ──────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as e:
                print(f"[autopilot] loop error: {e}")
            # Sleep in 1-second chunks so stop() is responsive
            for _ in range(self.interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    # ── One decision cycle ─────────────────────────────────────────────
    def run_once(self) -> dict:
        overview = get_market_overview()
        trades_tail = self.engine.closed[-15:]
        decision = self._ask_ai(overview, trades_tail)
        self._apply(decision)
        self._log(decision)
        self.last_decision = decision
        if self.on_status_update:
            try:
                self.on_status_update(decision)
            except Exception:
                pass
        return decision

    # ── AI / fallback ──────────────────────────────────────────────────
    def _ask_ai(self, overview: dict, trades_tail: list) -> dict:
        claude_bin = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")
        if not os.path.exists(claude_bin):
            return self._local_decision(overview, reason=f"claude CLI not found at {claude_bin}")

        prompt = self._build_prompt(overview, trades_tail)
        # Strip ANTHROPIC_API_KEY so the CLI always uses the OAuth subscription
        # token and never falls back to metered API spend.
        cli_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        try:
            r = subprocess.run(
                [claude_bin, "-p", "--model", self.model_name, "--output-format", "text"],
                input=prompt, capture_output=True, text=True, timeout=300, env=cli_env,
            )
            if r.returncode != 0:
                return self._local_decision(overview, reason=f"claude CLI exit {r.returncode}: {(r.stderr or '')[:120]}")
            text = (r.stdout or "").strip()
            if not text:
                return self._local_decision(overview, reason="claude CLI returned empty output")
            self.ai_calls += 1
            return self._parse_decision(text, overview)
        except subprocess.TimeoutExpired:
            return self._local_decision(overview, reason="claude CLI timed out (>300s)")
        except Exception as e:
            return self._local_decision(overview, reason=f"claude CLI error: {e}")

    def _build_prompt(self, overview: dict, trades_tail: list) -> str:
        snap = {}
        for sym, info in overview.items():
            if "error" in info:
                continue
            # Keep only strategies with nonzero scores for context brevity
            top_strats = {
                sid: {"direction": s["direction"], "score": round(s["score"], 1), "note": s["note"]}
                for sid, s in info.get("strategies", {}).items()
                if s.get("score", 0) > 0
            }
            ind = info.get("indicators", {})
            snap[sym] = {
                "name": info.get("name"),
                "last": round(info.get("last", 0), 4),
                "change_24h_pct": round(info.get("change_24h_pct", 0), 2),
                "rsi": round(ind.get("rsi", 50), 1) if ind else None,
                "bb_pct": round((ind.get("close", 0) - ind.get("bb_lower", 0)) /
                                 max(ind.get("bb_upper", 1) - ind.get("bb_lower", 0), 1e-9), 2) if ind else None,
                "strategies": top_strats,
            }
        trade_rows = []
        for t in trades_tail:
            trade_rows.append({
                "symbol": t.symbol, "strategy": t.strategy, "direction": t.direction,
                "pnl": round(t.pnl_dollars, 2), "reason": t.exit_reason,
            })

        strategy_catalog = "\n".join(
            f"- {sid} ({STRATEGIES[sid].get('direction','BOTH')} on {','.join(STRATEGIES[sid].get('markets',['all']))[:60]}): "
            f"{STRATEGIES[sid]['description']}"
            for sid in CONTROLLABLE_STRATEGIES
        )

        session = self.engine.get_session_report()

        return f"""You are the autopilot AI for a US futures day-trading / swing paper account spanning 18 contracts
(indexes ES/NQ/YM/RTY · energy CL/NG · metals GC/SI/HG · bonds ZB/ZN · grains ZC/ZS/ZW · softs KC/SB/CT · cattle LE).

Available strategies ({len(CONTROLLABLE_STRATEGIES)} total — enable any subset):
{strategy_catalog}

Current session state:
- balance: ${session['balance']:.2f}  (started at ${session['starting_balance']:.2f})
- total pnl pct: {session['total_pnl_pct']:.2f}%
- trades: {session['trades']}  win rate: {session['win_rate']:.1f}%
- risk_mode: {session['risk_mode']}
- open positions: {session['open_positions']}

Market snapshot (only strategies with score > 0 shown per symbol):
{json.dumps(snap, indent=2)[:6500]}

Recent closed trades:
{json.dumps(trade_rows, indent=2)[:1500]}

Decide:
  1. strategies.<name>.enable: true/false, strategies.<name>.confidence: 0-100, strategies.<name>.reason: one sentence
  2. risk_mode: "conservative" | "moderate" | "aggressive"
  3. overall_confidence: 0-100
  4. summary: 1-2 sentence market outlook
  5. high_conviction_trade: if a single setup stands out, include it (symbol, strategy, direction, entry_price, stop_loss, take_profit, confidence_pct, reasoning)

Rules of thumb:
- Macro events (FOMC, WASDE, EIA): prefer to stay flat until resolution unless confidence is very high
- Index ORB strategies only make sense during US RTH (09:30-16:00 ET)
- Grain strategies favor Sep/Oct for seasonal_harvest, and the day of WASDE for wasde_react
- Bond strategies favor the 3 days after FOMC
- If a strategy has lost 3 straight in recent trades, disable it for 1 cycle

Return ONLY a JSON object, no markdown. Exact shape:
{{
  "strategies": {{
    "ema_cross": {{"enable": true, "confidence": 65, "reason": "..."}},
    ...
  }},
  "risk_mode": "moderate",
  "overall_confidence": 72,
  "summary": "...",
  "high_conviction_trade": {{
    "exists": false,
    "symbol": "",
    "strategy": "",
    "direction": "LONG",
    "entry_price": "",
    "stop_loss": "",
    "take_profit": "",
    "confidence_pct": 0,
    "reasoning": ""
  }}
}}

Fallback: if you don't want to use the full JSON shape, the legacy form also works:
{{"enabled": ["ema_cross", "macd_momentum"], "risk_mode": "moderate", "reasoning": "..."}}
"""

    def _parse_decision(self, text: str, overview: dict) -> dict:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return self._local_decision(overview, reason="AI returned non-JSON")
        try:
            raw = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return self._local_decision(overview, reason="AI JSON parse failed")

        # New format: strategies.<name>.enable + confidence
        enabled = []
        strategy_confidences = {}
        strats_obj = raw.get("strategies") or {}
        if isinstance(strats_obj, dict) and strats_obj:
            for sid, info in strats_obj.items():
                if sid not in CONTROLLABLE_STRATEGIES or not isinstance(info, dict):
                    continue
                if info.get("enable"):
                    enabled.append(sid)
                strategy_confidences[sid] = {
                    "confidence": int(info.get("confidence", 0) or 0),
                    "reason": str(info.get("reason", ""))[:300],
                    "enabled": bool(info.get("enable")),
                }
        else:
            # Legacy format: {"enabled": [...]}
            enabled = [s for s in raw.get("enabled", []) if s in CONTROLLABLE_STRATEGIES]

        risk = raw.get("risk_mode", "moderate")
        if risk not in RISK_MODES:
            risk = "moderate"
        if not enabled:
            enabled = list(DEFAULT_STRATEGIES)

        return {
            "enabled": enabled,
            "risk_mode": risk,
            "reasoning": (raw.get("reasoning") or raw.get("summary") or "")[:1000],
            "overall_confidence": int(raw.get("overall_confidence", 0) or 0),
            "summary": str(raw.get("summary", ""))[:500],
            "strategy_confidences": strategy_confidences,
            "high_conviction_trade": raw.get("high_conviction_trade") or {"exists": False},
            "source": "claude",
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    def _local_decision(self, overview: dict, reason: str) -> dict:
        # Enable strategies whose best score across symbols is high enough.
        best_per_strat: dict[str, float] = {}
        for snap in overview.values():
            if "error" in snap:
                continue
            for sid, info in snap.get("strategies", {}).items():
                best_per_strat[sid] = max(best_per_strat.get(sid, 0.0), info.get("score", 0.0))
        enabled = [sid for sid, score in best_per_strat.items() if score >= 50]
        if not enabled:
            enabled = list(DEFAULT_STRATEGIES)[:3]
        return {
            "enabled": enabled,
            "risk_mode": "moderate",
            "reasoning": f"Local fallback ({reason}). Enabled strategies with score>=50.",
            "source": "local",
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    # ── Apply + log ────────────────────────────────────────────────────
    def _apply(self, decision: dict) -> None:
        self.engine.set_risk(decision.get("risk_mode", "moderate"))
        enabled = set(decision.get("enabled", []))
        for sid in CONTROLLABLE_STRATEGIES:
            if sid in enabled:
                self.engine.enable(sid)
            else:
                self.engine.disable(sid)

    def _log(self, decision: dict) -> None:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                INSERT INTO autopilot_log (ts, risk_mode, enabled, reasoning, ai_model)
                VALUES (?,?,?,?,?)
            """, (
                decision.get("ts"),
                decision.get("risk_mode"),
                ",".join(decision.get("enabled", [])),
                decision.get("reasoning", ""),
                decision.get("source", ""),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[autopilot] log write failed: {e}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Autopilot (AI strategy selector)")
    ap.add_argument("--once", action="store_true", help="Run one decision and exit")
    ap.add_argument("--interval", type=int, default=3600)
    ap.add_argument("--risk", default="moderate", choices=list(RISK_MODES))
    args = ap.parse_args()

    eng = StrategyEngine(risk_mode=args.risk)
    ap_ = AutoPilot(eng, interval_seconds=args.interval)
    decision = ap_.run_once()
    print(json.dumps(decision, indent=2))
    if not args.once:
        print(f"\nLooping every {args.interval}s. Ctrl-C to stop.")
        ap_.start()
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            ap_.stop()


if __name__ == "__main__":
    main()

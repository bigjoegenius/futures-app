#!/usr/bin/env python3
"""
autopilot.py — AI strategy selector for the futures app.

Every hour (configurable):
  1. Build a market snapshot (all symbols, current strategy scores)
  2. Send it to Claude with trade history and let the AI decide:
       - which strategies should be enabled this cycle
       - which risk mode (conservative / moderate / aggressive)
  3. Apply the AI's choices back to the StrategyEngine.

If ANTHROPIC_API_KEY is missing, falls back to "enable top-scoring strategies".
All decisions are logged to the `autopilot_log` table so you can audit them.
"""

from __future__ import annotations

import json
import os
import sqlite3
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
        self.model_name = "claude-opus-4-6"

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
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return self._local_decision(overview, reason="no ANTHROPIC_API_KEY")

        try:
            import anthropic
        except ImportError:
            return self._local_decision(overview, reason="anthropic SDK missing")

        client = anthropic.Anthropic(api_key=api_key)
        prompt = self._build_prompt(overview, trades_tail)
        try:
            msg = client.messages.create(
                model=self.model_name,
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            self.ai_calls += 1
            text = "".join(getattr(b, "text", "") for b in msg.content).strip()
            return self._parse_decision(text, overview)
        except Exception as e:
            return self._local_decision(overview, reason=f"Claude error: {e}")

    def _build_prompt(self, overview: dict, trades_tail: list) -> str:
        snap = {}
        for sym, info in overview.items():
            if "error" in info:
                continue
            snap[sym] = {
                "name": info.get("name"),
                "last": info.get("last"),
                "change_24h_pct": info.get("change_24h_pct"),
                "strategies": {
                    sid: {"direction": s["direction"], "score": s["score"], "note": s["note"]}
                    for sid, s in info.get("strategies", {}).items()
                },
            }
        trade_rows = []
        for t in trades_tail:
            trade_rows.append({
                "symbol": t.symbol, "strategy": t.strategy, "direction": t.direction,
                "pnl": round(t.pnl_dollars, 2), "reason": t.exit_reason,
            })

        strategy_catalog = "\n".join(
            f"- {sid}: {STRATEGIES[sid]['description']}" for sid in CONTROLLABLE_STRATEGIES
        )

        session = self.engine.get_session_report()

        return f"""You are the autopilot for a US futures day-trading / swing paper account.

Available strategies (you can enable any subset):
{strategy_catalog}

Current session state:
- balance: ${session['balance']:.2f}  (started at ${session['starting_balance']:.2f})
- total pnl pct: {session['total_pnl_pct']:.2f}%
- trades: {session['trades']}  win rate: {session['win_rate']:.1f}%
- risk_mode: {session['risk_mode']}

Market snapshot (score 0-100 for each strategy per contract):
{json.dumps(snap, indent=2)[:5000]}

Recent closed trades:
{json.dumps(trade_rows, indent=2)[:1500]}

Choose:
  1. Which of the {len(CONTROLLABLE_STRATEGIES)} strategies to enable for the next cycle.
  2. A risk_mode out of ["conservative", "moderate", "aggressive"].
  3. A brief (<120 word) reasoning explaining why.

Return ONLY a JSON object, no markdown, exactly this shape:
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

        enabled = [s for s in raw.get("enabled", []) if s in CONTROLLABLE_STRATEGIES]
        risk = raw.get("risk_mode", "moderate")
        if risk not in RISK_MODES:
            risk = "moderate"
        if not enabled:
            enabled = list(DEFAULT_STRATEGIES)
        return {
            "enabled": enabled,
            "risk_mode": risk,
            "reasoning": raw.get("reasoning", "")[:1000],
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

#!/usr/bin/env python3
"""
minimax_insights.py — Optional 10-minute AI pass via MiniMax (cheaper than Claude).

This is a stub implementation mirroring crypto-app's file layout. If the
MINIMAX_API_KEY env var is set it will make real calls; otherwise it just
writes a minimal valid insights file so run_autopilot.py / web_controller.py
can keep reading from it without special-casing.

Output: minimax_insights.json (one object, updated in place)
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
from market_analyzer import get_market_overview


INSIGHTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minimax_insights.json")
LOG_PATH      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minimax_insights_log.txt")
API_BASE      = "https://api.minimax.io/v1/text/chatcompletion_v2"


def _build_prompt(overview: dict) -> str:
    compact = []
    for sym, snap in overview.items():
        if "error" in snap:
            continue
        best = max(snap.get("strategies", {}).items(),
                   key=lambda kv: kv[1].get("score", 0), default=(None, None))
        if not best[0]:
            continue
        compact.append(f"{sym} last={snap.get('last', 0):.2f} "
                       f"24h={snap.get('change_24h_pct', 0):+.2f}% "
                       f"best={best[0]} ({best[1]['direction']}, {best[1]['score']:.0f})")
    return (
        "You are a short-form futures market analyst. Return JSON with keys "
        "'market_regime' (risk-on|risk-off|mixed), 'watchlist' (array of 3 contract symbols), "
        "and 'notes' (one short paragraph). Current snapshot:\n\n"
        + "\n".join(compact[:20])
    )


def _call_minimax(prompt: str, api_key: str, model: str = "MiniMax-Text-01") -> str | None:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 600,
    }).encode()
    req = urllib.request.Request(
        API_BASE,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        return f"[minimax http {e.code}: {e.read().decode(errors='ignore')[:200]}]"
    except Exception as e:
        return f"[minimax error: {e}]"

    try:
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"]
    except Exception:
        return raw[:2000]


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return {"notes": text[:500]}
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return {"notes": text[start : end + 1][:500]}


def run_once() -> dict:
    overview = get_market_overview()
    ts = datetime.now(timezone.utc).isoformat()
    key = os.environ.get("MINIMAX_API_KEY", "").strip()

    if not key:
        # Offline stub — derive a tiny local insight so downstream readers don't break
        regime_votes = 0
        for snap in overview.values():
            if "error" in snap:
                continue
            regime_votes += 1 if snap.get("change_24h_pct", 0) > 0 else -1
        regime = "risk-on" if regime_votes > 1 else "risk-off" if regime_votes < -1 else "mixed"
        watchlist = sorted(
            [(k, max((s["score"] for s in v.get("strategies", {}).values()), default=0))
             for k, v in overview.items() if "error" not in v],
            key=lambda kv: kv[1], reverse=True,
        )[:3]
        out = {
            "ts": ts,
            "source": "stub",
            "market_regime": regime,
            "watchlist": [w[0] for w in watchlist],
            "notes": f"Local fallback — no MINIMAX_API_KEY. Regime: {regime}. "
                     f"Top setups by best score: {[w[0] for w in watchlist]}.",
        }
    else:
        prompt = _build_prompt(overview)
        raw = _call_minimax(prompt, key)
        parsed = _extract_json(raw or "")
        out = {"ts": ts, "source": "minimax", **parsed}

    try:
        with open(INSIGHTS_PATH, "w") as f:
            json.dump(out, f, indent=2)
    except Exception as e:
        print(f"[minimax] write failed: {e}")
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts}  {out.get('market_regime','?')}  {out.get('notes','')[:200]}\n")
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="Keep running")
    ap.add_argument("--interval", type=int, default=600, help="Seconds (default 600)")
    args = ap.parse_args()

    out = run_once()
    print(json.dumps(out, indent=2))
    if not args.loop:
        return
    try:
        while True:
            time.sleep(args.interval)
            out = run_once()
            print(f"[{out['ts']}] regime={out.get('market_regime')}  source={out.get('source')}")
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()

"""
live_prices.py — Poll Schwab API every ~30s and write live quotes to the database.

Why this exists:
    Schwab gives us a real-time HTTP quote endpoint (formerly we polled
    yfinance, which was 10-15 minutes delayed for most futures). The web
    dashboard and the server controller both read from the `latest_prices`
    table, so all of their live data comes from this one poller.

    Stock/ETF quotes use the bare ticker (e.g. "SPY"). Futures use Schwab's
    continuous-future symbol like "/ES"; Schwab resolves to the front month
    automatically and returns it as e.g. "/ESM26". We store everything under
    the original yfinance-style symbol ("ES=F") so downstream readers don't
    care which broker produced the snapshot.

Auth:
    Reads SCHWAB_CLIENT_ID, SCHWAB_CLIENT_SECRET, SCHWAB_TOKEN_PATH from env
    (.env file or systemd EnvironmentFile). Token auto-refreshes via schwab-py
    as long as the refresh token is < 7 days old.

Usage:
    python live_prices.py              # run forever, poll every 30s
    python live_prices.py --once       # fetch one snapshot and exit
    python live_prices.py --interval 60  # custom interval in seconds
    python live_prices.py --symbols ES=F,NQ=F   # subset of contracts
"""

import argparse
import os
import sqlite3
import time
from datetime import datetime, timezone

import schwab
from dotenv import load_dotenv

from futures_config import DB_PATH, FUTURES


DEFAULT_INTERVAL = 30   # seconds between polls

load_dotenv()

# yfinance contract symbol → Schwab continuous-future root.
# Schwab auto-resolves the front month (e.g. "/ES" → "/ESM26" on response).
# KC/SB/CT (Coffee/Sugar/Cotton, ICE-listed) aren't quoted by Schwab — those
# fall back to yfinance via ICE_SOFTS_FALLBACK below.
YF_TO_SCHWAB_FUTURES = {
    "ES=F": "/ES", "NQ=F": "/NQ", "YM=F": "/YM", "RTY=F": "/RTY",
    "CL=F": "/CL", "NG=F": "/NG",
    "GC=F": "/GC", "SI=F": "/SI", "HG=F": "/HG",
    "ZB=F": "/ZB", "ZN=F": "/ZN",
    "ZC=F": "/ZC", "ZS=F": "/ZS", "ZW=F": "/ZW",
    "LE=F": "/LE",
}

# Schwab doesn't quote these ICE-listed soft commodities. yfinance only.
ICE_SOFTS_FALLBACK = {"KC=F", "SB=F", "CT=F"}

_schwab_client = None


def get_schwab_client():
    """Lazy-init the Schwab API client. Returns None if creds/token missing."""
    global _schwab_client
    if _schwab_client is not None:
        return _schwab_client
    api_key = os.environ.get("SCHWAB_CLIENT_ID")
    app_secret = os.environ.get("SCHWAB_CLIENT_SECRET")
    token_path = os.environ.get(
        "SCHWAB_TOKEN_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "schwab_token.json"),
    )
    if not api_key or not app_secret:
        print("[live_prices] SCHWAB_CLIENT_ID/SCHWAB_CLIENT_SECRET not set")
        return None
    if not os.path.exists(token_path):
        print(f"[live_prices] schwab token missing at {token_path}")
        return None
    try:
        _schwab_client = schwab.auth.client_from_token_file(
            token_path, api_key, app_secret)
        return _schwab_client
    except Exception as e:
        print(f"[live_prices] schwab client init failed: {e}")
        return None


def _schwab_symbol(yf_symbol: str) -> str:
    """yfinance-format → Schwab quote symbol. Pass-through for equities/ETFs."""
    return YF_TO_SCHWAB_FUTURES.get(yf_symbol, yf_symbol)


def _safe_float(v):
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    f = _safe_float(v)
    if f is None:
        return 0
    return int(f)


def fetch_batch(symbols: list[str]) -> dict[str, dict]:
    """Fetch quotes for all symbols in one Schwab request.

    Returns {yf_symbol: snapshot_dict}. Symbols that can't be priced are
    omitted from the result (caller decides whether to retry / warn).
    """
    client = get_schwab_client()
    if client is None or not symbols:
        return {}

    schwab_to_yf = {_schwab_symbol(s): s for s in symbols}
    schwab_syms = list(schwab_to_yf.keys())

    try:
        resp = client.get_quotes(schwab_syms)
    except Exception as e:
        print(f"  schwab get_quotes failed: {e}")
        return {}
    if resp.status_code != 200:
        print(f"  schwab status {resp.status_code}: {resp.text[:200]}")
        return {}

    try:
        data = resp.json()
    except Exception as e:
        print(f"  schwab json parse failed: {e}")
        return {}

    out: dict[str, dict] = {}
    for resolved_sym, info in (data or {}).items():
        # Schwab returns the resolved front-month for futures (e.g. /ESM26 for /ES).
        # Map back to the requested root by matching prefix.
        yf_sym = schwab_to_yf.get(resolved_sym)
        if yf_sym is None:
            for s_sym, y_sym in schwab_to_yf.items():
                if resolved_sym.startswith(s_sym):
                    yf_sym = y_sym
                    break
        if yf_sym is None:
            continue

        q = info.get("quote", {}) or {}
        last = _safe_float(q.get("lastPrice") or q.get("mark"))
        if last is None or last <= 0:
            continue
        out[yf_sym] = {
            "symbol":     yf_sym,
            "last":       last,
            "day_open":   _safe_float(q.get("openPrice")),
            "day_high":   _safe_float(q.get("highPrice")),
            "day_low":    _safe_float(q.get("lowPrice")),
            "prev_close": _safe_float(q.get("closePrice")),
            "volume":     _safe_int(q.get("totalVolume")),
        }
    return out


def write_snapshot(conn: sqlite3.Connection, snap: dict) -> None:
    """Upsert a single snapshot into the latest_prices table."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO latest_prices (symbol, last, day_open, day_high, day_low,
                                   prev_close, volume, updated_at)
        VALUES (:symbol, :last, :day_open, :day_high, :day_low,
                :prev_close, :volume, :updated_at)
        ON CONFLICT(symbol) DO UPDATE SET
            last = excluded.last,
            day_open = excluded.day_open,
            day_high = excluded.day_high,
            day_low = excluded.day_low,
            prev_close = excluded.prev_close,
            volume = excluded.volume,
            updated_at = excluded.updated_at
    """, {**snap, "updated_at": now})


def fetch_yfinance_one(symbol: str) -> dict | None:
    """Fallback for ICE softs (KC/SB/CT) that Schwab doesn't quote.
    Imported lazily so production deployments without yfinance don't crash."""
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        t = yf.Ticker(symbol)
        fi = getattr(t, "fast_info", None) or {}

        def _pick(*keys):
            for k in keys:
                try:
                    v = fi[k] if isinstance(fi, dict) else getattr(fi, k, None)
                except Exception:
                    v = None
                if v is not None:
                    return v
            return None

        last = _pick("last_price", "lastPrice", "regularMarketPrice")
        day_open = _pick("day_open", "open", "regularMarketOpen")
        day_high = _pick("day_high", "dayHigh", "regularMarketDayHigh")
        day_low = _pick("day_low", "dayLow", "regularMarketDayLow")
        prev_close = _pick("previous_close", "previousClose",
                           "regularMarketPreviousClose")
        volume = _pick("last_volume", "regularMarketVolume", "volume") or 0

        if last is None:
            hist = t.history(period="1d", interval="1m")
            if hist.empty:
                return None
            last = float(hist["Close"].iloc[-1])
            day_open = float(hist["Open"].iloc[0])
            day_high = float(hist["High"].max())
            day_low = float(hist["Low"].min())
            volume = int(hist["Volume"].sum())
            prev_close = prev_close or last

        return {
            "symbol":     symbol,
            "last":       _safe_float(last),
            "day_open":   _safe_float(day_open),
            "day_high":   _safe_float(day_high),
            "day_low":    _safe_float(day_low),
            "prev_close": _safe_float(prev_close),
            "volume":     _safe_int(volume),
        }
    except Exception as e:
        print(f"  {symbol}: yfinance fallback failed - {e}")
        return None


def poll_once(symbols: list[str], conn: sqlite3.Connection,
              verbose: bool = True) -> int:
    """Fetch a snapshot for each symbol via Schwab (with yfinance fallback
    for ICE softs). Returns count of successes."""
    schwab_syms = [s for s in symbols if s not in ICE_SOFTS_FALLBACK]
    yf_syms = [s for s in symbols if s in ICE_SOFTS_FALLBACK]

    snaps: dict[str, dict] = fetch_batch(schwab_syms) if schwab_syms else {}
    for s in yf_syms:
        snap = fetch_yfinance_one(s)
        if snap and snap["last"] is not None:
            snaps[s] = snap

    ok = 0
    for sym in symbols:
        snap = snaps.get(sym)
        if snap and snap["last"] is not None:
            write_snapshot(conn, snap)
            ok += 1
            if verbose:
                chg = ""
                if snap["prev_close"]:
                    pct = (snap["last"] - snap["prev_close"]) / snap["prev_close"] * 100
                    chg = f" ({pct:+.2f}%)"
                src = "yf" if sym in ICE_SOFTS_FALLBACK else "sw"
                print(f"  {sym:<8} {snap['last']:>12,.4f}{chg}  [{src}]")
        elif verbose:
            print(f"  {sym:<8} (no quote)")
    conn.commit()
    return ok


def main():
    ap = argparse.ArgumentParser(description="Live futures price poller (Schwab)")
    ap.add_argument("--once", action="store_true", help="Fetch one snapshot and exit")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"Seconds between polls (default {DEFAULT_INTERVAL})")
    ap.add_argument("--symbols", type=str, default=None,
                    help="Comma-separated subset of symbols")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else list(FUTURES.keys())
    unknown = [s for s in symbols if s not in FUTURES]
    if unknown:
        print(f"Unknown symbols: {unknown}")
        return

    conn = sqlite3.connect(DB_PATH)
    # Schema lives in db_setup.py — make sure it's there.
    from db_setup import create_database
    conn.close()
    create_database()
    conn = sqlite3.connect(DB_PATH)

    print(f"Polling {len(symbols)} contracts every {args.interval}s via Schwab")
    print("-" * 55)

    try:
        while True:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] fetching...")
            ok = poll_once(symbols, conn, verbose=not args.quiet)
            print(f"[{ts}] {ok}/{len(symbols)} updated")
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

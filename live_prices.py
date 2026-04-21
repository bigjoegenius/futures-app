"""
live_prices.py — Poll yfinance every ~30s and write live quotes to the database.

Why this exists:
    yfinance doesn't give us a real WebSocket, so the "live" feed is just
    polled every N seconds. Prices are 10-15 minutes delayed for most futures
    (that's a Yahoo Finance limitation, not ours). The web dashboard and the
    server controller both read from the `latest_prices` table, so all of
    their live data comes from this one poller.

Usage:
    python live_prices.py              # run forever, poll every 30s
    python live_prices.py --once       # fetch one snapshot and exit
    python live_prices.py --interval 60  # custom interval in seconds
    python live_prices.py --symbols ES=F,NQ=F   # subset of contracts
"""

import argparse
import sqlite3
import time
from datetime import datetime, timezone

import yfinance as yf

from futures_config import DB_PATH, FUTURES


DEFAULT_INTERVAL = 30   # seconds between polls


def fetch_snapshot(symbol: str) -> dict | None:
    """Fetch the latest quote for one symbol. Returns None on failure."""
    try:
        t = yf.Ticker(symbol)
        # fast_info is the quickest path; fall back to info if needed
        fi = getattr(t, "fast_info", None) or {}
        last = _pick(fi, "last_price", "lastPrice", "regularMarketPrice")
        day_open = _pick(fi, "day_open", "open", "regularMarketOpen")
        day_high = _pick(fi, "day_high", "dayHigh", "regularMarketDayHigh")
        day_low = _pick(fi, "day_low", "dayLow", "regularMarketDayLow")
        prev_close = _pick(fi, "previous_close", "previousClose", "regularMarketPreviousClose")
        volume = _pick(fi, "last_volume", "regularMarketVolume", "volume") or 0

        if last is None:
            # Last resort: pull the most recent 1-minute bar
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
            "last":       float(last) if last is not None else None,
            "day_open":   float(day_open) if day_open is not None else None,
            "day_high":   float(day_high) if day_high is not None else None,
            "day_low":    float(day_low) if day_low is not None else None,
            "prev_close": float(prev_close) if prev_close is not None else None,
            "volume":     int(volume) if volume is not None else 0,
        }
    except Exception as e:
        print(f"  {symbol}: fetch error - {e}")
        return None


def _pick(obj, *keys):
    """Return the first non-None value for any of the given keys on obj."""
    for k in keys:
        try:
            v = obj[k] if isinstance(obj, dict) else getattr(obj, k, None)
        except Exception:
            v = None
        if v is not None:
            return v
    return None


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


def poll_once(symbols: list[str], conn: sqlite3.Connection, verbose: bool = True) -> int:
    """Fetch a snapshot for each symbol. Returns count of successes."""
    ok = 0
    for sym in symbols:
        snap = fetch_snapshot(sym)
        if snap and snap["last"] is not None:
            write_snapshot(conn, snap)
            ok += 1
            if verbose:
                chg = ""
                if snap["prev_close"]:
                    pct = (snap["last"] - snap["prev_close"]) / snap["prev_close"] * 100
                    chg = f" ({pct:+.2f}%)"
                print(f"  {sym:<8} {snap['last']:>12,.4f}{chg}")
    conn.commit()
    return ok


def main():
    ap = argparse.ArgumentParser(description="Live futures price poller")
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

    print(f"Polling {len(symbols)} contracts every {args.interval}s")
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

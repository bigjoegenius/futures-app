"""
fetch_data.py — Downloads futures data from yfinance and stores it in SQLite.

Usage:
    python fetch_data.py              # Download daily data for all contracts
    python fetch_data.py --all        # Download ALL timeframes for all contracts
    python fetch_data.py --symbol ES=F  # Download just one contract
    python fetch_data.py --timeframe 1d # Download just one timeframe

How it works:
    1. Connects to yfinance and downloads OHLCV candle data
    2. Opens the SQLite database
    3. Inserts new candles (skips duplicates automatically)
    4. Logs the fetch so we know when data was last updated
"""

import argparse
import sqlite3
from datetime import datetime

import yfinance as yf

from futures_config import DB_PATH, FUTURES, TIMEFRAMES
from db_setup import create_database


def fetch_and_store(symbol, name, timeframe, period, conn):
    """
    Download candle data for one contract + timeframe and save to database.

    Args:
        symbol:    yfinance ticker like "ES=F"
        name:      human name like "E-mini S&P 500"
        timeframe: interval like "1d" or "5m"
        period:    how far back to look like "max" or "7d"
        conn:      SQLite connection
    """
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period=period, interval=timeframe)

        if data.empty:
            print(f"  {symbol} ({timeframe}): no data returned")
            return 0

        cursor = conn.cursor()
        rows_added = 0

        for timestamp, row in data.iterrows():
            # Convert timestamp to a clean string
            dt_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")

            try:
                cursor.execute("""
                    INSERT INTO candles (symbol, timeframe, datetime, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    symbol,
                    timeframe,
                    dt_str,
                    round(float(row["Open"]), 4),
                    round(float(row["High"]), 4),
                    round(float(row["Low"]), 4),
                    round(float(row["Close"]), 4),
                    int(row["Volume"]),
                ))
                rows_added += 1
            except sqlite3.IntegrityError:
                # Duplicate row — already have this candle, skip it
                pass

        # Update fetch log
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO fetch_log (symbol, timeframe, fetched_at, rows_added)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe)
            DO UPDATE SET fetched_at = ?, rows_added = ?
        """, (symbol, timeframe, now, rows_added, now, rows_added))

        conn.commit()
        print(f"  {symbol:<10} {name:<28} {timeframe:<5} -> {rows_added} new candles")
        return rows_added

    except Exception as e:
        print(f"  {symbol} ({timeframe}): ERROR - {e}")
        return 0


def main():
    parser = argparse.ArgumentParser(description="Download futures data from yfinance")
    parser.add_argument("--all", action="store_true",
                        help="Download ALL timeframes (not just daily)")
    parser.add_argument("--symbol", type=str, default=None,
                        help="Only fetch one symbol (e.g. ES=F)")
    parser.add_argument("--timeframe", type=str, default=None,
                        help="Only fetch one timeframe (e.g. 1d, 5m)")
    args = parser.parse_args()

    # Make sure the database exists
    create_database()

    # Decide which symbols to fetch
    if args.symbol:
        if args.symbol not in FUTURES:
            print(f"Unknown symbol: {args.symbol}")
            print(f"Available: {', '.join(FUTURES.keys())}")
            return
        symbols = {args.symbol: FUTURES[args.symbol]}
    else:
        symbols = FUTURES

    # Decide which timeframes to fetch
    if args.timeframe:
        if args.timeframe not in TIMEFRAMES:
            print(f"Unknown timeframe: {args.timeframe}")
            print(f"Available: {', '.join(TIMEFRAMES.keys())}")
            return
        timeframes = {args.timeframe: TIMEFRAMES[args.timeframe]}
    elif args.all:
        timeframes = TIMEFRAMES
    else:
        # Default: just daily data
        timeframes = {"1d": TIMEFRAMES["1d"]}

    # Connect to database
    conn = sqlite3.connect(DB_PATH)
    total_new = 0

    print()
    print("=" * 65)
    print("  FUTURES DATA DOWNLOADER")
    print(f"  Contracts: {len(symbols)}  |  Timeframes: {len(timeframes)}")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)
    print()

    for tf_code, tf_info in timeframes.items():
        period = tf_info["max_period"]
        print(f"--- {tf_info['label']} ({tf_code}) — looking back {period} ---")

        for symbol, name in symbols.items():
            count = fetch_and_store(symbol, name, tf_code, period, conn)
            total_new += count

        print()

    conn.close()

    print("=" * 65)
    print(f"  DONE — {total_new} total new candles saved to {DB_PATH}")
    print("=" * 65)


if __name__ == "__main__":
    main()

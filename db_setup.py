"""
db_setup.py — Creates the SQLite database and tables for futures data.

Tables:
  - candles:        OHLCV data for every contract and timeframe
  - fetch_log:      tracks when we last fetched data (so we don't re-download)
  - latest_prices:  live polled price snapshots (written by live_prices.py)
  - trades:         closed paper trades (also mirrored to trade_log.json)
  - autopilot_log:  AI decision log (strategies enabled, risk mode, etc.)
"""

import sqlite3
from futures_config import DB_PATH


def create_database():
    """Create the SQLite database and tables if they don't exist."""

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Main candle data table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT    NOT NULL,
            timeframe   TEXT    NOT NULL,
            datetime    TEXT    NOT NULL,
            open        REAL    NOT NULL,
            high        REAL    NOT NULL,
            low         REAL    NOT NULL,
            close       REAL    NOT NULL,
            volume      INTEGER NOT NULL,
            UNIQUE(symbol, timeframe, datetime)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_candles_lookup
        ON candles (symbol, timeframe, datetime)
    """)

    # Fetch log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT    NOT NULL,
            timeframe   TEXT    NOT NULL,
            fetched_at  TEXT    NOT NULL,
            rows_added  INTEGER NOT NULL,
            UNIQUE(symbol, timeframe)
        )
    """)

    # Live polled prices (updated every ~30s by live_prices.py)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS latest_prices (
            symbol        TEXT PRIMARY KEY,
            last          REAL,
            day_open      REAL,
            day_high      REAL,
            day_low       REAL,
            prev_close    REAL,
            volume        INTEGER,
            updated_at    TEXT NOT NULL
        )
    """)

    # Closed paper trades
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol             TEXT NOT NULL,
            strategy           TEXT NOT NULL,
            direction          TEXT NOT NULL,       -- "long" or "short"
            entry_time         TEXT NOT NULL,
            entry_price        REAL NOT NULL,
            exit_time          TEXT,
            exit_price         REAL,
            stop_price         REAL,
            target_price       REAL,
            contracts          REAL NOT NULL,
            pnl_dollars        REAL,
            pnl_pct            REAL,
            fees               REAL,
            exit_reason        TEXT,
            status             TEXT NOT NULL,       -- "open" or "closed"
            confidence         REAL,                -- 0-100 probability this trade wins
            confidence_source  TEXT                 -- which signals went into the blend
        )
    """)

    # Older deploys may have a pre-confidence schema; add the columns if missing.
    for col, col_type in (("confidence", "REAL"), ("confidence_source", "TEXT")):
        try:
            cursor.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_trades_status
        ON trades (status, symbol)
    """)

    # Autopilot decision log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS autopilot_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL,
            risk_mode    TEXT,
            enabled      TEXT,            -- comma-separated list
            reasoning    TEXT,
            ai_model     TEXT
        )
    """)

    conn.commit()
    conn.close()
    print(f"Database ready at: {DB_PATH}")


if __name__ == "__main__":
    create_database()

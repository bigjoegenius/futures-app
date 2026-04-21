"""
futures_config.py — Configuration for the futures trading app.

This file defines which futures contracts we track,
what timeframes we use, and database settings.
"""

import os

# ---------- Database ----------
# SQLite database file — stored in the project folder
DB_PATH = os.path.join(os.path.dirname(__file__), "futures.db")

# ---------- Futures Contracts ----------
# Each entry: ticker symbol used by yfinance -> human-readable name
# The "=F" suffix tells yfinance it's a futures front-month contract

FUTURES = {
    # Stock Indexes
    "ES=F": "E-mini S&P 500",
    "NQ=F": "E-mini Nasdaq 100",
    "YM=F": "E-mini Dow Jones",
    "RTY=F": "E-mini Russell 2000",

    # Energy
    "CL=F": "Crude Oil (WTI)",
    "NG=F": "Natural Gas",

    # Metals
    "GC=F": "Gold",
    "SI=F": "Silver",
    "HG=F": "Copper",

    # Bonds
    "ZB=F": "US Treasury Bond (30yr)",
    "ZN=F": "10-Year T-Note",

    # Agriculture
    "ZC=F": "Corn",
    "ZS=F": "Soybeans",
    "ZW=F": "Wheat",
    "KC=F": "Coffee",
    "SB=F": "Sugar",
    "CT=F": "Cotton",
    "LE=F": "Live Cattle",
}

# ---------- Timeframes ----------
# yfinance interval codes and how far back each can go
# Minute-level data only goes back ~7 days on yfinance
# Daily data can go back to "max" (decades for some contracts)

TIMEFRAMES = {
    "1m":  {"label": "1 Minute",  "max_period": "7d"},
    "5m":  {"label": "5 Minute",  "max_period": "60d"},
    "15m": {"label": "15 Minute", "max_period": "60d"},
    "1h":  {"label": "1 Hour",    "max_period": "730d"},
    "1d":  {"label": "Daily",     "max_period": "max"},
}

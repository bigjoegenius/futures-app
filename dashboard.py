"""
dashboard.py — Futures Trading Dashboard

A dark-themed desktop trading dashboard built with CustomTkinter + matplotlib.
Displays candlestick charts, volume, and technical indicators (BB, RSI, MACD)
for 18 futures contracts across multiple timeframes.

Usage:
    python dashboard.py

Controls:
    - Click a contract button to switch markets
    - Click a timeframe button to change chart interval
    - Scroll to pan left/right through history
    - Ctrl+Scroll (or Cmd+Scroll) to zoom in/out
    - Click and drag to pan
    - Mouse hover shows crosshair with OHLCV data
    - Toggle indicators in the right sidebar
"""

import sqlite3
import queue
import time
import threading
from datetime import datetime

import numpy as np
import pandas as pd
import customtkinter as ctk
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib import gridspec
import matplotlib.ticker as mticker
import matplotlib.dates as mdates

from futures_config import DB_PATH, FUTURES, TIMEFRAMES

# ──────────────────────────────────────────────
#  COLOR SCHEME  (matching crypto-app dark theme)
# ──────────────────────────────────────────────
C_BG       = "#0d0d0d"      # main background
C_PANEL    = "#111122"      # panels / sidebar
C_GRID     = "#1a1a1a"      # chart grid lines
C_TEXT     = "#aaaaaa"      # default text
C_UP       = "#00e676"      # bullish candles (green)
C_DOWN     = "#ff1744"      # bearish candles (red)
C_CROSS    = "#ffffff"      # crosshair
ACT_CLR    = "#1f6aa5"      # active button
INACT_CLR  = "gray25"       # inactive button

# Indicator colors
C_BB_MID   = "#CE93D8"      # Bollinger mid line
C_BB_BAND  = "#9C27B0"      # Bollinger upper/lower
C_RSI      = "#FF9800"      # RSI line
C_MACD     = "#2196F3"      # MACD line
C_SIGNAL   = "#FF9800"      # MACD signal line

# Font
FONT       = "Arial"

# ──────────────────────────────────────────────
#  CONTRACT GROUPS — organized by category
# ──────────────────────────────────────────────
CONTRACT_GROUPS = {
    "Indexes": ["ES=F", "NQ=F", "YM=F", "RTY=F"],
    "Energy":  ["CL=F", "NG=F"],
    "Metals":  ["GC=F", "SI=F", "HG=F"],
    "Bonds":   ["ZB=F", "ZN=F"],
    "Ags":     ["ZC=F", "ZS=F", "ZW=F", "KC=F", "SB=F", "CT=F", "LE=F"],
}

# Short display names for buttons
SHORT_NAMES = {
    "ES=F": "ES", "NQ=F": "NQ", "YM=F": "YM", "RTY=F": "RTY",
    "CL=F": "CL", "NG=F": "NG", "GC=F": "GC", "SI=F": "SI",
    "HG=F": "HG", "ZB=F": "ZB", "ZN=F": "ZN", "ZC=F": "ZC",
    "ZS=F": "ZS", "ZW=F": "ZW", "KC=F": "KC", "SB=F": "SB",
    "CT=F": "CT", "LE=F": "LE",
}


# ──────────────────────────────────────────────
#  TECHNICAL INDICATOR CALCULATIONS
# ──────────────────────────────────────────────

def calc_bollinger(df, period=20, std_dev=2):
    """Calculate Bollinger Bands."""
    mid = df["close"].rolling(window=period).mean()
    std = df["close"].rolling(window=period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return mid, upper, lower


def calc_rsi(df, period=14):
    """Calculate Relative Strength Index."""
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_macd(df, fast=12, slow=26, signal=9):
    """Calculate MACD, Signal line, and Histogram."""
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


# ──────────────────────────────────────────────
#  MAIN DASHBOARD CLASS
# ──────────────────────────────────────────────

class Dashboard(ctk.CTk):
    def __init__(self):
        super().__init__()

        # ── Window setup ──
        self.title("Futures Trading Dashboard")
        self.geometry("1400x850")
        self.configure(fg_color=C_BG)
        ctk.set_appearance_mode("dark")

        # ── State ──
        self.current_symbol = "ES=F"
        self.current_tf = "1d"
        self.df_full = pd.DataFrame()       # all loaded data
        self.view_size = 150                 # candles visible
        self.view_end = 0                    # rightmost candle index
        self.show_bb = False
        self.show_rsi = False
        self.show_macd = False

        # Buffer system — draw 4x visible candles for smooth panning
        self._buf_start = 0
        self._buf_n = 0
        self._np_highs = np.array([])
        self._np_lows = np.array([])

        # Draw throttle — 60fps cap for smooth scrolling
        self._last_draw_time = 0

        # Drag state
        self._drag_start_x = None
        self._drag_start_view_end = None

        # Crosshair artists
        self._cross_v = None
        self._cross_h = None
        self._price_label = None
        self._time_label = None
        self._info_text = None

        # Chart axes references
        self.ax_candles = None
        self.ax_volume = None
        self.ax_rsi = None
        self.ax_macd = None

        # Button references for highlighting
        self._symbol_buttons = {}
        self._tf_buttons = {}

        # ── Build UI ──
        self._build_top_bar()
        self._build_main_area()
        self._build_sidebar()
        self._build_status_bar()

        # ── Load initial data ──
        self._load_data()
        self._build_chart_figure()
        self._draw_chart()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  UI BUILDING
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_top_bar(self):
        """Build the top bar with contract buttons, timeframes, and price info."""
        top = ctk.CTkFrame(self, fg_color=C_PANEL, height=110, corner_radius=0)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)

        # ── Row 1: Title + contract group buttons ──
        row1 = ctk.CTkFrame(top, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=(8, 2))

        ctk.CTkLabel(
            row1, text="Futures Dashboard",
            font=(FONT, 17, "bold"), text_color="#ffffff"
        ).pack(side="left", padx=(5, 20))

        # Contract buttons grouped by category
        for group_name, symbols in CONTRACT_GROUPS.items():
            # Group label
            ctk.CTkLabel(
                row1, text=f"{group_name}:",
                font=(FONT, 9), text_color="#666666"
            ).pack(side="left", padx=(10, 2))

            for sym in symbols:
                short = SHORT_NAMES[sym]
                btn = ctk.CTkButton(
                    row1, text=short, width=42, height=26,
                    font=(FONT, 10, "bold"), corner_radius=4,
                    fg_color=ACT_CLR if sym == self.current_symbol else INACT_CLR,
                    hover_color="#2a7abf",
                    command=lambda s=sym: self._switch_symbol(s)
                )
                btn.pack(side="left", padx=1)
                self._symbol_buttons[sym] = btn

        # ── Row 2: Timeframe buttons + price info ──
        row2 = ctk.CTkFrame(top, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=(4, 2))

        ctk.CTkLabel(
            row2, text="Timeframe:",
            font=(FONT, 10), text_color="#666666"
        ).pack(side="left", padx=(5, 5))

        for tf_code, tf_info in TIMEFRAMES.items():
            btn = ctk.CTkButton(
                row2, text=tf_info["label"], width=70, height=26,
                font=(FONT, 10), corner_radius=4,
                fg_color=ACT_CLR if tf_code == self.current_tf else INACT_CLR,
                hover_color="#2a7abf",
                command=lambda t=tf_code: self._switch_timeframe(t)
            )
            btn.pack(side="left", padx=2)
            self._tf_buttons[tf_code] = btn

        # Price display
        self._price_frame = ctk.CTkFrame(row2, fg_color="transparent")
        self._price_frame.pack(side="right", padx=10)

        self._lbl_contract = ctk.CTkLabel(
            self._price_frame,
            text=f"{FUTURES[self.current_symbol]}",
            font=(FONT, 13, "bold"), text_color="#ffffff"
        )
        self._lbl_contract.pack(side="left", padx=(0, 15))

        self._lbl_price = ctk.CTkLabel(
            self._price_frame, text="---",
            font=(FONT, 13, "bold"), text_color=C_UP
        )
        self._lbl_price.pack(side="left", padx=(0, 10))

        self._lbl_change = ctk.CTkLabel(
            self._price_frame, text="",
            font=(FONT, 11), text_color=C_TEXT
        )
        self._lbl_change.pack(side="left")

    def _build_main_area(self):
        """Build the main content area with chart canvas."""
        self._main_frame = ctk.CTkFrame(self, fg_color=C_BG, corner_radius=0)
        self._main_frame.pack(fill="both", expand=True, side="top")

        # Chart frame (left side, expands)
        self._chart_frame = ctk.CTkFrame(self._main_frame, fg_color=C_BG, corner_radius=0)
        self._chart_frame.pack(fill="both", expand=True, side="left")

    def _build_sidebar(self):
        """Build the right sidebar with indicator toggles and info."""
        sidebar = ctk.CTkFrame(self._main_frame, fg_color=C_PANEL, width=200, corner_radius=0)
        sidebar.pack(fill="y", side="right", padx=(1, 0))
        sidebar.pack_propagate(False)

        # Scrollable inner area
        inner = ctk.CTkScrollableFrame(
            sidebar, fg_color=C_PANEL,
            scrollbar_button_color="#333333",
            scrollbar_button_hover_color="#555555"
        )
        inner.pack(fill="both", expand=True, padx=2, pady=2)

        # ── INDICATORS section ──
        ctk.CTkLabel(
            inner, text="INDICATORS",
            font=(FONT, 11, "bold"), text_color="#ffffff"
        ).pack(anchor="w", padx=10, pady=(10, 5))

        # Bollinger Bands toggle
        self._sw_bb = ctk.CTkSwitch(
            inner, text="Bollinger Bands",
            font=(FONT, 10), text_color=C_TEXT,
            fg_color="#333333", progress_color=C_BB_BAND,
            command=self._toggle_bb
        )
        self._sw_bb.pack(anchor="w", padx=15, pady=3)

        # RSI toggle
        self._sw_rsi = ctk.CTkSwitch(
            inner, text="RSI (14)",
            font=(FONT, 10), text_color=C_TEXT,
            fg_color="#333333", progress_color=C_RSI,
            command=self._toggle_rsi
        )
        self._sw_rsi.pack(anchor="w", padx=15, pady=3)

        # MACD toggle
        self._sw_macd = ctk.CTkSwitch(
            inner, text="MACD (12,26,9)",
            font=(FONT, 10), text_color=C_TEXT,
            fg_color="#333333", progress_color=C_MACD,
            command=self._toggle_macd
        )
        self._sw_macd.pack(anchor="w", padx=15, pady=3)

        # ── Separator ──
        ctk.CTkFrame(inner, fg_color="#333333", height=1).pack(fill="x", padx=10, pady=10)

        # ── MARKET INFO section ──
        ctk.CTkLabel(
            inner, text="MARKET INFO",
            font=(FONT, 11, "bold"), text_color="#ffffff"
        ).pack(anchor="w", padx=10, pady=(5, 5))

        self._info_labels = {}
        for field in ["Open", "High", "Low", "Close", "Volume", "Candles"]:
            row = ctk.CTkFrame(inner, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=1)
            ctk.CTkLabel(
                row, text=f"{field}:", width=60, anchor="w",
                font=(FONT, 10), text_color="#666666"
            ).pack(side="left")
            lbl = ctk.CTkLabel(
                row, text="---", anchor="e",
                font=(FONT, 10, "bold"), text_color=C_TEXT
            )
            lbl.pack(side="right")
            self._info_labels[field] = lbl

        # ── Separator ──
        ctk.CTkFrame(inner, fg_color="#333333", height=1).pack(fill="x", padx=10, pady=10)

        # ── WATCHLIST section ──
        ctk.CTkLabel(
            inner, text="WATCHLIST",
            font=(FONT, 11, "bold"), text_color="#ffffff"
        ).pack(anchor="w", padx=10, pady=(5, 5))

        self._watchlist_labels = {}
        # Show the 4 index futures as a quick watchlist
        for sym in ["ES=F", "NQ=F", "CL=F", "GC=F"]:
            row = ctk.CTkFrame(inner, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=2)
            ctk.CTkLabel(
                row, text=SHORT_NAMES[sym], width=30, anchor="w",
                font=(FONT, 10, "bold"), text_color="#ffffff"
            ).pack(side="left")
            lbl = ctk.CTkLabel(
                row, text="---", anchor="e",
                font=(FONT, 10), text_color=C_TEXT
            )
            lbl.pack(side="right")
            self._watchlist_labels[sym] = lbl

        # Load watchlist prices in background
        threading.Thread(target=self._load_watchlist_prices, daemon=True).start()

        # ── Separator ──
        ctk.CTkFrame(inner, fg_color="#333333", height=1).pack(fill="x", padx=10, pady=10)

        # ── DATA section ──
        ctk.CTkLabel(
            inner, text="DATA",
            font=(FONT, 11, "bold"), text_color="#ffffff"
        ).pack(anchor="w", padx=10, pady=(5, 5))

        self._btn_refresh = ctk.CTkButton(
            inner, text="Refresh Data", height=30,
            font=(FONT, 10), corner_radius=4,
            fg_color="#333333", hover_color="#444444",
            command=self._refresh_data
        )
        self._btn_refresh.pack(padx=15, pady=5, fill="x")

        self._lbl_last_update = ctk.CTkLabel(
            inner, text="", font=(FONT, 9), text_color="#555555"
        )
        self._lbl_last_update.pack(padx=10, pady=(0, 5))

        # ── CONTROLS HELP ──
        ctk.CTkFrame(inner, fg_color="#333333", height=1).pack(fill="x", padx=10, pady=10)

        ctk.CTkLabel(
            inner, text="CONTROLS",
            font=(FONT, 11, "bold"), text_color="#ffffff"
        ).pack(anchor="w", padx=10, pady=(5, 3))

        controls = [
            "Scroll → Pan",
            "Ctrl+Scroll → Zoom",
            "Drag → Pan",
            "Hover → Crosshair",
        ]
        for c in controls:
            ctk.CTkLabel(
                inner, text=c,
                font=(FONT, 9), text_color="#555555"
            ).pack(anchor="w", padx=15, pady=0)

    def _build_status_bar(self):
        """Build the bottom status bar."""
        bar = ctk.CTkFrame(self, fg_color=C_PANEL, height=24, corner_radius=0)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self._lbl_status = ctk.CTkLabel(
            bar, text="Ready",
            font=(FONT, 9), text_color="#555555"
        )
        self._lbl_status.pack(side="left", padx=10)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  DATA LOADING
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _load_data(self):
        """Load candle data from SQLite for current symbol and timeframe."""
        self._set_status(f"Loading {self.current_symbol} {self.current_tf}...")

        conn = sqlite3.connect(DB_PATH)
        query = """
            SELECT datetime, open, high, low, close, volume
            FROM candles
            WHERE symbol = ? AND timeframe = ?
            ORDER BY datetime ASC
        """
        df = pd.read_sql_query(query, conn, params=(self.current_symbol, self.current_tf))
        conn.close()

        if df.empty:
            self.df_full = pd.DataFrame()
            self._set_status(f"No data for {self.current_symbol} {self.current_tf}")
            return

        df.columns = ["datetime", "open", "high", "low", "close", "volume"]
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)

        self.df_full = df
        self.view_end = len(df)

        # Update price display
        last = df.iloc[-1]
        price = last["close"]
        prev_close = df.iloc[-2]["close"] if len(df) > 1 else price
        change = price - prev_close
        pct = (change / prev_close) * 100 if prev_close else 0

        self._lbl_contract.configure(text=FUTURES[self.current_symbol])
        self._lbl_price.configure(
            text=f"${price:,.2f}",
            text_color=C_UP if change >= 0 else C_DOWN
        )
        self._lbl_change.configure(
            text=f"{'+'if change>=0 else ''}{change:,.2f} ({pct:+.2f}%)",
            text_color=C_UP if change >= 0 else C_DOWN
        )

        # Update info labels
        self._info_labels["Open"].configure(text=f"${last['open']:,.2f}")
        self._info_labels["High"].configure(text=f"${last['high']:,.2f}")
        self._info_labels["Low"].configure(text=f"${last['low']:,.2f}")
        self._info_labels["Close"].configure(text=f"${last['close']:,.2f}")
        self._info_labels["Volume"].configure(text=f"{last['volume']:,.0f}")
        self._info_labels["Candles"].configure(text=f"{len(df):,}")

        self._set_status(f"Loaded {len(df):,} candles for {self.current_symbol} {self.current_tf}")

    def _load_watchlist_prices(self):
        """Load latest prices for watchlist items (runs in background thread)."""
        conn = sqlite3.connect(DB_PATH)
        for sym in self._watchlist_labels:
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT close FROM candles
                    WHERE symbol = ? AND timeframe = '1d'
                    ORDER BY datetime DESC LIMIT 2
                """, (sym,))
                rows = cursor.fetchall()
                if len(rows) >= 2:
                    price = rows[0][0]
                    prev = rows[1][0]
                    change_pct = ((price - prev) / prev) * 100
                    color = C_UP if change_pct >= 0 else C_DOWN
                    text = f"${price:,.2f}  {change_pct:+.1f}%"
                    # Schedule UI update on main thread
                    self.after(0, lambda l=self._watchlist_labels[sym], t=text, c=color:
                              l.configure(text=t, text_color=c))
            except Exception:
                pass
        conn.close()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  CHART BUILDING
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_chart_figure(self):
        """Create the matplotlib figure and embed it in the UI."""
        self.fig = Figure(figsize=(14, 8), facecolor=C_BG)
        self.fig.subplots_adjust(left=0.06, right=0.94, top=0.97, bottom=0.06, hspace=0.05)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self._chart_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # Connect mouse events
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("motion_notify_event", self._on_mouse_move)
        self.canvas.mpl_connect("axes_leave_event", self._on_axes_leave)

        # Keyboard zoom (+/- keys)
        self.bind("<Key>", self._on_key)

    def _draw_chart(self):
        """Draw/redraw the full chart with candles, volume, and active indicators.
        Uses a 4x buffer so panning within the buffer only shifts xlim (fast)."""
        self.fig.clear()

        if self.df_full.empty:
            ax = self.fig.add_subplot(111)
            ax.set_facecolor(C_BG)
            ax.text(0.5, 0.5, "No data available\nRun: python fetch_data.py",
                    ha="center", va="center", color=C_TEXT, fontsize=14,
                    transform=ax.transAxes)
            self.canvas.draw_idle()
            return

        # ── Buffer: draw 4x visible candles for smooth panning ──
        buf_size  = min(len(self.df_full), self.view_size * 4)
        buf_end   = min(len(self.df_full) - 1, self.view_end + self.view_size)
        buf_start = max(0, buf_end - buf_size + 1)
        buf_end   = min(len(self.df_full) - 1, buf_start + buf_size - 1)

        df = self.df_full.iloc[buf_start : buf_end + 1].copy()
        if len(df) < 2:
            self.canvas.draw_idle()
            return

        self._buf_start = buf_start
        self._buf_n = len(df)

        # Store full arrays for _apply_xlim y-range recalculation
        self._np_highs = self.df_full["high"].values
        self._np_lows = self.df_full["low"].values

        # ── Determine subplot layout ──
        n_panels = 2  # candles + volume always
        ratios = [4, 1]
        if self.show_rsi:
            n_panels += 1
            ratios.append(1.5)
        if self.show_macd:
            n_panels += 1
            ratios.append(1.5)

        gs = gridspec.GridSpec(n_panels, 1, figure=self.fig, height_ratios=ratios, hspace=0.05)

        x = np.arange(len(df))
        opens = df["open"].values
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        volumes = df["volume"].values

        bull = closes >= opens
        bear = ~bull

        # Price range for minimum candle body
        price_range = highs.max() - lows.min()
        min_body = price_range * 0.005 if price_range > 0 else 0.01

        # ━━ CANDLE CHART ━━
        self.ax_candles = self.fig.add_subplot(gs[0])
        ax_c = self.ax_candles
        ax_c.set_facecolor(C_BG)

        # Wicks
        ax_c.vlines(x[bull], lows[bull], highs[bull], colors=C_UP, linewidth=0.8)
        ax_c.vlines(x[bear], lows[bear], highs[bear], colors=C_DOWN, linewidth=0.8)

        # Bodies — bullish
        body_height_bull = np.maximum(closes[bull] - opens[bull], min_body)
        ax_c.bar(x[bull], body_height_bull, bottom=opens[bull],
                 width=0.6, color=C_UP, edgecolor=C_UP, linewidth=0.5)

        # Bodies — bearish
        body_height_bear = np.maximum(opens[bear] - closes[bear], min_body)
        ax_c.bar(x[bear], body_height_bear, bottom=closes[bear],
                 width=0.6, color=C_DOWN, edgecolor=C_DOWN, linewidth=0.5)

        # ── Bollinger Bands overlay ──
        if self.show_bb:
            bb_start = max(0, buf_start - 20)
            df_calc = self.df_full.iloc[bb_start : buf_end + 1].copy()
            mid, upper, lower = calc_bollinger(df_calc)
            offset = buf_start - bb_start
            mid_vis = mid.values[offset:]
            upper_vis = upper.values[offset:]
            lower_vis = lower.values[offset:]

            ax_c.plot(x, mid_vis, color=C_BB_MID, linewidth=1.0, alpha=0.8)
            ax_c.plot(x, upper_vis, color=C_BB_BAND, linewidth=0.8, alpha=0.7, linestyle="--")
            ax_c.plot(x, lower_vis, color=C_BB_BAND, linewidth=0.8, alpha=0.7, linestyle="--")
            ax_c.fill_between(x, upper_vis, lower_vis, color=C_BB_BAND, alpha=0.06)

        # Style candle axes
        ax_c.grid(True, color=C_GRID, linewidth=0.5, alpha=0.5)
        ax_c.tick_params(colors=C_TEXT, labelsize=8)
        ax_c.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.2f"))
        ax_c.set_xticklabels([])

        # Contract title on chart
        ax_c.text(
            0.01, 0.97,
            f"{FUTURES[self.current_symbol]}  •  {TIMEFRAMES[self.current_tf]['label']}",
            transform=ax_c.transAxes, fontsize=11, color="#ffffff",
            fontweight="bold", va="top", ha="left",
            fontfamily=FONT
        )

        # ━━ VOLUME ━━
        self.ax_volume = self.fig.add_subplot(gs[1], sharex=ax_c)
        ax_v = self.ax_volume
        ax_v.set_facecolor(C_BG)

        vol_colors = np.where(bull, C_UP, C_DOWN)
        ax_v.bar(x, volumes, width=0.6, color=vol_colors, alpha=0.55)
        if volumes.max() > 0:
            ax_v.set_ylim(0, volumes.max() * 1.2)
        ax_v.grid(True, color=C_GRID, linewidth=0.5, alpha=0.3)
        ax_v.tick_params(colors=C_TEXT, labelsize=8)
        ax_v.yaxis.set_major_formatter(mticker.FuncFormatter(
            lambda v, p: f"{v/1000:.0f}K" if v >= 1000 else f"{v:.0f}"
        ))

        # ── Date labels on bottom-most visible axis ──
        bottom_ax = ax_v  # will change if indicators are shown

        # ━━ RSI ━━
        panel_idx = 2
        self.ax_rsi = None
        if self.show_rsi:
            self.ax_rsi = self.fig.add_subplot(gs[panel_idx], sharex=ax_c)
            ax_r = self.ax_rsi
            ax_r.set_facecolor(C_BG)

            rsi_start = max(0, buf_start - 14)
            df_calc = self.df_full.iloc[rsi_start : buf_end + 1].copy()
            rsi = calc_rsi(df_calc)
            offset = buf_start - rsi_start
            rsi_vis = rsi.values[offset:]

            ax_r.plot(x, rsi_vis, color=C_RSI, linewidth=1.0)
            ax_r.axhline(70, color=C_DOWN, linewidth=0.6, linestyle="--", alpha=0.6)
            ax_r.axhline(30, color=C_UP, linewidth=0.6, linestyle="--", alpha=0.6)
            ax_r.axhline(50, color="#555555", linewidth=0.5, linestyle="--", alpha=0.4)
            ax_r.fill_between(x, 70, 100, color=C_DOWN, alpha=0.05)
            ax_r.fill_between(x, 0, 30, color=C_UP, alpha=0.05)
            ax_r.set_ylim(0, 100)
            ax_r.set_yticks([25, 50, 75])
            ax_r.grid(True, color=C_GRID, linewidth=0.5, alpha=0.3)
            ax_r.tick_params(colors=C_TEXT, labelsize=8)
            ax_r.text(0.01, 0.9, "RSI", transform=ax_r.transAxes,
                      fontsize=9, color=C_RSI, fontweight="bold", va="top")
            bottom_ax = ax_r
            panel_idx += 1

        # ━━ MACD ━━
        self.ax_macd = None
        if self.show_macd:
            self.ax_macd = self.fig.add_subplot(gs[panel_idx], sharex=ax_c)
            ax_m = self.ax_macd
            ax_m.set_facecolor(C_BG)

            macd_start = max(0, buf_start - 26)
            df_calc = self.df_full.iloc[macd_start : buf_end + 1].copy()
            macd_line, signal_line, histogram = calc_macd(df_calc)
            offset = buf_start - macd_start
            macd_vis = macd_line.values[offset:]
            signal_vis = signal_line.values[offset:]
            hist_vis = histogram.values[offset:]

            hist_colors = np.where(hist_vis >= 0, C_UP, C_DOWN)
            ax_m.bar(x, hist_vis, width=0.6, color=hist_colors, alpha=0.53)
            ax_m.plot(x, macd_vis, color=C_MACD, linewidth=1.0)
            ax_m.plot(x, signal_vis, color=C_SIGNAL, linewidth=1.0, linestyle="--")
            ax_m.axhline(0, color="#555555", linewidth=0.5)
            ax_m.grid(True, color=C_GRID, linewidth=0.5, alpha=0.3)
            ax_m.tick_params(colors=C_TEXT, labelsize=8)
            ax_m.text(0.01, 0.9, "MACD", transform=ax_m.transAxes,
                      fontsize=9, color=C_MACD, fontweight="bold", va="top")
            bottom_ax = ax_m
            panel_idx += 1

        # ── X-axis date labels on bottom panel ──
        self._format_x_axis(bottom_ax, df)

        # Reset crosshair refs
        self._cross_v = None
        self._cross_h = None
        self._price_label = None
        self._time_label = None
        self._info_text = None

        # Set initial viewport via _apply_xlim
        self._apply_xlim()

    def _apply_xlim(self):
        """Shift the visible window without redrawing candles — fast pan."""
        if self.ax_candles is None or self.df_full.empty:
            return

        view_start = max(0, self.view_end - self.view_size + 1)
        x_left  = (view_start - self._buf_start) - 0.8
        x_right = (self.view_end - self._buf_start) + 0.8
        self.ax_candles.set_xlim(x_left, x_right)
        self.ax_volume.set_xlim(x_left, x_right)
        if self.ax_rsi:
            self.ax_rsi.set_xlim(x_left, x_right)
        if self.ax_macd:
            self.ax_macd.set_xlim(x_left, x_right)

        # Recalculate y-range from visible candles
        lo = self._np_lows[view_start : self.view_end + 1].min()
        hi = self._np_highs[view_start : self.view_end + 1].max()
        pad = (hi - lo) * 0.04
        self.ax_candles.set_ylim(lo - pad, hi + pad)

        self.canvas.draw_idle()

    def _format_x_axis(self, ax, df):
        """Put readable date/time labels on the bottom axis."""
        n = len(df)
        if n == 0:
            return

        # Pick ~8-12 evenly spaced tick positions
        step = max(1, n // 10)
        tick_positions = list(range(0, n, step))

        ax.set_xticks(tick_positions)

        # Format depends on timeframe
        if self.current_tf == "1d":
            fmt = "%b %Y"
        elif self.current_tf in ("1h",):
            fmt = "%b %d"
        else:
            fmt = "%m/%d %H:%M"

        labels = [df.iloc[i]["datetime"].strftime(fmt) if i < n else "" for i in tick_positions]
        ax.set_xticklabels(labels, rotation=0, fontsize=7, color=C_TEXT)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  MOUSE INTERACTIONS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _on_key(self, event):
        """Handle +/- key presses for zoom."""
        if self.df_full.empty:
            return
        if event.keysym in ("plus", "equal"):
            factor = 0.75       # zoom in
        elif event.keysym == "minus":
            factor = 1.33       # zoom out
        else:
            return
        new_size = int(self.view_size * factor)
        self.view_size = max(20, min(len(self.df_full), new_size))
        self._draw_chart()

    def _on_scroll(self, event):
        """Scroll to pan (velocity-based), Ctrl+Scroll to zoom. 60fps throttled."""
        if self.df_full.empty or event.inaxes is None:
            return

        now = time.time()
        ctrl = event.key in ("ctrl", "control")

        if ctrl:
            # ── Zoom ──
            if now - self._last_draw_time < 0.016:
                return
            self._last_draw_time = now
            factor = 0.75 if event.button == "up" else 1.33
            new_size = int(self.view_size * factor)
            self.view_size = max(20, min(len(self.df_full), new_size))
            self._draw_chart()
        else:
            # ── Pan — velocity-based for smooth scrolling ──
            velocity = abs(event.step) if event.step else 1.0
            step = max(1, int(velocity * 50))
            if event.button == "up":
                self.view_end = max(self.view_size - 1, self.view_end - step)
            else:
                self.view_end = min(len(self.df_full) - 1, self.view_end + step)

            # Check if still within the pre-drawn buffer
            view_start = max(0, self.view_end - self.view_size + 1)
            in_buffer = (view_start >= self._buf_start and
                         self.view_end <= self._buf_start + self._buf_n - 1)
            if in_buffer:
                self._apply_xlim()      # fast — just shift the viewport
            else:
                if now - self._last_draw_time < 0.016:
                    return
                self._last_draw_time = now
                self._draw_chart()      # slow — rebuild buffer

    def _on_press(self, event):
        """Start drag-to-pan."""
        if event.inaxes and event.button == 1:
            self._drag_start_x = event.x
            self._drag_start_view_end = self.view_end

    def _on_release(self, event):
        """End drag-to-pan."""
        self._drag_start_x = None
        self._drag_start_view_end = None

    def _on_mouse_move(self, event):
        """Crosshair + OHLCV tooltip on hover. Also handles drag-to-pan."""
        if self.ax_candles is None or self.df_full.empty:
            return

        # ── Drag to pan (buffer-aware) ──
        if self._drag_start_x is not None and event.x is not None:
            bbox = self.ax_candles.get_window_extent()
            if bbox.width > 0:
                candles_per_px = self.view_size / bbox.width
                shift = int((self._drag_start_x - event.x) * candles_per_px)
                new_end = self._drag_start_view_end + shift
                new_end = max(self.view_size - 1, min(len(self.df_full) - 1, new_end))
                if new_end != self.view_end:
                    self.view_end = new_end
                    view_start = max(0, self.view_end - self.view_size + 1)
                    in_buffer = (view_start >= self._buf_start and
                                 self.view_end <= self._buf_start + self._buf_n - 1)
                    if in_buffer:
                        self._apply_xlim()
                    else:
                        self._draw_chart()
            return

        # ── Crosshair ──
        if event.inaxes is None:
            self._clear_crosshair()
            return

        # Get candle index (relative to buffer)
        if event.xdata is None:
            self._clear_crosshair()
            return

        buf_idx = int(round(event.xdata))
        abs_idx = self._buf_start + buf_idx

        if abs_idx < 0 or abs_idx >= len(self.df_full):
            self._clear_crosshair()
            return

        # Remove old crosshair
        self._clear_crosshair()

        # Draw crosshair on candle axes
        self._cross_v = self.ax_candles.axvline(buf_idx, color=C_CROSS, linewidth=0.5, alpha=0.4)
        if event.inaxes == self.ax_candles:
            self._cross_h = self.ax_candles.axhline(event.ydata, color=C_CROSS, linewidth=0.5, alpha=0.4)

        # OHLCV info text
        row = self.df_full.iloc[abs_idx]
        dt_str = row["datetime"].strftime("%Y-%m-%d %H:%M")
        info = (
            f"{dt_str}\n"
            f"O: {row['open']:,.2f}  H: {row['high']:,.2f}\n"
            f"L: {row['low']:,.2f}  C: {row['close']:,.2f}\n"
            f"Vol: {row['volume']:,.0f}"
        )

        self._info_text = self.ax_candles.text(
            0.99, 0.97, info,
            transform=self.ax_candles.transAxes,
            fontsize=8, color=C_TEXT, fontfamily="Courier",
            va="top", ha="right",
            bbox=dict(boxstyle="round,pad=0.4", facecolor=C_PANEL, edgecolor="#333333", alpha=0.9)
        )

        self.canvas.draw_idle()

    def _on_axes_leave(self, event):
        """Hide crosshair when mouse leaves chart."""
        self._clear_crosshair()
        self.canvas.draw_idle()

    def _clear_crosshair(self):
        """Remove crosshair lines and info text."""
        for artist in [self._cross_v, self._cross_h, self._info_text]:
            if artist is not None:
                try:
                    artist.remove()
                except (ValueError, AttributeError):
                    pass
        self._cross_v = None
        self._cross_h = None
        self._info_text = None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  INDICATOR TOGGLES
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _toggle_bb(self):
        self.show_bb = self._sw_bb.get() == 1
        self._draw_chart()

    def _toggle_rsi(self):
        self.show_rsi = self._sw_rsi.get() == 1
        self._draw_chart()

    def _toggle_macd(self):
        self.show_macd = self._sw_macd.get() == 1
        self._draw_chart()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  SWITCHING CONTRACTS / TIMEFRAMES
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _switch_symbol(self, symbol):
        """Switch to a different futures contract."""
        if symbol == self.current_symbol:
            return

        # Update button colors
        self._symbol_buttons[self.current_symbol].configure(fg_color=INACT_CLR)
        self._symbol_buttons[symbol].configure(fg_color=ACT_CLR)

        self.current_symbol = symbol
        self.view_size = 150
        self._load_data()
        self._draw_chart()

    def _switch_timeframe(self, tf):
        """Switch to a different timeframe."""
        if tf == self.current_tf:
            return

        # Update button colors
        self._tf_buttons[self.current_tf].configure(fg_color=INACT_CLR)
        self._tf_buttons[tf].configure(fg_color=ACT_CLR)

        self.current_tf = tf
        self.view_size = 150
        self._load_data()
        self._draw_chart()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  REFRESH DATA
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _refresh_data(self):
        """Re-download data from yfinance in a background thread."""
        self._btn_refresh.configure(text="Downloading...", state="disabled")
        self._set_status("Downloading fresh data from yfinance...")

        def _do_refresh():
            import subprocess
            result = subprocess.run(
                ["python3", "fetch_data.py", "--all"],
                capture_output=True, text=True,
                cwd="/Users/joethieme/futures-app"
            )
            # Back on main thread
            self.after(0, self._on_refresh_done, result.returncode)

        threading.Thread(target=_do_refresh, daemon=True).start()

    def _on_refresh_done(self, return_code):
        """Called when data refresh completes."""
        self._btn_refresh.configure(text="Refresh Data", state="normal")
        now = datetime.now().strftime("%H:%M:%S")
        self._lbl_last_update.configure(text=f"Last: {now}")

        if return_code == 0:
            self._set_status("Data refreshed successfully!")
            self._load_data()
            self._draw_chart()
            # Refresh watchlist
            threading.Thread(target=self._load_watchlist_prices, daemon=True).start()
        else:
            self._set_status("Refresh failed — check terminal for errors")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  UTILITIES
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _set_status(self, text):
        """Update the status bar text."""
        self._lbl_status.configure(text=text)


# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────

if __name__ == "__main__":
    app = Dashboard()
    app.mainloop()

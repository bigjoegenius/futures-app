#!/usr/bin/env python3
"""
server_controller.py — Desktop controller for the futures-app.

Tkinter (or CustomTkinter if installed) window that shows:
  - Portfolio summary (balance, P&L, trades, win rate)
  - Live prices for all contracts
  - Embedded candlestick chart (matplotlib)
  - Latest autopilot decision
  - Service control panel (talks to a running web_controller.py)
  - Tail of autopilot_log.txt

This is the desktop equivalent of the PWA served by web_controller.py. Both
read the same files and hit the same API. Use whichever fits your workflow.

Usage:
    python server_controller.py
    python server_controller.py --api http://localhost:5100 --token XYZ
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime

try:
    import customtkinter as ctk
    _HAS_CTK = True
except ImportError:
    _HAS_CTK = False
import tkinter as tk
from tkinter import ttk, messagebox

import pandas as pd
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

from futures_config import DB_PATH, FUTURES, TIMEFRAMES
from market_analyzer import load_bars


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_PATH = os.path.join(BASE_DIR, "trade_log.json")
AUTOPILOT_LOG = os.path.join(BASE_DIR, "autopilot_log.txt")

REFRESH_MS = 10_000   # UI refresh every 10s
DARK_BG = "#0d1117"
DARK_CARD = "#161b22"
BORDER = "#30363d"
TEXT = "#c9d1d9"
MUTED = "#8b949e"
GREEN = "#3fb950"
RED = "#f85149"
ACCENT = "#58a6ff"


# ─── API client ────────────────────────────────────────────────────────
class ApiClient:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def get(self, path: str) -> dict | None:
        if not self.base or not self.token:
            return None
        url = f"{self.base}{path}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    def post(self, path: str) -> dict | None:
        if not self.base or not self.token:
            return None
        url = f"{self.base}{path}"
        req = urllib.request.Request(url, method="POST",
                                     headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            return {"error": str(e)}


# ─── Local file readers (work even without API) ────────────────────────
def read_trade_log() -> list:
    if not os.path.exists(TRADE_LOG_PATH):
        return []
    try:
        with open(TRADE_LOG_PATH) as f:
            raw = f.read().strip()
            return json.loads(raw) if raw else []
    except Exception:
        return []


def read_latest_prices() -> dict:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT symbol, last, prev_close, updated_at FROM latest_prices"
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return {}
    out = {}
    for sym, last, pc, ts in rows:
        if not last:
            continue
        pct = ((last - pc) / pc * 100) if pc else 0.0
        out[sym] = {"last": last, "pct": pct, "updated_at": ts}
    return out


def tail_log(n: int = 80) -> str:
    if not os.path.exists(AUTOPILOT_LOG):
        return "(no log — run run_autopilot.py)"
    try:
        with open(AUTOPILOT_LOG) as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception as e:
        return f"(log read error: {e})"


def session_summary(trades: list | None = None) -> dict:
    trades = trades or read_trade_log()
    closed = [t for t in trades if t.get("status") == "closed"]
    wins = [t for t in closed if t.get("pnl_dollars", 0) > 0]
    pnl = sum(t.get("pnl_dollars", 0) for t in closed)
    start = 10_000.0
    return {
        "balance": start + pnl,
        "pnl": pnl,
        "pnl_pct": (pnl / start * 100),
        "trades": len(closed),
        "wins": len(wins),
        "win_rate": (len(wins) / len(closed) * 100) if closed else 0.0,
        "open": len([t for t in trades if t.get("status") == "open"]),
    }


# ─── UI ─────────────────────────────────────────────────────────────────
class FuturesControllerApp:
    def __init__(self, api: ApiClient):
        self.api = api
        if _HAS_CTK:
            ctk.set_appearance_mode("dark")
            self.root = ctk.CTk()
        else:
            self.root = tk.Tk()
            self.root.configure(bg=DARK_BG)
        self.root.title("Futures Controller")
        self.root.geometry("1280x820")

        self.current_symbol = tk.StringVar(value="ES=F")
        self.current_tf = tk.StringVar(value="1h")

        self._build_layout()
        self._refresh_loop()

    # ── Layout ─────────────────────────────────────────────────────────
    def _build_layout(self):
        main = tk.Frame(self.root, bg=DARK_BG)
        main.pack(fill="both", expand=True, padx=8, pady=8)

        # Left column: portfolio + prices + services
        left = tk.Frame(main, bg=DARK_BG, width=340)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)

        self._build_portfolio_card(left)
        self._build_prices_card(left)
        self._build_services_card(left)

        # Right column: chart + ai + log
        right = tk.Frame(main, bg=DARK_BG)
        right.pack(side="left", fill="both", expand=True)

        self._build_chart_card(right)
        self._build_ai_card(right)
        self._build_log_card(right)

    def _card(self, parent, title: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=DARK_CARD, highlightbackground=BORDER,
                         highlightthickness=1)
        outer.pack(fill="x", pady=(0, 8))
        header = tk.Label(outer, text=title, bg=DARK_CARD, fg=MUTED,
                          font=("Helvetica", 9, "bold"), anchor="w", padx=10, pady=6)
        header.pack(fill="x")
        return outer

    # ── Portfolio ──────────────────────────────────────────────────────
    def _build_portfolio_card(self, parent):
        card = self._card(parent, "AUTOPILOT")
        self.lbl_pnl = tk.Label(card, text="$0.00", bg=DARK_CARD, fg=TEXT,
                                font=("Helvetica", 28, "bold"))
        self.lbl_pnl.pack(anchor="w", padx=10)
        self.lbl_pnl_pct = tk.Label(card, text="(0.00%)", bg=DARK_CARD, fg=MUTED,
                                    font=("Helvetica", 11))
        self.lbl_pnl_pct.pack(anchor="w", padx=10, pady=(0, 6))

        stats = tk.Frame(card, bg=DARK_CARD)
        stats.pack(fill="x", padx=10, pady=(0, 10))
        self.stat_vals = {}
        for col, label in enumerate(["Balance", "Trades", "Win Rate", "Open"]):
            cell = tk.Frame(stats, bg=DARK_CARD)
            cell.grid(row=0, column=col, sticky="w", padx=(0, 14))
            tk.Label(cell, text=label, bg=DARK_CARD, fg=MUTED,
                     font=("Helvetica", 8)).pack(anchor="w")
            v = tk.Label(cell, text="--", bg=DARK_CARD, fg=TEXT,
                         font=("Helvetica", 12, "bold"))
            v.pack(anchor="w")
            self.stat_vals[label] = v

    # ── Prices ─────────────────────────────────────────────────────────
    def _build_prices_card(self, parent):
        card = self._card(parent, "LIVE PRICES")
        body = tk.Frame(card, bg=DARK_CARD)
        body.pack(fill="both", padx=6, pady=(0, 8))

        cols = ("sym", "last", "pct")
        self.prices_tree = ttk.Treeview(body, columns=cols, show="headings", height=12)
        for c, w in zip(cols, (80, 110, 90)):
            self.prices_tree.column(c, width=w, anchor="w")
        self.prices_tree.heading("sym", text="Symbol")
        self.prices_tree.heading("last", text="Last")
        self.prices_tree.heading("pct", text="Change")
        self.prices_tree.pack(fill="x")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=DARK_CARD, fieldbackground=DARK_CARD,
                        foreground=TEXT, bordercolor=BORDER)
        style.configure("Treeview.Heading", background=DARK_BG, foreground=MUTED)
        style.map("Treeview", background=[("selected", BORDER)])

        self.prices_tree.tag_configure("up", foreground=GREEN)
        self.prices_tree.tag_configure("dn", foreground=RED)

    # ── Services ───────────────────────────────────────────────────────
    def _build_services_card(self, parent):
        card = self._card(parent, "SERVICES")
        self.svc_body = tk.Frame(card, bg=DARK_CARD)
        self.svc_body.pack(fill="x", padx=8, pady=(0, 8))

    def _draw_services(self, services: list):
        for w in self.svc_body.winfo_children():
            w.destroy()
        if not services:
            tk.Label(self.svc_body, text="API not reachable — start web_controller.py",
                     bg=DARK_CARD, fg=MUTED, font=("Helvetica", 9), wraplength=300).pack(anchor="w")
            return
        for s in services:
            row = tk.Frame(self.svc_body, bg=DARK_CARD)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=s["name"], bg=DARK_CARD, fg=TEXT,
                     font=("Helvetica", 10, "bold")).pack(side="left")
            state = s.get("state", "?")
            color = {"active": GREEN, "inactive": MUTED, "failed": RED}.get(state, MUTED)
            tk.Label(row, text=state, bg=DARK_CARD, fg=color,
                     font=("Helvetica", 9)).pack(side="right")
            if state != "unavailable" and self.api.token:
                btn = tk.Button(row, text="restart", bg=DARK_BG, fg=TEXT, bd=0,
                                font=("Helvetica", 8), padx=6,
                                command=lambda n=s["name"]: self._service_action(n, "restart"))
                btn.pack(side="right", padx=4)

    def _service_action(self, name: str, action: str):
        resp = self.api.post(f"/api/services/{name}/{action}")
        if resp and resp.get("error"):
            messagebox.showerror("Service action", resp["error"])
        else:
            self.root.after(500, self._refresh_services)

    # ── Chart ──────────────────────────────────────────────────────────
    def _build_chart_card(self, parent):
        card = self._card(parent, "CHART")
        controls = tk.Frame(card, bg=DARK_CARD)
        controls.pack(fill="x", padx=10, pady=(0, 6))
        sym_menu = ttk.Combobox(controls, textvariable=self.current_symbol,
                                values=list(FUTURES.keys()), state="readonly", width=10)
        sym_menu.pack(side="left", padx=4)
        tf_menu = ttk.Combobox(controls, textvariable=self.current_tf,
                               values=list(TIMEFRAMES.keys()), state="readonly", width=6)
        tf_menu.pack(side="left", padx=4)
        tk.Button(controls, text="Refresh", bg=ACCENT, fg="#0d1117", bd=0,
                  font=("Helvetica", 9, "bold"), padx=10,
                  command=self._draw_chart).pack(side="left", padx=4)
        sym_menu.bind("<<ComboboxSelected>>", lambda e: self._draw_chart())
        tf_menu.bind("<<ComboboxSelected>>", lambda e: self._draw_chart())

        self.fig = Figure(figsize=(8, 4.5), facecolor=DARK_CARD)
        self.ax = self.fig.add_subplot(111, facecolor=DARK_BG)
        self.canvas = FigureCanvasTkAgg(self.fig, card)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=(0, 8))

    def _draw_chart(self):
        sym = self.current_symbol.get()
        tf = self.current_tf.get()
        df = load_bars(sym, tf, 150)
        self.ax.clear()
        self.ax.set_facecolor(DARK_BG)
        for spine in self.ax.spines.values():
            spine.set_color(BORDER)
        self.ax.tick_params(colors=MUTED, labelsize=8)
        self.ax.set_title(f"{sym}  {FUTURES.get(sym, '')}  ({tf})",
                          color=TEXT, fontsize=10, loc="left")

        if df is None or df.empty:
            self.ax.text(0.5, 0.5, "no data — run fetch_data.py",
                         color=MUTED, ha="center", va="center",
                         transform=self.ax.transAxes)
        else:
            xs = list(range(len(df)))
            for x, (_, row) in zip(xs, df.iterrows()):
                up = row["close"] >= row["open"]
                color = GREEN if up else RED
                self.ax.vlines(x, row["low"], row["high"], color=color, linewidth=0.7)
                y0, y1 = min(row["open"], row["close"]), max(row["open"], row["close"])
                self.ax.add_patch(matplotlib.patches.Rectangle(
                    (x - 0.3, y0), 0.6, max(0.0001, y1 - y0),
                    facecolor=color, edgecolor=color,
                ))
            self.ax.set_xlim(-1, len(df))
            self.ax.grid(True, color=BORDER, linewidth=0.3, alpha=0.4)
        self.fig.tight_layout()
        self.canvas.draw_idle()

    # ── AI ─────────────────────────────────────────────────────────────
    def _build_ai_card(self, parent):
        card = self._card(parent, "LATEST AI DECISION")
        self.ai_text = tk.Label(card, text="loading...", bg=DARK_CARD, fg=TEXT,
                                justify="left", wraplength=820, anchor="w",
                                font=("Helvetica", 10), padx=10, pady=6)
        self.ai_text.pack(fill="x")

    # ── Log ────────────────────────────────────────────────────────────
    def _build_log_card(self, parent):
        card = self._card(parent, "AUTOPILOT LOG")
        self.log_text = tk.Text(card, bg="black", fg=TEXT, height=10,
                                font=("Menlo", 9), bd=0, padx=8, pady=6,
                                insertbackground=TEXT)
        self.log_text.pack(fill="both", expand=True, padx=4, pady=(0, 8))

    # ── Refresh cycle ─────────────────────────────────────────────────
    def _refresh_loop(self):
        threading.Thread(target=self._refresh_data, daemon=True).start()
        self.root.after(REFRESH_MS, self._refresh_loop)

    def _refresh_data(self):
        try:
            prices = read_latest_prices()
            summary = session_summary()
            self.root.after(0, self._apply_prices, prices)
            self.root.after(0, self._apply_portfolio, summary)
            self._refresh_services()
            self._refresh_ai()
            self._refresh_log()
            # Chart refresh on same cadence — the first call may overlap with startup,
            # which is fine, tkinter runs it on the main loop.
            self.root.after(0, self._draw_chart)
        except Exception as e:
            print(f"[server_controller] refresh error: {e}")

    def _apply_prices(self, prices: dict):
        self.prices_tree.delete(*self.prices_tree.get_children())
        for sym, data in sorted(prices.items()):
            pct = data["pct"]
            tag = "up" if pct >= 0 else "dn"
            self.prices_tree.insert("", "end", values=(
                sym, f"{data['last']:.2f}", f"{pct:+.2f}%"
            ), tags=(tag,))

    def _apply_portfolio(self, s: dict):
        self.lbl_pnl.config(text=f"${s['pnl']:+,.2f}",
                            fg=GREEN if s["pnl"] >= 0 else RED)
        self.lbl_pnl_pct.config(text=f"({s['pnl_pct']:+.2f}%)")
        self.stat_vals["Balance"].config(text=f"${s['balance']:,.0f}")
        self.stat_vals["Trades"].config(text=str(s["trades"]))
        self.stat_vals["Win Rate"].config(text=f"{s['win_rate']:.0f}%")
        self.stat_vals["Open"].config(text=str(s["open"]))

    def _refresh_services(self):
        data = self.api.get("/api/services")
        services = (data or {}).get("services", []) if data else []
        self.root.after(0, self._draw_services, services)

    def _refresh_ai(self):
        data = self.api.get("/api/ai-overview")
        text = "no API connection — start web_controller.py"
        if data:
            ap = data.get("autopilot")
            if ap:
                text = (f"Risk: {ap.get('risk_mode','?')}   Model: {ap.get('ai_model','?')}\n"
                        f"Enabled: {', '.join(ap.get('enabled', []))}\n\n"
                        f"{ap.get('reasoning','')[:800]}")
            else:
                text = "No autopilot decisions yet."
        self.root.after(0, lambda: self.ai_text.config(text=text))

    def _refresh_log(self):
        txt = tail_log(80)
        def apply():
            self.log_text.delete("1.0", "end")
            self.log_text.insert("1.0", txt)
            self.log_text.see("end")
        self.root.after(0, apply)

    def run(self):
        self.root.mainloop()


# ─── Main ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("CONTROLLER_API", "http://localhost:5100"))
    ap.add_argument("--token", default=os.environ.get("WEB_CONTROLLER_TOKEN", ""))
    args = ap.parse_args()

    client = ApiClient(args.api, args.token)
    app = FuturesControllerApp(client)
    app.run()


if __name__ == "__main__":
    main()

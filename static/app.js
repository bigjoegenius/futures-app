// Futures Controller — SPA logic
"use strict";

const TOKEN_KEY = "futures_ctrl_token";
let authToken = null;
let role = null;
let refreshTimer = null;

// ─── Auth ─────────────────────────────────────────────────────────────
function doLogin() {
    const t = document.getElementById("token-input").value.trim();
    if (!t) { setLoginError("Token required"); return; }
    authToken = t;
    verifyAuth().then(ok => {
        if (ok) {
            localStorage.setItem(TOKEN_KEY, authToken);
            showApp();
        } else {
            setLoginError("Invalid token");
            authToken = null;
        }
    });
}

function doLogout() {
    localStorage.removeItem(TOKEN_KEY);
    authToken = null; role = null;
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
    document.getElementById("app").classList.add("hidden");
    document.getElementById("login-screen").classList.remove("hidden");
    document.getElementById("token-input").value = "";
}

function setLoginError(msg) {
    document.getElementById("login-error").textContent = msg || "";
}

async function verifyAuth() {
    try {
        const r = await api("/api/status");
        role = r && r.role;
        return !!role;
    } catch { return false; }
}

// ─── API wrapper ──────────────────────────────────────────────────────
async function api(path, opts = {}) {
    const resp = await fetch(path, {
        ...opts,
        headers: {
            "Authorization": "Bearer " + authToken,
            ...(opts.headers || {}),
        },
    });
    if (!resp.ok) throw new Error(`${resp.status}`);
    return resp.json();
}

// ─── App boot ─────────────────────────────────────────────────────────
function showApp() {
    document.getElementById("login-screen").classList.add("hidden");
    document.getElementById("app").classList.remove("hidden");
    const rl = document.getElementById("role-label");
    rl.textContent = role === "viewer" ? "[read-only]" : "";
    document.getElementById("run-ai-btn").style.display = (role === "admin") ? "" : "none";
    populateChartDropdown();
    refreshAll();
    refreshTimer = setInterval(refreshAll, 15000);
}

window.addEventListener("load", () => {
    const saved = localStorage.getItem(TOKEN_KEY);
    if (saved) {
        authToken = saved;
        verifyAuth().then(ok => { if (ok) showApp(); });
    }
    // PWA service worker
    if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
    }
});

// ─── Tabs ─────────────────────────────────────────────────────────────
function switchTab(name) {
    document.querySelectorAll(".tab-pane").forEach(el => el.classList.remove("active"));
    document.querySelectorAll(".tab").forEach(el => el.classList.remove("active"));
    document.getElementById("tab-" + name).classList.add("active");
    document.querySelector(`.tab[data-tab="${name}"]`).classList.add("active");
    if (name === "chart") loadChart();
    if (name === "services") loadServices();
    if (name === "prices") loadPrices();
    if (name === "trades") loadTrades();
}

// ─── Refresh loop ─────────────────────────────────────────────────────
async function refreshAll() {
    try {
        await Promise.all([loadStatus(), loadPricesHeader(), loadNews(), loadAI()]);
        setDot(true);
    } catch {
        setDot(false);
    }
}

function setDot(ok) {
    const d = document.getElementById("conn-dot");
    if (d) d.classList.toggle("off", !ok);
}

// ─── Status ───────────────────────────────────────────────────────────
async function loadStatus() {
    const data = await api("/api/status");
    const s = data.session || {};
    const pnl = s.total_pnl || 0;
    const pnlPct = s.total_pnl_pct || 0;
    const el = document.getElementById("pnl-display");
    el.textContent = fmtMoney(pnl);
    el.classList.toggle("up", pnl >= 0);
    el.classList.toggle("dn", pnl < 0);
    document.getElementById("pnl-pct").textContent = `(${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%)`;
    document.getElementById("balance").textContent = fmtMoney(s.balance || 0);
    document.getElementById("total-trades").textContent = s.trades || 0;
    document.getElementById("win-rate").textContent = `${(s.win_rate || 0).toFixed(0)}%`;

    const ap = data.autopilot;
    const rb = document.getElementById("risk-badge");
    if (ap && ap.risk_mode) {
        rb.textContent = ap.risk_mode;
        rb.className = "badge " + (ap.risk_mode === "aggressive" ? "red" : ap.risk_mode === "moderate" ? "green" : "");
    } else {
        rb.textContent = "--";
        rb.className = "badge";
    }
}

// ─── AI ───────────────────────────────────────────────────────────────
async function loadAI() {
    try {
        const d = await api("/api/ai-overview");
        const ap = d.autopilot;
        const el = document.getElementById("ai-decision");
        if (!ap) { el.innerHTML = `<div class="muted">no decision yet</div>`; return; }
        el.innerHTML = `
            <div><strong>${ap.risk_mode || "?"}</strong> · ${ap.ai_model || ""}</div>
            <div class="enabled">${(ap.enabled || []).join(", ") || "(none enabled)"}</div>
            <div class="reasoning">${escapeHtml(ap.reasoning || "")}</div>
            <div class="muted" style="margin-top:6px;font-size:10px">${ap.ts || ""}</div>
        `;
        // Open positions count (from trades endpoint is heavier; skip here)
    } catch {}
}

async function runAutopilotNow() {
    const btn = document.getElementById("run-ai-btn");
    btn.disabled = true;
    btn.textContent = "Running...";
    try {
        await api("/api/autopilot/run-now", { method: "POST" });
        await loadAI();
    } catch (e) {
        alert("Failed: " + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = "Run now";
    }
}

// ─── Prices ───────────────────────────────────────────────────────────
async function loadPricesHeader() {
    try {
        const d = await api("/api/prices");
        const top = Object.values(d).slice(0, 4);
        document.getElementById("header-prices").innerHTML = top.map(p =>
            `<span class="hp"><span class="n">${p.symbol.replace("=F", "")}</span>
             <span class="${p.change_pct >= 0 ? "up" : "dn"}">${fmtNum(p.last)}</span></span>`
        ).join("");
    } catch {}
}

async function loadPrices() {
    const d = await api("/api/prices");
    const rows = Object.values(d).sort((a, b) => a.symbol.localeCompare(b.symbol));
    document.getElementById("prices-tbody").innerHTML = rows.map(p => `
        <tr>
            <td>${p.symbol} <span class="muted" style="font-size:10px">${p.name}</span></td>
            <td>${fmtNum(p.last)}</td>
            <td class="${p.change_pct >= 0 ? "up" : "dn"}">${p.change_pct >= 0 ? "+" : ""}${p.change_pct.toFixed(2)}%</td>
            <td class="muted" style="font-size:10px">${shortTime(p.updated_at)}</td>
        </tr>
    `).join("") || `<tr><td colspan="4" class="muted">no prices yet — start live_prices.py</td></tr>`;
}

// ─── News ─────────────────────────────────────────────────────────────
async function loadNews() {
    try {
        const d = await api("/api/news-digest");
        const list = (d.all_headlines || []).slice(0, 6);
        document.getElementById("news-summary").innerHTML = list.length
            ? list.map(h => `<div class="headline">${escapeHtml(h.title || "")}</div>`).join("")
            : `<div class="muted">no headlines</div>`;
    } catch {}
}

// ─── Trades ───────────────────────────────────────────────────────────
async function loadTrades() {
    const d = await api("/api/trades");
    const open = d.open || [];
    const closed = (d.closed || []).slice().reverse();
    document.getElementById("open-positions").textContent = open.length;

    document.getElementById("open-tbody").innerHTML = open.length
        ? open.map(t => `
            <tr>
                <td>${t.symbol}</td><td>${t.strategy}</td><td>${t.direction}</td>
                <td>${fmtNum(t.entry_price)}</td><td>${fmtNum(t.stop_price)}</td><td>${fmtNum(t.target_price)}</td>
            </tr>`).join("")
        : `<tr><td colspan="6" class="muted">none</td></tr>`;

    document.getElementById("closed-tbody").innerHTML = closed.length
        ? closed.slice(0, 40).map(t => `
            <tr>
                <td>${t.symbol}</td><td>${t.strategy}</td><td>${t.direction}</td>
                <td class="${t.pnl_dollars >= 0 ? "up" : "dn"}">${fmtMoney(t.pnl_dollars || 0)}</td>
                <td class="${t.pnl_pct >= 0 ? "up" : "dn"}">${(t.pnl_pct || 0).toFixed(2)}%</td>
                <td class="muted">${t.exit_reason || ""}</td>
            </tr>`).join("")
        : `<tr><td colspan="6" class="muted">no closed trades</td></tr>`;
}

// ─── Services + log ───────────────────────────────────────────────────
async function loadServices() {
    try {
        const d = await api("/api/services");
        document.getElementById("services-list").innerHTML = (d.services || []).map(s => `
            <div class="svc-row">
                <div class="svc-meta">
                    <div class="svc-name">${s.name}</div>
                    <div class="svc-desc">${s.desc || ""}</div>
                </div>
                <span class="svc-state ${s.state}">${s.state}</span>
                ${role === "admin" && s.state !== "unavailable" ? `
                    <div class="svc-actions">
                        <button onclick="svcAction('${s.name}','start')">start</button>
                        <button onclick="svcAction('${s.name}','stop')">stop</button>
                        <button onclick="svcAction('${s.name}','restart')">rst</button>
                    </div>` : ""}
            </div>
        `).join("");
        const l = await api("/api/log?lines=60");
        document.getElementById("log-pane").textContent = l.log || "(no log yet)";
    } catch (e) {
        document.getElementById("services-list").innerHTML = `<div class="muted">error: ${e.message}</div>`;
    }
}

async function svcAction(name, action) {
    try {
        await api(`/api/services/${name}/${action}`, { method: "POST" });
        setTimeout(loadServices, 500);
    } catch (e) { alert("Failed: " + e.message); }
}

// ─── Chart (simple candlestick on canvas) ─────────────────────────────
async function populateChartDropdown() {
    try {
        const d = await api("/api/prices");
        const syms = Object.keys(d).sort();
        const list = syms.length ? syms : ["ES=F", "NQ=F", "GC=F", "CL=F"];
        document.getElementById("chart-symbol").innerHTML =
            list.map(s => `<option value="${s}">${s}</option>`).join("");
    } catch {
        document.getElementById("chart-symbol").innerHTML =
            ["ES=F", "NQ=F", "GC=F", "CL=F"].map(s => `<option value="${s}">${s}</option>`).join("");
    }
}

async function loadChart() {
    const sym = document.getElementById("chart-symbol").value;
    const tf  = document.getElementById("chart-timeframe").value;
    if (!sym) return;
    try {
        const d = await api(`/api/candles/${encodeURIComponent(sym)}?timeframe=${tf}&limit=150`);
        drawCandles(d.candles || []);
    } catch (e) { console.error(e); }
}

function drawCandles(candles) {
    const canvas = document.getElementById("chart-canvas");
    const ctx = canvas.getContext("2d");
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * (window.devicePixelRatio || 1);
    canvas.height = 400 * (window.devicePixelRatio || 1);
    ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);
    const W = rect.width, H = 400;
    ctx.fillStyle = "#0d1117";
    ctx.fillRect(0, 0, W, H);

    if (!candles.length) {
        ctx.fillStyle = "#8b949e"; ctx.font = "13px sans-serif";
        ctx.fillText("no candles — run python fetch_data.py", 20, 30);
        return;
    }
    const hi = Math.max(...candles.map(c => c.high));
    const lo = Math.min(...candles.map(c => c.low));
    const span = (hi - lo) || 1;
    const bw = Math.max(1, (W - 40) / candles.length - 1);
    const margin = 20;

    const yFor = (p) => H - margin - ((p - lo) / span) * (H - 2 * margin);

    candles.forEach((c, i) => {
        const x = margin + i * (bw + 1);
        const up = c.close >= c.open;
        ctx.strokeStyle = up ? "#3fb950" : "#f85149";
        ctx.fillStyle = up ? "#3fb950" : "#f85149";
        // wick
        ctx.beginPath(); ctx.moveTo(x + bw / 2, yFor(c.high)); ctx.lineTo(x + bw / 2, yFor(c.low)); ctx.stroke();
        // body
        const yo = yFor(c.open); const yc = yFor(c.close);
        ctx.fillRect(x, Math.min(yo, yc), bw, Math.max(1, Math.abs(yc - yo)));
    });

    // price scale
    ctx.fillStyle = "#8b949e"; ctx.font = "11px Menlo, monospace";
    ctx.fillText(hi.toFixed(2), 2, 14);
    ctx.fillText(lo.toFixed(2), 2, H - 4);
}

// ─── Utils ────────────────────────────────────────────────────────────
function fmtMoney(v) {
    const sign = v < 0 ? "-" : "";
    const n = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return `${sign}$${n}`;
}
function fmtNum(v) {
    if (v == null) return "--";
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}
function shortTime(s) {
    if (!s) return "";
    try {
        const d = new Date(s.replace(" ", "T") + (s.endsWith("Z") ? "" : "Z"));
        return d.toLocaleTimeString();
    } catch { return s; }
}
function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[c]);
}

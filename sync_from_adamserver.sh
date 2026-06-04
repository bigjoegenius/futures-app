#!/bin/bash
# sync_from_adamserver.sh — pull the latest futures.db AND crypto prices.db
# from adamserver. Runs hourly via launchd
# (see ~/Library/LaunchAgents/com.joe.futures-sync.plist).
#
# Why both DBs land here:
#   - futures.db is collected on adamserver via futures-collector.timer (:07)
#   - prices.db is written 24/7 by crypto-collectors (perp_all.py) on adamserver
#   Both feed tradingdashboard.py's chart rendering. Without prices.db sync,
#   the crypto half of the dashboard goes stale (used to lag ~6 days because
#   the file was a one-off SCP from initial deployment, never refreshed).

set -u
LOCAL_DIR="$HOME/trading/futures-app"
CRYPTO_DIR="$HOME/trading/crypto-app"
CATALYST_DIR="$HOME/trading/catalyst-trader"
OPTIONS_DIR="$HOME/trading/catalyst-options"
ETF_DIR="$HOME/trading/catalyst-etf"
LOG="$LOCAL_DIR/sync.log"
LOCK_DIR="$LOCAL_DIR/.sync.lock.d"
REMOTE_FUTURES="adamserver:/home/joe/futures-app/futures.db"
REMOTE_CRYPTO="adamserver:/home/joe/crypto-app/prices.db"
REMOTE_CATALYST="adamserver:/home/joe/catalyst-trader/catalyst.db"
REMOTE_OPTIONS="adamserver:/home/joe/catalyst-options/options.db"
REMOTE_ETF="adamserver:/home/joe/catalyst-etf/etf.db"
# Reverse push (Mac → adamserver): the public viewer's Reports tab serves
# stock dossiers + publishable reports that are GENERATED on this Mac. Source
# folders live under the iCloud "Created by claude" hub; dest dirs are created
# on adamserver below. Personal portfolio reviews are EXCLUDED at push time so
# they never leave the Mac (public_viewer also denylists them as a backstop).
CREATED_DIR="$HOME/Desktop/trading/Created by claude"
DOSSIERS_SRC="$CREATED_DIR/03 Dossiers"
REVIEWS_SRC="$CREATED_DIR/04 Reports & Reviews"
BACKTESTS_SRC="$CREATED_DIR/05 Backtests"
REMOTE_REPORTS_DOSSIERS="adamserver:/home/joe/reports/dossiers/"
REMOTE_REPORTS_REVIEWS="adamserver:/home/joe/reports/reviews/"

# Make sure mirror dirs exist for the 3 sibling engines that don't live in the
# trading/ repo tree on this Mac. The crypto + futures dirs already exist
# (they're the apps themselves), but catalyst-trader, catalyst-options, and
# catalyst-etf live under /home/joe on adamserver and are read-only mirrors
# here just to feed public_viewer.py running locally.
for d in "$CATALYST_DIR" "$OPTIONS_DIR" "$ETF_DIR"; do
    [ -d "$d" ] || /bin/mkdir -p "$d"
done

cd "$LOCAL_DIR" || exit 1

# Portable mutex via mkdir (atomic on all unix filesystems, works on macOS + Linux).
# If the lock dir already exists, another sync is still going — bail out.
if ! /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
    # Safety net: if the lock is older than 1 hour something went wrong — clear it.
    if [ -n "$(/usr/bin/find "$LOCK_DIR" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
        /bin/rmdir "$LOCK_DIR" 2>/dev/null
        /bin/mkdir "$LOCK_DIR" 2>/dev/null || { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] stale lock cleanup failed" >> "$LOG"; exit 1; }
    else
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] skipped — previous sync still running" >> "$LOG"
        exit 0
    fi
fi
trap '/bin/rmdir "$LOCK_DIR" 2>/dev/null' EXIT

{
    echo ""
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ---- sync start ----"

    # -a      archive (preserve times, perms)
    # -v      verbose
    # -z      compress on the wire (SQLite files compress very well)
    # --partial  resume interrupted transfers
    # --stats    final transfer summary
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] -- futures.db --"
    /usr/bin/rsync -avz --partial --stats \
        -e 'ssh -o ConnectTimeout=10 -o BatchMode=yes' \
        "$REMOTE_FUTURES" "$LOCAL_DIR/futures.db" 2>&1
    rc_futures=$?

    # crypto-app/prices.db — same pattern. ~2GB on disk but rsync compresses
    # delta-only so a normal hourly catch-up is tens of MB on the wire.
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] -- prices.db --"
    if [ -d "$CRYPTO_DIR" ]; then
        /usr/bin/rsync -avz --partial --stats \
            -e 'ssh -o ConnectTimeout=10 -o BatchMode=yes' \
            "$REMOTE_CRYPTO" "$CRYPTO_DIR/prices.db" 2>&1
        rc_crypto=$?
    else
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] crypto-app dir missing, skipping prices.db"
        rc_crypto=0
    fi

    # catalyst-trader/catalyst.db — stocks/ETFs/futures/crypto AI daily picks
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] -- catalyst.db --"
    /usr/bin/rsync -avz --partial --stats \
        -e 'ssh -o ConnectTimeout=10 -o BatchMode=yes' \
        "$REMOTE_CATALYST" "$CATALYST_DIR/catalyst.db" 2>&1
    rc_catalyst=$?

    # catalyst-options/options.db — long-options paper trader
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] -- options.db --"
    /usr/bin/rsync -avz --partial --stats \
        -e 'ssh -o ConnectTimeout=10 -o BatchMode=yes' \
        "$REMOTE_OPTIONS" "$OPTIONS_DIR/options.db" 2>&1
    rc_options=$?

    # catalyst-etf/etf.db — Sector SPDR news-driven trader (11 ETFs)
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] -- etf.db --"
    /usr/bin/rsync -avz --partial --stats \
        -e 'ssh -o ConnectTimeout=10 -o BatchMode=yes' \
        "$REMOTE_ETF" "$ETF_DIR/etf.db" 2>&1
    rc_etf=$?

    # ── Reverse push: dossiers + publishable reports → adamserver ─────────
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] -- push reports (dossiers + reviews) --"
    rc_push_doss=0; rc_push_rev=0
    if [ -d "$CREATED_DIR" ]; then
        /usr/bin/ssh -o ConnectTimeout=10 -o BatchMode=yes adamserver \
            'mkdir -p /home/joe/reports/dossiers/sources /home/joe/reports/reviews' 2>&1

        # Dossiers: PDFs + their .md sources only. --delete mirrors removals,
        # guarded by a non-empty check so an unmounted/empty iCloud folder
        # can never wipe the published set.
        if [ -n "$(/bin/ls "$DOSSIERS_SRC"/*.pdf 2>/dev/null)" ]; then
            /usr/bin/rsync -az --partial --delete \
                --include='/*.pdf' --include='/sources/' --include='/sources/*.md' --exclude='*' \
                -e 'ssh -o ConnectTimeout=10 -o BatchMode=yes' \
                "$DOSSIERS_SRC/" "$REMOTE_REPORTS_DOSSIERS" 2>&1
            rc_push_doss=$?
        else
            echo "  (no dossier PDFs found — skipping dossier push)"
        fi

        # Reviews + backtests merge into one dest (so NO --delete). Personal
        # portfolio reviews are excluded before the pdf/docx/html include.
        for SRC in "$REVIEWS_SRC" "$BACKTESTS_SRC"; do
            [ -d "$SRC" ] || continue
            /usr/bin/rsync -az --partial \
                --exclude='.*' --exclude='~$*' \
                --exclude='*Portfolio_Review*' --exclude='*portfolio_review*' \
                --exclude='*Portfolio_Deep_Dive*' --exclude='*portfolio_deep_dive*' \
                --exclude='Big_Joe*' --exclude='*Big Joe*' --exclude='*[Pp]ersonal*' \
                --include='*.pdf' --include='*.docx' --include='*.html' --exclude='*' \
                -e 'ssh -o ConnectTimeout=10 -o BatchMode=yes' \
                "$SRC/" "$REMOTE_REPORTS_REVIEWS" 2>&1
            rc_push_rev=$(( rc_push_rev | $? ))
        done
    else
        echo "  (Created-by-Claude folder not found — skipping reports push)"
    fi

    rc=$(( rc_futures | rc_crypto | rc_catalyst | rc_options | rc_etf ))
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ---- sync done (futures=$rc_futures crypto=$rc_crypto catalyst=$rc_catalyst options=$rc_options etf=$rc_etf push_doss=$rc_push_doss push_rev=$rc_push_rev) ----"
} >> "$LOG" 2>&1

# Keep log under ~2000 lines
/usr/bin/tail -n 2000 "$LOG" > "$LOG.tmp" && /bin/mv "$LOG.tmp" "$LOG"

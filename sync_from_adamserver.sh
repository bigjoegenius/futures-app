#!/bin/bash
# sync_from_adamserver.sh — pull consistent snapshots of every live SQLite DB
# from adamserver. Runs hourly via launchd
# (see ~/Library/LaunchAgents/com.joe.futures-sync.plist).
#
# Why snapshots instead of rsyncing the live .db files (changed 2026-06-11):
#   The server DBs are written 24/7 in WAL mode (prices.db every few seconds).
#   rsync reading a file while a writer is mid-transaction produces a TORN
#   copy — on 2026-06-11 the Mac's prices.db failed PRAGMA quick_check
#   ("invalid pages", broken sol_perp_candles index) while the server copy
#   checked out "ok". Rsyncing only the .db also silently drops whatever is
#   still sitting in the -wal sidecar, so the Mac copy lagged recent writes.
#   Fix: one ssh call runs SQLite's online-backup API (python3 one-shot
#   Connection.backup) on the server, writing point-in-time snapshots into
#   /tmp/mac-sync-snap. /tmp on adamserver is tmpfs (RAM-backed, 63 GB), so
#   snapshots cost zero space on the 81%-full root disk and vanish on reboot.
#   A single-step backup takes a consistent read snapshot, folds the WAL in,
#   and is neither blocked nor restarted by the live writers (measured ~2 s
#   for the 2.9 GB prices.db). It also preserves page layout, so rsync's
#   delta transfer stays cheap. We pull the snapshots, delete them
#   server-side, then verify every pulled copy with PRAGMA quick_check.
#
# Why these DBs land here:
#   - futures.db is collected on adamserver via futures-collector.timer (:07)
#   - prices.db is written 24/7 by crypto-collectors (perp_all.py)
#   Both feed tradingdashboard.py's chart rendering. The four sibling-engine
#   DBs (catalyst/options/etf/forex) are read-only mirrors feeding the local
#   public_viewer.py / research runs.

set -u
LOCAL_DIR="$HOME/trading/futures-app"
CRYPTO_DIR="$HOME/trading/crypto-app"
CATALYST_DIR="$HOME/trading/catalyst-trader"
OPTIONS_DIR="$HOME/trading/catalyst-options"
ETF_DIR="$HOME/trading/catalyst-etf"
FOREX_DIR="$HOME/trading/forex-app"
LOG="$LOCAL_DIR/sync.log"
LOCK_DIR="$LOCAL_DIR/.sync.lock.d"
# Server-side snapshot dir — tmpfs (RAM), NOT the 81%-full root disk.
# Keep in sync with SNAP_DIR inside the python heredoc below.
SNAP_DIR="/tmp/mac-sync-snap"

# The DBs we mirror, as parallel arrays (macOS ships bash 3.2 — no associative
# arrays). To add a DB later (e.g. fable.db) append one entry to EACH array:
#   futures  — futures-app candles + trades
#   crypto   — crypto perp candles, ~2.9 GB, hottest writer
#   catalyst — catalyst-trader AI daily picks
#   options  — catalyst-options long-options paper trader
#   etf      — catalyst-etf Sector SPDR trader
#   forex    — forex-app majors candles + paper books
#   fable    — fable-trader 6-book strategy suite
#   pumpfun  — pumpfun-trader 💩COIN screener + $10k paper book
DB_NAMES=(futures crypto catalyst options etf forex fable pumpfun)
DB_FILES=(futures.db prices.db catalyst.db options.db etf.db forex.db fable.db pumpfun.db)
DB_REMOTE=(
    /home/joe/futures-app/futures.db
    /home/joe/crypto-app/prices.db
    /home/joe/catalyst-trader/catalyst.db
    /home/joe/catalyst-options/options.db
    /home/joe/catalyst-etf/etf.db
    /home/joe/forex-app/forex.db
    /home/joe/fable-trader/fable.db
    /home/joe/pumpfun-trader/pumpfun.db
)
FABLE_DIR="$HOME/trading/fable-trader"
PUMPFUN_DIR="$HOME/trading/pumpfun-trader"
DB_DEST=("$LOCAL_DIR" "$CRYPTO_DIR" "$CATALYST_DIR" "$OPTIONS_DIR" "$ETF_DIR" "$FOREX_DIR" "$FABLE_DIR" "$PUMPFUN_DIR")

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

# Make sure every local mirror dir exists. crypto + futures are the apps
# themselves; catalyst-trader, catalyst-options, catalyst-etf live under
# /home/joe on adamserver and are read-only mirrors here just to feed
# public_viewer.py running locally.
for d in "${DB_DEST[@]}"; do
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

    # ── 1) Snapshot every DB server-side ──────────────────────────────────
    # One ssh call. name=path pairs go in as argv; the python comes in on
    # stdin (quoted heredoc — nothing expands locally except $SNAP_ARGS on
    # the ssh line itself). Exit code: 0 all good, 1 some DB failed,
    # 255 ssh/connection failure.
    SNAP_ARGS=""
    for i in "${!DB_NAMES[@]}"; do
        SNAP_ARGS="$SNAP_ARGS ${DB_FILES[$i]}=${DB_REMOTE[$i]}"
    done
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] -- server-side snapshots --"
    /usr/bin/ssh -o ConnectTimeout=10 -o BatchMode=yes adamserver \
        "python3 - $SNAP_ARGS" <<'PYEOF' 2>&1
import os, sqlite3, sys
SNAP_DIR = '/tmp/mac-sync-snap'   # keep in sync with SNAP_DIR in the shell script
os.makedirs(SNAP_DIR, exist_ok=True)
fail = 0
for pair in sys.argv[1:]:
    name, src = pair.split('=', 1)
    dst = os.path.join(SNAP_DIR, name)
    # Never leave last hour's snapshot where rsync could grab it if this
    # one fails — remove first, and again on failure.
    try:
        os.remove(dst)
    except OSError:
        pass
    try:
        s = sqlite3.connect('file:%s?mode=ro' % src, uri=True, timeout=30)
        d = sqlite3.connect(dst)
        # Default pages=-1 → entire DB in ONE backup step: a consistent
        # point-in-time copy with the WAL folded in. Live writers are
        # neither blocked nor able to restart it (unlike the sqlite3 CLI's
        # .backup, which steps 100 pages at a time and restarts from page 1
        # whenever a writer commits — a livelock on a hot 2.9 GB DB).
        s.backup(d)
        d.close()
        s.close()
        print('  %-12s %8.1f MB  -> %s' % (name, os.path.getsize(dst) / 1048576, dst))
    except Exception as e:
        fail = 1
        print('  %-12s SNAPSHOT FAILED: %s' % (name, e))
        try:
            os.remove(dst)
        except OSError:
            pass
sys.exit(fail)
PYEOF
    rc_snap=$?
    if [ "$rc_snap" -eq 255 ]; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ssh to adamserver failed — skipping all pulls, keeping existing local copies"
    elif [ "$rc_snap" -ne 0 ]; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] some snapshots failed (rc=$rc_snap) — failed DBs will be skipped, see above"
    fi

    # ── 2) Pull each snapshot, then verify the local copy ─────────────────
    # -a archive (times, perms) / -v verbose / -z compress on the wire
    # --partial resume interrupted transfers / --stats final summary
    # rsync writes to a hidden temp file and renames into place, so local
    # readers never see a half-written DB. A failed pull leaves the previous
    # local copy untouched.
    PULL_RC=()
    VERIFY=()
    for i in "${!DB_NAMES[@]}"; do
        file="${DB_FILES[$i]}"
        dest="${DB_DEST[$i]}/$file"
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] -- $file --"
        if [ "$rc_snap" -eq 255 ]; then
            PULL_RC+=(255)
            VERIFY+=(skip)
            continue
        fi
        /usr/bin/rsync -avz --partial --stats \
            -e 'ssh -o ConnectTimeout=10 -o BatchMode=yes' \
            "adamserver:$SNAP_DIR/$file" "$dest" 2>&1
        rc=$?
        PULL_RC+=("$rc")
        if [ "$rc" -ne 0 ]; then
            echo "  pull failed (rc=$rc) — keeping previous local copy"
            VERIFY+=(skip)
            continue
        fi
        # The snapshot arrives fully checkpointed, so any -wal/-shm sidecars
        # at the destination belong to the OLD file. SQLite would replay a
        # stale -wal into the fresh DB on next open — a corruption recipe —
        # so drop them now.
        /bin/rm -f "$dest-wal" "$dest-shm"
        # Verify the pulled copy really is a clean database. immutable=1
        # makes sqlite treat the file as read-only media (no sidecars
        # created during the check). quick_check(5) stops after 5 errors.
        res=$(/usr/bin/sqlite3 "file:$dest?mode=ro&immutable=1" 'PRAGMA quick_check(5);' 2>&1)
        if [ "$res" = "ok" ]; then
            echo "  quick_check: ok"
            VERIFY+=(ok)
        else
            echo "  quick_check FAILED for $dest:"
            echo "$res"
            VERIFY+=(FAIL)
        fi
    done

    # ── 3) Server-side cleanup ─────────────────────────────────────────────
    # Snapshots live in tmpfs (RAM) — leaving ~3 GB lying around steals
    # memory from the bots, so delete as soon as the pull is done.
    if [ "$rc_snap" -ne 255 ]; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] -- snapshot cleanup --"
        /usr/bin/ssh -o ConnectTimeout=10 -o BatchMode=yes adamserver \
            "rm -rf $SNAP_DIR" 2>&1
    fi

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

    # Summary: per-DB "pull-rc/verify" so one grep of 'sync done' shows both
    # transfer and integrity status for every DB.
    summary=""
    for i in "${!DB_NAMES[@]}"; do
        summary="$summary ${DB_NAMES[$i]}=${PULL_RC[$i]}/${VERIFY[$i]}"
    done
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ---- sync done (snap=$rc_snap$summary push_doss=$rc_push_doss push_rev=$rc_push_rev) ----"
} >> "$LOG" 2>&1

# Keep log under ~2000 lines
/usr/bin/tail -n 2000 "$LOG" > "$LOG.tmp" && /bin/mv "$LOG.tmp" "$LOG"

#!/bin/bash
# sync_from_adamserver.sh — pull the latest futures.db from adamserver.
# Runs hourly via launchd (see ~/Library/LaunchAgents/com.joe.futures-sync.plist).

set -u
LOCAL_DIR="$HOME/futures-app"
LOG="$LOCAL_DIR/sync.log"
LOCK_DIR="$LOCAL_DIR/.sync.lock.d"
REMOTE="adamserver:/home/joe/futures-app/futures.db"

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
    /usr/bin/rsync -avz --partial --stats \
        -e 'ssh -o ConnectTimeout=10 -o BatchMode=yes' \
        "$REMOTE" "$LOCAL_DIR/futures.db" 2>&1

    rc=$?
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ---- sync done (exit $rc) ----"
} >> "$LOG" 2>&1

# Keep log under ~2000 lines
/usr/bin/tail -n 2000 "$LOG" > "$LOG.tmp" && /bin/mv "$LOG.tmp" "$LOG"

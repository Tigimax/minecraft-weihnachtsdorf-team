#!/bin/bash
# Hourly World & Plugin Snapshot + Off-Node Sync (deployed on mc-main:/usr/local/bin/mc-backup.sh)
set -euo pipefail

WORLD_DIR="${MC_WORLD_DIR:-/var/lib/pterodactyl/volumes/<SERVER_UUID>}"
BACKUP_DIR="${MC_BACKUP_DIR:-/opt/mc-backups}"
HOURLY_DIR="${BACKUP_DIR}/hourly"
DAILY_DIR="${BACKUP_DIR}/daily"
REMOTE_HOST="${MC_BACKUP_REMOTE_HOST:-tigimax@192.168.1.174}"
REMOTE_DIR="${MC_BACKUP_REMOTE_DIR:-/home/tigimax/wd7-backups/worlds}"
SSH_KEY="${MC_BACKUP_SSH_KEY:-/root/.ssh/id_ed25519_backup}"

mkdir -p "$HOURLY_DIR" "$DAILY_DIR"

# Clean up stale temp files
find "$BACKUP_DIR" -maxdepth 2 -name "*.tmp" -mmin +60 -delete 2>/dev/null || true

HOUR=$(date +%H)
DAY=$(date +%Y-%m-%d)
TARGET="${HOURLY_DIR}/world_${HOUR}.tar.gz"
TEMP_TARGET="${TARGET}.tmp"

echo "[mc-backup] Starting hourly snapshot to ${TARGET}..."
DIRS=("world")
[ -d "$WORLD_DIR/world_nether" ] && DIRS+=("world_nether")
[ -d "$WORLD_DIR/world_the_end" ] && DIRS+=("world_the_end")
[ -d "$WORLD_DIR/plugins/WorldGuard" ] && DIRS+=("plugins/WorldGuard")
[ -d "$WORLD_DIR/plugins/Adventskalender" ] && DIRS+=("plugins/Adventskalender")
[ -d "$WORLD_DIR/plugins/BreweryX" ] && DIRS+=("plugins/BreweryX")
[ -d "$WORLD_DIR/plugins/EvenMoreFish" ] && DIRS+=("plugins/EvenMoreFish")

set +e
tar -czf "$TEMP_TARGET" -C "$WORLD_DIR" "${DIRS[@]}"
TAR_RC=$?
set -e
if [ "$TAR_RC" -gt 1 ]; then
  rm -f "$TEMP_TARGET"
  echo "[mc-backup] ERROR: tar failed with exit code $TAR_RC" >&2
  exit "$TAR_RC"
fi
mv -f "$TEMP_TARGET" "$TARGET"
SIZE=$(ls -lh "$TARGET" | awk '{print $5}')
echo "[mc-backup] Hourly snapshot ready: ${TARGET} (${SIZE})"

# Retain last 24h locally & remove old root-level snapshots
find "$HOURLY_DIR" -name "world_*.tar.gz" -mmin +1440 -delete
find "$BACKUP_DIR" -maxdepth 1 -name "world_*.tar.gz*" -delete 2>/dev/null || true

# Daily snapshot (04:xx or if today's daily does not exist yet)
DAILY_TARGET="${DAILY_DIR}/world_${DAY}.tar.gz"
if [ ! -f "$DAILY_TARGET" ] || [ "$HOUR" = "04" ]; then
  cp -f "$TARGET" "$DAILY_TARGET"
  echo "[mc-backup] Daily snapshot saved: ${DAILY_TARGET}"
  find "$DAILY_DIR" -name "world_*.tar.gz" -mtime +14 -delete
fi

# Off-node sync to usrv01
if [ -f "$SSH_KEY" ]; then
  echo "[mc-backup] Syncing to ${REMOTE_HOST}:${REMOTE_DIR}..."
  rsync -az --timeout=180 -e "ssh -i $SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
    "$TARGET" "${REMOTE_HOST}:${REMOTE_DIR}/latest_world.tar.gz"
  rsync -az --timeout=180 -e "ssh -i $SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
    "$DAILY_TARGET" "${REMOTE_HOST}:${REMOTE_DIR}/"
  ssh -i "$SSH_KEY" -o BatchMode=yes "$REMOTE_HOST" \
    "find '$REMOTE_DIR' -name 'world_20*.tar.gz' -mtime +14 -delete" || true
  echo "[mc-backup] Off-node sync completed successfully."
fi

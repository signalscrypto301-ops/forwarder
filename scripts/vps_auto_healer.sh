#!/usr/bin/env bash
# ==============================================================================
# VPS Host RAM & Disk Auto-Healer Script
# Monitors host-level RAM and disk space. If memory exceeds the safety threshold
# (default 90%), automatically drops kernel caches, trims container logs,
# prunes docker builder caches, and triggers container cleanup.
# ==============================================================================

set -eo pipefail

RAM_THRESHOLD="${RAM_THRESHOLD:-90}"
DISK_THRESHOLD="${DISK_THRESHOLD:-90}"
FORCE_HEAL=false

if [[ "$1" == "--force" ]]; then
    FORCE_HEAL=true
fi

# Calculate host RAM utilization using /proc/meminfo
if [[ -f /proc/meminfo ]]; then
    MEM_TOTAL_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
    MEM_AVAIL_KB=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
    MEM_USED_KB=$((MEM_TOTAL_KB - MEM_AVAIL_KB))
    RAM_PERCENT=$((MEM_USED_KB * 100 / MEM_TOTAL_KB))
else
    RAM_PERCENT=0
fi

# Calculate root disk utilization
DISK_PERCENT=$(df -k / | awk 'NR==2 {gsub("%",""); print $5}')

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "[$TIMESTAMP] [VPS-AutoHealer] Host RAM: ${RAM_PERCENT}%, Disk: ${DISK_PERCENT}% (Threshold: ${RAM_THRESHOLD}%)"

if [[ "$RAM_PERCENT" -ge "$RAM_THRESHOLD" ]] || [[ "$DISK_PERCENT" -ge "$DISK_THRESHOLD" ]] || [[ "$FORCE_HEAL" == true ]]; then
    echo "[$TIMESTAMP] ?? Triggering Host Auto-Heal (RAM: ${RAM_PERCENT}%, Disk: ${DISK_PERCENT}%)..."

    # 1. Sync dirty pages to persistent storage
    sync

    # 2. Release kernel pagecache, dentries, and inodes
    if [[ -w /proc/sys/vm/drop_caches ]]; then
        echo 3 > /proc/sys/vm/drop_caches
        echo "[$TIMESTAMP] ? Cleared Linux pagecache and directory/inode caches."
    fi

    # 3. Truncate Docker container logs exceeding 30MB
    if [[ -d /var/lib/docker/containers ]]; then
        LOG_COUNT=0
        for logfile in $(find /var/lib/docker/containers/ -name "*-json.log" -size +30M 2>/dev/null || true); do
            truncate -s 5M "$logfile" 2>/dev/null || true
            LOG_COUNT=$((LOG_COUNT + 1))
        done
        echo "[$TIMESTAMP] ? Truncated $LOG_COUNT bloated Docker container logs (>30MB)."
    fi

    # 4. Prune dangling docker build cache
    if command -v docker >/dev/null 2>&1; then
        docker builder prune -f --filter "until=24h" >/dev/null 2>&1 || true
        echo "[$TIMESTAMP] ? Pruned dangling docker build cache."
    fi

    # 5. Call WhatsApp microservice cleanup endpoint if port 5426 is open
    if command -v curl >/dev/null 2>&1; then
        CLEANUP_RES=$(curl -s -X POST http://127.0.0.1:5426/cleanup -H "x-api-key: forwarder_internal_secret_key_8f3a92b" -m 5 2>/dev/null || true)
        if [[ -n "$CLEANUP_RES" ]]; then
            echo "[$TIMESTAMP] ? Triggered WhatsApp container /cleanup: $CLEANUP_RES"
        fi
    fi

    # Telemetry after heal
    if [[ -f /proc/meminfo ]]; then
        POST_AVAIL_KB=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
        POST_USED_KB=$((MEM_TOTAL_KB - POST_AVAIL_KB))
        POST_RAM_PERCENT=$((POST_USED_KB * 100 / MEM_TOTAL_KB))
        FREED_MB=$(((MEM_AVAIL_KB - POST_AVAIL_KB) * -1 / 1024))
        echo "[$TIMESTAMP] ?? Auto-Heal Complete: RAM ${RAM_PERCENT}% -> ${POST_RAM_PERCENT}% (${FREED_MB}MB freed)."
    fi
else
    echo "[$TIMESTAMP] ?? Resources healthy. No action required."
fi